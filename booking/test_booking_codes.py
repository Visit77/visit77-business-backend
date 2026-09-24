from datetime import date, datetime, timezone as datetime_timezone
from copy import deepcopy
from decimal import Decimal
from unittest.mock import call, patch

from django.test import TestCase, override_settings
from pypdf import PdfReader
from io import BytesIO

from booking.models import (
    BOOKING_CODES_PER_SERIES,
    DOCUMENT_CODES_PER_SERIES,
    Booking,
    BookingRoom,
    BookingCodeSequence,
    Guest,
    Hotel,
    Invoice,
    InvoiceLine,
    InvoiceNumberSequence,
    HotelDocumentSequence,
    OTABookingYearSequence,
    OTADocumentYearSequence,
    Payment,
    ReceiptNumberSequence,
    RatePlan,
    RoomType,
    format_booking_code,
    format_invoice_number,
    format_receipt_number,
)
from booking.booking_services.email import send_booking_confirmation_email
from booking.booking_services.sms import normalize_sms_phone_number
from booking.booking_services.receipt import (
    _contact_values,
    build_invoice_snapshot,
    ensure_receipt_pdf,
    render_invoice_pdf,
    render_receipt_pdf,
)
from booking.serializers import BookingSerializer, InvoiceSerializer
from booking.services import record_payment
from booking.tasks import (
    queue_booking_confirmation_notifications,
    send_booking_confirmation_email_task,
    send_booking_confirmation_sms_task,
)


class BookingCodeTests(TestCase):
    def setUp(self):
        self.hotel = Hotel.objects.create(core_business_id=987654321, name="Code Test Hotel", document_code="MAND")

    def test_default_hotel_document_code_is_unique_four_capital_letters(self):
        first = Hotel.objects.create(core_business_id=987654323, name="First Default Hotel")
        second = Hotel.objects.create(core_business_id=987654324, name="Second Default Hotel")
        self.assertRegex(first.document_code, r"^[A-Z]{4}$")
        self.assertRegex(second.document_code, r"^[A-Z]{4}$")
        self.assertNotEqual(first.document_code, second.document_code)

    def create_booking(self, reference):
        return Booking.objects.create(
            reference=reference,
            hotel=self.hotel,
            check_in=date(2026, 9, 1),
            check_out=date(2026, 9, 2),
            contact_name="Test Guest",
            contact_phone="09123456789",
        )

    def test_receipt_contact_values_hide_empty_and_split_json_lists(self):
        self.assertEqual(_contact_values('[""]'), [])
        self.assertEqual(
            _contact_values('["82 58 5", "09 123 456"]'),
            ["82 58 5", "09 123 456"],
        )

    def add_confirmation_rooms(self, booking):
        twin = RoomType.objects.create(
            hotel=self.hotel,
            core_room_type_id=81001,
            name="Standard Twin Room",
        )
        deluxe = RoomType.objects.create(
            hotel=self.hotel,
            core_room_type_id=81002,
            name="Deluxe Room",
        )
        twin_rate = RatePlan.objects.create(
            room_type=twin,
            code="twin-rate",
            name="Twin Rate",
            default_price=100,
        )
        deluxe_rate = RatePlan.objects.create(
            room_type=deluxe,
            code="deluxe-rate",
            name="Deluxe Rate",
            default_price=100,
        )
        BookingRoom.objects.create(
            booking=booking,
            room_type=twin,
            rate_plan=twin_rate,
            quantity=2,
        )
        BookingRoom.objects.create(
            booking=booking,
            room_type=deluxe,
            rate_plan=deluxe_rate,
            quantity=1,
        )

    def test_code_is_allocated_and_serialized(self):
        booking = self.create_booking("TEST-CODE-1")

        self.assertRegex(booking.booking_code, r"^[A-Z0-9]{6}$")
        self.assertEqual(BookingSerializer(booking).data["booking_code"], booking.booking_code)

        invoice = Invoice.objects.create(
            booking=booking,
            invoice_number="TEST-INVOICE-1",
            currency="MMK",
        )
        invoice_data = InvoiceSerializer(invoice).data
        self.assertEqual(invoice_data["booking_code"], booking.booking_code)
        self.assertEqual(invoice_data["invoice_details"]["booking_code"], booking.booking_code)

    def test_unpaid_invoice_has_pdf_url_and_can_be_rendered(self):
        booking = self.create_booking("TEST-UNPAID-INVOICE-PDF")
        invoice = Invoice.objects.create(
            booking=booking,
            currency="MMK",
            subtotal=Decimal("1000"),
            total=Decimal("1000"),
        )
        InvoiceLine.objects.create(
            invoice=invoice,
            description="Room charge",
            quantity=1,
            unit_price=Decimal("1000"),
            total=Decimal("1000"),
            metadata={"line_type": "room"},
        )

        data = InvoiceSerializer(invoice).data
        self.assertTrue(data["invoice_pdf_url"].endswith(
            f"/public/bookings/{booking.public_token}/invoices/{invoice.id}/pdf/"
        ))
        snapshot = build_invoice_snapshot(invoice)
        self.assertEqual(snapshot["amount_paid"], "0")
        self.assertEqual(snapshot["remaining_balance"], "1000.00")
        self.assertTrue(render_invoice_pdf(snapshot).startswith(b"%PDF"))

    def test_codes_increment(self):
        first = self.create_booking("TEST-CODE-1")
        second = self.create_booking("TEST-CODE-2")

        self.assertRegex(first.booking_code, r"^[A-Z0-9]{6}$")
        self.assertRegex(second.booking_code, r"^[A-Z0-9]{6}$")
        self.assertNotEqual(first.booking_code, second.booking_code)

    def test_invoice_and_receipt_numbers_increment_independently(self):
        booking = self.create_booking("TEST-DOCUMENT-CODES")
        booking.source = Booking.Source.PMS
        booking.save(update_fields=["source"])
        first_invoice = Invoice.objects.create(booking=booking, currency="MMK")
        second_invoice = Invoice.objects.create(booking=booking, currency="MMK")

        first_receipt = Payment.objects.create(
            booking=booking,
            invoice=first_invoice,
            provider=Payment.Provider.CASH,
            status=Payment.Status.PAID,
            amount=100,
            currency="MMK",
            invoice_number=first_invoice.invoice_number,
        )
        second_receipt = Payment.objects.create(
            booking=booking,
            invoice=first_invoice,
            provider=Payment.Provider.CASH,
            status=Payment.Status.PAID,
            amount=100,
            currency="MMK",
            invoice_number=first_invoice.invoice_number,
        )

        self.assertEqual(first_invoice.invoice_number, "MAND-INV-26-000001")
        self.assertEqual(second_invoice.invoice_number, "MAND-INV-26-000002")
        self.assertEqual(first_receipt.receipt_number, "MAND-REC-26-000001")
        self.assertEqual(second_receipt.receipt_number, "MAND-REC-26-000002")

    def test_pms_reservation_numbers_use_an_independent_hotel_year_series(self):
        first = Booking.objects.create(
            reference="PMS-RESERVATION-1",
            hotel=self.hotel,
            source=Booking.Source.PMS,
            check_in=date(2026, 9, 1),
            check_out=date(2026, 9, 2),
            contact_name="First Guest",
            contact_phone="091111111",
        )
        second = Booking.objects.create(
            reference="PMS-RESERVATION-2",
            hotel=self.hotel,
            source=Booking.Source.PMS,
            check_in=date(2026, 9, 2),
            check_out=date(2026, 9, 3),
            contact_name="Second Guest",
            contact_phone="092222222",
        )
        ota = self.create_booking("OTA-WITHOUT-RESERVATION-CODE")

        self.assertEqual(first.reservation_code, "MAND-RES-26-000001")
        self.assertEqual(second.reservation_code, "MAND-RES-26-000002")
        self.assertIsNone(ota.reservation_code)
        self.assertEqual(
            BookingSerializer(first).data["reservation_code"],
            "MAND-RES-26-000001",
        )

    def test_hotel_document_series_are_separate_and_reset_each_year(self):
        other_hotel = Hotel.objects.create(core_business_id=987654322, name="Other Hotel", document_code="GERD")
        other_booking = Booking.objects.create(
            reference="OTHER-HOTEL", hotel=other_hotel,
            source=Booking.Source.PMS,
            check_in=date(2026, 9, 1), check_out=date(2026, 9, 2),
            contact_name="Guest", contact_phone="09123456789",
        )
        first_booking = self.create_booking("FIRST-HOTEL")
        first_booking.source = Booking.Source.PMS
        first_booking.save(update_fields=["source"])
        with patch("booking.models.timezone.now", return_value=datetime(2026, 12, 31, 12, tzinfo=datetime_timezone.utc)):
            first_invoice = Invoice.objects.create(booking=first_booking, currency="MMK")
            other_invoice = Invoice.objects.create(booking=other_booking, currency="MMK")
            first_receipt = Payment.objects.create(
                booking=first_booking, invoice=first_invoice, provider=Payment.Provider.CASH,
                status=Payment.Status.PAID, amount=100, currency="MMK",
                invoice_number=first_invoice.invoice_number,
            )
            other_receipt = Payment.objects.create(
                booking=other_booking, invoice=other_invoice, provider=Payment.Provider.CASH,
                status=Payment.Status.PAID, amount=100, currency="MMK",
                invoice_number=other_invoice.invoice_number,
            )
        self.assertEqual(first_invoice.invoice_number, "MAND-INV-26-000001")
        self.assertEqual(other_invoice.invoice_number, "GERD-INV-26-000001")
        self.assertEqual(first_receipt.receipt_number, "MAND-REC-26-000001")
        self.assertEqual(other_receipt.receipt_number, "GERD-REC-26-000001")
        with patch("booking.models.timezone.now", return_value=datetime(2027, 1, 1, 12, tzinfo=datetime_timezone.utc)):
            next_invoice = Invoice.objects.create(booking=first_booking, currency="MMK")
        self.assertEqual(next_invoice.invoice_number, "MAND-INV-27-000001")
        self.assertEqual(HotelDocumentSequence.objects.count(), 7)

    def test_ota_code_changes_only_after_payment_and_pms_remains_random(self):
        ota = self.create_booking("OTA-PENDING")
        pending_code = ota.booking_code
        self.assertRegex(pending_code, r"^[A-Z0-9]{6}$")
        ota.status = Booking.Status.CONFIRMED
        ota.save(update_fields=["status"])
        self.assertEqual(ota.booking_code, pending_code)
        ota.amount_paid = 100
        ota.save(update_fields=["amount_paid"])
        self.assertEqual(ota.booking_code, "V77-HTL-26-000001")
        self.assertEqual(OTABookingYearSequence.objects.get(year=2026).last_value, 1)
        pms = self.create_booking("PMS-CODE")
        pms.source = Booking.Source.PMS
        pms.status = Booking.Status.CONFIRMED
        pms.amount_paid = 100
        pms.save(update_fields=["source", "status", "amount_paid"])
        self.assertRegex(pms.booking_code, r"^[A-Z0-9]{6}$")

    def test_ota_booking_sequence_resets_next_year(self):
        first = self.create_booking("OTA-YEAR-2026")
        second = self.create_booking("OTA-YEAR-2027")
        with patch("booking.models.timezone.now", return_value=datetime(2026, 12, 31, 12, tzinfo=datetime_timezone.utc)):
            first.status = Booking.Status.CONFIRMED
            first.amount_paid = 1
            first.save(update_fields=["status", "amount_paid"])
        with patch("booking.models.timezone.now", return_value=datetime(2027, 1, 1, 12, tzinfo=datetime_timezone.utc)):
            second.status = Booking.Status.CONFIRMED
            second.amount_paid = 1
            second.save(update_fields=["status", "amount_paid"])
        self.assertEqual(first.booking_code, "V77-HTL-26-000001")
        self.assertEqual(second.booking_code, "V77-HTL-27-000001")

    def test_document_number_series_rolls_over(self):
        self.assertEqual(format_invoice_number(DOCUMENT_CODES_PER_SERIES), "V77-INV-A9999999")
        self.assertEqual(format_invoice_number(DOCUMENT_CODES_PER_SERIES + 1), "V77-INV-B0000001")
        self.assertEqual(format_receipt_number(DOCUMENT_CODES_PER_SERIES + 1), "V77-REC-B0000001")

        booking = self.create_booking("TEST-DOCUMENT-ROLLOVER")
        booking.source = Booking.Source.PMS
        booking.save(update_fields=["source"])
        InvoiceNumberSequence.objects.update_or_create(
            pk=1, defaults={"last_value": DOCUMENT_CODES_PER_SERIES}
        )
        invoice = Invoice.objects.create(booking=booking, currency="MMK")
        self.assertEqual(invoice.invoice_number, "MAND-INV-26-000001")

        ReceiptNumberSequence.objects.update_or_create(
            pk=1, defaults={"last_value": DOCUMENT_CODES_PER_SERIES}
        )
        receipt = Payment.objects.create(
            booking=booking,
            invoice=invoice,
            provider=Payment.Provider.CASH,
            status=Payment.Status.PAID,
            amount=100,
            currency="MMK",
            invoice_number=invoice.invoice_number,
        )
        self.assertEqual(receipt.receipt_number, "MAND-REC-26-000001")

    def test_ota_invoice_and_receipt_always_use_visit77_year_series(self):
        other_hotel = Hotel.objects.create(
            core_business_id=987654322,
            name="Other Hotel",
            document_code="GERD",
        )
        first_booking = self.create_booking("OTA-DOCUMENT-FIRST")
        other_booking = Booking.objects.create(
            reference="OTA-DOCUMENT-OTHER",
            hotel=other_hotel,
            source=Booking.Source.OTA,
            check_in=date(2026, 9, 1),
            check_out=date(2026, 9, 2),
            contact_name="Guest",
            contact_phone="09123456789",
        )

        first_invoice = Invoice.objects.create(booking=first_booking, currency="MMK")
        second_invoice = Invoice.objects.create(booking=other_booking, currency="MMK")
        first_receipt = Payment.objects.create(
            booking=first_booking,
            invoice=first_invoice,
            provider=Payment.Provider.CASH,
            status=Payment.Status.PAID,
            amount=100,
            currency="MMK",
            invoice_number=first_invoice.invoice_number,
        )
        second_receipt = Payment.objects.create(
            booking=other_booking,
            invoice=second_invoice,
            provider=Payment.Provider.CASH,
            status=Payment.Status.PAID,
            amount=100,
            currency="MMK",
            invoice_number=second_invoice.invoice_number,
        )

        self.assertEqual(first_invoice.invoice_number, "V77-INV-26-000001")
        self.assertEqual(second_invoice.invoice_number, "V77-INV-26-000002")
        self.assertEqual(first_receipt.receipt_number, "V77-REC-26-000001")
        self.assertEqual(second_receipt.receipt_number, "V77-REC-26-000002")
        self.assertEqual(
            OTADocumentYearSequence.objects.get(year=2026, kind="INV").last_value,
            2,
        )
        self.assertEqual(
            OTADocumentYearSequence.objects.get(year=2026, kind="REC").last_value,
            2,
        )

    def test_pending_payment_does_not_get_a_receipt_number(self):
        booking = self.create_booking("TEST-PENDING-RECEIPT")
        invoice = Invoice.objects.create(booking=booking, currency="MMK")
        payment = Payment.objects.create(
            booking=booking,
            invoice=invoice,
            provider=Payment.Provider.CASH,
            status=Payment.Status.PENDING,
            amount=100,
            currency="MMK",
            invoice_number=invoice.invoice_number,
        )
        self.assertIsNone(payment.receipt_number)

        payment.status = Payment.Status.PAID
        payment.save(update_fields=["status"])
        payment.refresh_from_db()
        self.assertEqual(payment.receipt_number, "V77-REC-26-000001")

    def test_pms_payment_also_creates_a_receipt(self):
        self.hotel.address = "Stale local address"
        self.hotel.phone = "Stale local phone"
        self.hotel.core_snapshot = {
            "address_info": "1 Hotel Road, Yangon",
            "phone": ["09123456789", "019876543"],
            "email": ["hotel@example.com", "frontdesk@example.com"],
        }
        self.hotel.save(update_fields=["address", "phone", "core_snapshot"])
        booking = self.create_booking("TEST-PMS-RECEIPT")
        booking.source = Booking.Source.PMS
        booking.save(update_fields=["source"])
        invoice = Invoice.objects.create(
            booking=booking,
            currency="MMK",
            subtotal=5000,
            total=5000,
        )
        InvoiceLine.objects.create(
            invoice=invoice,
            description="PMS Room Charge",
            quantity=1,
            unit_price=5000,
            total=5000,
            metadata={"line_type": "room"},
        )

        payment = record_payment(booking, {
            "invoice_id": invoice.id,
            "provider": Payment.Provider.CASH,
            "amount": 5000,
            "status": Payment.Status.PAID,
        })

        self.assertEqual(payment.receipt_number, "MAND-REC-26-000001")
        booking.refresh_from_db()
        self.assertEqual(payment.receipt_snapshot["booking"]["booking_code"], booking.booking_code)
        self.assertEqual(
            payment.receipt_snapshot["booking"]["reservation_code"],
            booking.reservation_code,
        )
        self.assertRegex(booking.booking_code, r"^[A-Z0-9]{6}$")
        self.assertEqual(payment.receipt_snapshot["provider"], Payment.Provider.CASH)
        self.assertEqual(payment.receipt_snapshot["issuer"]["name"], self.hotel.name)
        self.assertEqual(payment.receipt_snapshot["issuer"]["address"], "1 Hotel Road, Yangon")
        self.assertEqual(payment.receipt_snapshot["issuer"]["phone"], "09123456789, 019876543")
        self.assertEqual(
            payment.receipt_snapshot["issuer"]["email"],
            "hotel@example.com, frontdesk@example.com",
        )
        self.assertEqual(payment.receipt_snapshot["booking"]["hotel_address"], "1 Hotel Road, Yangon")
        self.assertEqual(payment.receipt_snapshot["booking"]["hotel_phone"], "09123456789, 019876543")
        self.assertEqual(
            payment.receipt_snapshot["booking"]["hotel_email"],
            "hotel@example.com, frontdesk@example.com",
        )
        self.assertEqual(payment.receipt_snapshot["issuer"]["branding"], "hotel")
        self.assertEqual(
            payment.receipt_snapshot["issuer"]["footer_text"],
            "Powered by Visit77",
        )
        for render in (render_receipt_pdf, render_invoice_pdf):
            pdf_text = "\n".join(
                page.extract_text()
                for page in PdfReader(BytesIO(render(payment.receipt_snapshot))).pages
            )
            self.assertIn("Discount", pdf_text)
            self.assertIn("Adjustment", pdf_text)
            self.assertIn(self.hotel.name, pdf_text)
            self.assertIn("Powered by Visit77", pdf_text)
            self.assertIn("Reservation ID", pdf_text)
            self.assertIn(booking.reservation_code, pdf_text)
            self.assertNotIn("Booking ID", pdf_text)
            if render is render_invoice_pdf:
                self.assertIn("Payment Status", pdf_text)
                self.assertNotIn("Amount Due", pdf_text)
            else:
                self.assertIn("Amount Due", pdf_text)

    def test_receipt_groups_same_room_type_and_sums_quantity(self):
        booking = self.create_booking("TEST-GROUPED-ROOM-RECEIPT")
        booking.source = Booking.Source.PMS
        booking.status = Booking.Status.CONFIRMED
        booking.save(update_fields=["source", "status"])
        room_type = RoomType.objects.create(
            hotel=self.hotel,
            core_room_type_id=82001,
            name="Penthouse",
        )
        rate_plan = RatePlan.objects.create(
            room_type=room_type,
            code="penthouse-rate",
            name="Penthouse Rate",
            default_price=1000,
        )
        BookingRoom.objects.bulk_create([
            BookingRoom(
                booking=booking, room_type=room_type, rate_plan=rate_plan,
                quantity=1, extra_beds=0,
            ),
            BookingRoom(
                booking=booking, room_type=room_type, rate_plan=rate_plan,
                quantity=1, extra_beds=1,
            ),
            BookingRoom(
                booking=booking, room_type=room_type, rate_plan=rate_plan,
                quantity=1, extra_beds=0,
            ),
        ])
        invoice = Invoice.objects.create(
            booking=booking,
            currency="MMK",
            subtotal=3000,
            total=3000,
        )

        payment = record_payment(booking, {
            "invoice_id": invoice.id,
            "provider": Payment.Provider.CASH,
            "amount": 3000,
            "status": Payment.Status.PAID,
        })

        self.assertEqual(payment.receipt_snapshot["booking"]["rooms"], [{
            "room_type": "Penthouse",
            "quantity": 3,
            "extra_beds": 1,
        }])

    @override_settings(STORAGES={
        "default": {"BACKEND": "django.core.files.storage.InMemoryStorage"},
        "private": {"BACKEND": "django.core.files.storage.InMemoryStorage"},
        "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"},
    })
    def test_paid_receipt_snapshot_and_pdf_are_generated_once(self):
        booking = self.create_booking("TEST-RECEIPT-PDF")
        Guest.objects.create(
            booking=booking,
            name="Receipt Guest",
            email="receipt@example.com",
            is_primary=True,
        )
        invoice = Invoice.objects.create(
            booking=booking,
            charge_scope=Invoice.ChargeScope.OTA,
            currency="MMK",
            subtotal=1000,
            total=1000,
        )
        InvoiceLine.objects.create(
            invoice=invoice,
            description="Test Room x 1 x 1 Night",
            quantity=1,
            unit_price=1000,
            total=1000,
            metadata={"line_type": "room"},
        )
        payment = record_payment(booking, {
            "invoice_id": invoice.id,
            "provider": Payment.Provider.CASH,
            "amount": 1000,
            "status": Payment.Status.PAID,
        })

        booking.refresh_from_db()
        self.assertEqual(payment.receipt_snapshot["booking"]["booking_code"], booking.booking_code)
        self.assertEqual(booking.booking_code, "V77-HTL-26-000001")
        self.assertEqual(payment.receipt_snapshot["amount_paid"], "1000.00")
        self.assertEqual(payment.receipt_snapshot["issuer"]["branding"], "visit77")
        self.assertEqual(payment.receipt_snapshot["issuer"]["footer_text"], "")
        self.assertFalse(payment.receipt_pdf)

        receipt_url = (
            f"/api/v1/public/bookings/{booking.public_token}/"
            f"receipts/{payment.id}/pdf/"
        )
        response = self.client.get(receipt_url)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "application/pdf")
        self.assertEqual(
            response["Content-Disposition"],
            f'inline; filename="{payment.receipt_number}.pdf"',
        )
        receipt_bytes = b"".join(response.streaming_content)
        self.assertTrue(receipt_bytes.startswith(b"%PDF"))
        invoice_url = (
            f"/api/v1/public/bookings/{booking.public_token}/"
            f"invoices/{payment.invoice_id}/pdf/"
        )
        invoice_response = self.client.get(invoice_url)
        self.assertEqual(invoice_response.status_code, 200)
        self.assertEqual(invoice_response["Content-Type"], "application/pdf")
        self.assertEqual(
            invoice_response["Content-Disposition"],
            f'inline; filename="{payment.invoice_number}.pdf"',
        )
        invoice_bytes = b"".join(invoice_response.streaming_content)
        self.assertTrue(invoice_bytes.startswith(b"%PDF"))
        for pdf_bytes in (receipt_bytes, invoice_bytes):
            text = "\n".join(page.extract_text() for page in PdfReader(BytesIO(pdf_bytes)).pages)
            self.assertIn("Visit 77 Company Limited", text)
            self.assertIn(
                "contact.myanmar@visit77.com, (+95) 988 577 0011",
                text,
            )
            labels = (
                "Total Room Charge", "Additional Charges", "Subtotal",
                "Grand Total", "Amount Paid", "Amount Due",
            )
            positions = [text.index(label) for label in labels]
            self.assertEqual(positions, sorted(positions))
            self.assertNotIn("Service Charge", text)
            self.assertNotIn("\nTax\n", text)
            self.assertNotIn("Discount", text)
            self.assertNotIn("Adjustment", text)
        adjusted_snapshot = deepcopy(payment.receipt_snapshot)
        adjusted_snapshot["invoice"]["lines"].append({
            "description": "Adjustment", "total": "50", "line_type": "adjustment",
        })
        adjusted_snapshot["invoice"]["subtotal"] = "1050"
        adjusted_snapshot["invoice"]["discount_total"] = "100"
        adjusted_snapshot["invoice"]["invoice_total"] = "950"
        adjusted_snapshot["invoice"]["tax_charges"] = [
            {"title": "Hidden Tax", "mode": "do_not_show", "amount": "0"},
        ]
        adjusted_text = "\n".join(
            page.extract_text()
            for page in PdfReader(BytesIO(render_receipt_pdf(adjusted_snapshot))).pages
        )
        self.assertNotIn("Adjustment", adjusted_text)
        self.assertNotIn("Discount", adjusted_text)
        self.assertNotIn("Hidden Tax", adjusted_text)
        self.assertNotIn("\nTax\n", adjusted_text)
        self.assertNotIn("Total Charges", adjusted_text)
        self.assertIn("Subtotal\n MMK 950", adjusted_text)

        charge_snapshot = deepcopy(payment.receipt_snapshot)
        charge_snapshot["invoice"]["lines"].extend([
            {
                "description": "Service Charge",
                "total": "50",
                "line_type": "invoice_service_charge",
                "metadata": {"charge_index": 0},
            },
            {
                "description": "Cleaning Fee",
                "total": "25",
                "line_type": "invoice_service_charge",
                "metadata": {"charge_index": 1},
            },
        ])
        charge_snapshot["invoice"]["service_charges"] = [
            {"title": "Service Charge", "mode": "percentage", "value": "5", "amount": "50"},
            {"title": "Cleaning Fee", "mode": "fixed", "value": "25", "amount": "25"},
        ]
        charge_snapshot["invoice"]["tax_charges"] = [
            {"title": "VAT", "mode": "percentage", "value": "7", "amount": "70"},
            {"title": "Tourism Tax", "mode": "fixed", "value": "5", "amount": "5"},
        ]
        charge_text = "\n".join(
            page.extract_text()
            for page in PdfReader(BytesIO(render_receipt_pdf(charge_snapshot))).pages
        )
        self.assertIn("Service Charge (5%)", charge_text)
        self.assertIn("Cleaning Fee (fixed amount)", charge_text)
        self.assertIn("VAT (7%)", charge_text)
        self.assertIn("Tourism Tax (fixed amount)", charge_text)
        payment.refresh_from_db()
        original_name = payment.receipt_pdf.name
        with payment.receipt_pdf.open("rb") as receipt_file:
            self.assertTrue(receipt_file.read(4).startswith(b"%PDF"))

        booking.contact_name = "Changed Later"
        booking.save(update_fields=["contact_name"])
        payment = ensure_receipt_pdf(payment)
        self.assertEqual(payment.receipt_pdf.name, original_name)
        self.assertEqual(payment.receipt_snapshot["guest"]["name"], "Receipt Guest")

        with patch("booking.booking_services.email.EmailMultiAlternatives") as email_class:
            self.assertTrue(send_booking_confirmation_email(booking))
            self.assertEqual(email_class.return_value.attach.call_count, 2)
            logo_attachment = email_class.return_value.attach.call_args_list[0].args[0]
            self.assertEqual(logo_attachment["Content-ID"], "<visit77-logo>")
            attachment = email_class.return_value.attach.call_args_list[1].args
            self.assertEqual(attachment[0], f"{payment.receipt_number}.pdf")
            self.assertTrue(attachment[1].startswith(b"%PDF"))
            self.assertEqual(attachment[2], "application/pdf")

    def test_series_rolls_over_at_ten_million(self):
        self.assertEqual(format_booking_code(BOOKING_CODES_PER_SERIES), "V77H-A09999999")
        self.assertEqual(format_booking_code(BOOKING_CODES_PER_SERIES + 1), "V77H-B00000001")

        BookingCodeSequence.objects.update_or_create(
            pk=1, defaults={"last_value": BOOKING_CODES_PER_SERIES}
        )
        booking = self.create_booking("TEST-CODE-ROLLOVER")
        self.assertRegex(booking.booking_code, r"^[A-Z0-9]{6}$")

    @override_settings(
        BOOKING_FRONTEND_URL="https://booking.example.com",
        DEFAULT_FROM_EMAIL="Visit77 Customer Service <no-reply@visit77.com>",
    )
    @patch("booking.booking_services.email.EmailMultiAlternatives")
    def test_confirmation_email_uses_booking_code(self, email_class_mock):
        booking = self.create_booking("INTERNAL-EMAIL-REFERENCE")
        self.hotel.phone = '["012312", "123123"]'
        self.hotel.core_snapshot = {
            "email": '["hotel@example.com", "frontdesk@example.com"]',
        }
        self.hotel.save(update_fields=["phone", "core_snapshot"])
        self.add_confirmation_rooms(booking)
        Guest.objects.create(
            booking=booking,
            name="Email Guest",
            email="guest@example.com",
            is_primary=True,
        )

        send_booking_confirmation_email(booking)
        email_class_mock.assert_called_once()
        self.assertEqual(
            email_class_mock.call_args.kwargs["from_email"],
            "Visit77 Customer Service <no-reply@visit77.com>",
        )
        email_class_mock.return_value.send.assert_called_once_with(fail_silently=False)
        email_class_mock.return_value.attach_alternative.assert_called_once()
        message = email_class_mock.call_args.kwargs["body"]
        html_message = email_class_mock.return_value.attach_alternative.call_args.args[0]
        self.assertIn(f"Booking ID: {booking.booking_code}", message)
        self.assertIn(booking.booking_code, html_message)
        self.assertIn('src="cid:visit77-logo"', html_message)
        logo_attachment = email_class_mock.return_value.attach.call_args.args[0]
        self.assertEqual(logo_attachment["Content-ID"], "<visit77-logo>")
        self.assertIn('href="tel:012312"', html_message)
        self.assertIn('href="tel:123123"', html_message)
        self.assertIn('href="mailto:hotel@example.com"', html_message)
        self.assertIn('href="mailto:frontdesk@example.com"', html_message)
        self.assertNotIn('[&quot;&quot;]', html_message)
        self.assertIn("Your Booking is Confirmed and Paid.", html_message)
        self.assertNotIn("Your Booking is Pending Payment.", html_message)
        self.assertIn(">PAID</span", html_message)
        self.assertNotIn(">PENDING</span", html_message)
        self.assertIn(
            f'href="https://booking.example.com/bookings/{booking.public_token}"',
            html_message,
        )
        self.assertIn("View Booking", html_message)
        self.assertNotIn('[&quot;012312&quot;', html_message)
        self.assertNotIn("{{", html_message)
        self.assertIn("Room: Standard Twin Room x 2", message)
        self.assertIn("Room: Deluxe Room x 1", message)
        self.assertNotIn(booking.reference, message)

    @override_settings(BOOKING_FRONTEND_URL="https://booking.example.com")
    @patch("booking.booking_services.sms.send_custom_sms")
    def test_confirmation_sms_uses_booking_code(self, send_sms_mock):
        booking = self.create_booking("INTERNAL-SMS-REFERENCE")
        self.add_confirmation_rooms(booking)
        Guest.objects.create(
            booking=booking,
            name="SMS Guest",
            phone="09123456789",
            is_primary=True,
        )

        self.assertTrue(send_booking_confirmation_sms_task(str(booking.id)))
        message = send_sms_mock.call_args.kwargs["message"]
        self.assertIn(f"Booking ID: {booking.booking_code}", message)
        self.assertIn("Room: Standard Twin Room x 2", message)
        self.assertIn("Room: Deluxe Room x 1", message)
        self.assertNotIn(booking.reference, message)

    @patch("booking.booking_services.sms.send_custom_sms")
    def test_confirmation_sms_prefers_booking_contact_phone(self, send_sms_mock):
        booking = self.create_booking("CONTACT-PHONE-PRIORITY")
        booking.contact_phone = "09999999999"
        booking.save(update_fields=["contact_phone"])
        Guest.objects.create(
            booking=booking,
            name="Primary Guest",
            phone="09111111111",
            is_primary=True,
        )

        self.assertTrue(send_booking_confirmation_sms_task(str(booking.id), "guest"))
        self.assertEqual(send_sms_mock.call_args.kwargs["phone_no"], "09999999999")

    @patch("booking.booking_services.email.EmailMultiAlternatives")
    def test_confirmation_email_prefers_booking_contact_email(self, email_class_mock):
        booking = self.create_booking("CONTACT-EMAIL-PRIORITY")
        booking.contact_email = "booking-contact@example.com"
        booking.save(update_fields=["contact_email"])
        Guest.objects.create(
            booking=booking,
            name="Primary Guest",
            email="primary-guest@example.com",
            is_primary=True,
        )

        self.assertTrue(send_booking_confirmation_email(booking))
        self.assertEqual(
            email_class_mock.call_args.kwargs["to"],
            ["booking-contact@example.com"],
        )

    @patch("booking.booking_services.email.EmailMultiAlternatives")
    def test_ota_confirmation_also_emails_hotel_verification_contact(self, email_class_mock):
        booking = self.create_booking("OTA-HOTEL-EMAIL")
        self.hotel.core_snapshot = {
            "booking_notification_email": "hotel-bookings@example.com",
        }
        self.hotel.save(update_fields=["core_snapshot"])
        Guest.objects.create(
            booking=booking,
            name="Email Guest",
            email="guest@example.com",
            is_primary=True,
        )

        self.assertTrue(send_booking_confirmation_email_task(str(booking.id)))
        self.assertEqual(email_class_mock.call_count, 2)
        recipients = [call.kwargs["to"] for call in email_class_mock.call_args_list]
        self.assertEqual(
            recipients,
            [["guest@example.com"], ["hotel-bookings@example.com"]],
        )

    @patch("booking.booking_services.email.EmailMultiAlternatives")
    def test_ota_guest_and_hotel_email_can_run_as_independent_tasks(self, email_class_mock):
        booking = self.create_booking("OTA-SPLIT-EMAIL")
        self.hotel.core_snapshot = {
            "booking_notification_email": "hotel-bookings@example.com",
        }
        self.hotel.save(update_fields=["core_snapshot"])
        Guest.objects.create(
            booking=booking,
            name="Email Guest",
            email="guest@example.com",
            is_primary=True,
        )

        self.assertTrue(send_booking_confirmation_email_task(str(booking.id), "guest"))
        self.assertTrue(send_booking_confirmation_email_task(str(booking.id), "hotel"))
        self.assertEqual(
            [call.kwargs["to"] for call in email_class_mock.call_args_list],
            [["guest@example.com"], ["hotel-bookings@example.com"]],
        )

    @patch("booking.booking_services.sms.send_custom_sms")
    def test_ota_confirmation_also_texts_hotel_verification_contact(self, send_sms_mock):
        booking = self.create_booking("OTA-HOTEL-SMS")
        self.hotel.core_snapshot = {
            "booking_notification_phone_number": "+959333333333",
        }
        self.hotel.save(update_fields=["core_snapshot"])
        Guest.objects.create(
            booking=booking,
            name="SMS Guest",
            phone="09123456789",
            is_primary=True,
        )

        self.assertTrue(send_booking_confirmation_sms_task(str(booking.id)))
        self.assertEqual(send_sms_mock.call_count, 2)
        recipients = [
            call.kwargs["phone_no"] for call in send_sms_mock.call_args_list
        ]
        self.assertEqual(recipients, ["09123456789", "+959333333333"])

    @patch("booking.booking_services.sms.send_custom_sms")
    def test_ota_guest_and_hotel_sms_can_run_as_independent_tasks(self, send_sms_mock):
        booking = self.create_booking("OTA-SPLIT-SMS")
        self.hotel.core_snapshot = {
            "booking_notification_phone_number": "+95 9 333-333-333",
        }
        self.hotel.save(update_fields=["core_snapshot"])
        Guest.objects.create(
            booking=booking,
            name="SMS Guest",
            phone="09123456789",
            is_primary=True,
        )

        self.assertTrue(send_booking_confirmation_sms_task(str(booking.id), "guest"))
        self.assertTrue(send_booking_confirmation_sms_task(str(booking.id), "hotel"))
        self.assertEqual(
            [call.kwargs["phone_no"] for call in send_sms_mock.call_args_list],
            ["09123456789", "+95 9 333-333-333"],
        )

    def test_sms_phone_normalization_uses_local_myanmar_format(self):
        self.assertEqual(normalize_sms_phone_number("+95 9 333-333-333"), "09333333333")
        self.assertEqual(normalize_sms_phone_number("959333333333"), "09333333333")
        self.assertEqual(normalize_sms_phone_number("09 333 333 333"), "09333333333")

    @patch("booking.tasks.send_booking_confirmation_sms_task.delay")
    @patch("booking.tasks.send_booking_confirmation_email_task.delay")
    def test_notification_dispatch_queues_email_and_sms_independently(
        self, email_delay, sms_delay,
    ):
        email_delay.side_effect = [
            type("Result", (), {"id": "guest-email-task"})(),
            type("Result", (), {"id": "hotel-email-task"})(),
        ]
        sms_delay.side_effect = [
            type("Result", (), {"id": "guest-sms-task"})(),
            type("Result", (), {"id": "hotel-sms-task"})(),
        ]

        result = queue_booking_confirmation_notifications("booking-id")

        self.assertEqual(
            email_delay.call_args_list,
            [call("booking-id", "guest"), call("booking-id", "hotel")],
        )
        self.assertEqual(
            sms_delay.call_args_list,
            [
                call("booking-id", "guest"),
                call("booking-id", "hotel"),
            ],
        )
        self.assertEqual(result["guest_email_task_id"], "guest-email-task")
        self.assertEqual(result["hotel_email_task_id"], "hotel-email-task")
