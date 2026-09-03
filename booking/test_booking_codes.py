from datetime import date
from unittest.mock import patch

from django.test import TestCase, override_settings

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
    Payment,
    ReceiptNumberSequence,
    RatePlan,
    RoomType,
    format_booking_code,
    format_invoice_number,
    format_receipt_number,
)
from booking.booking_services.email import send_booking_confirmation_email
from booking.booking_services.receipt import ensure_receipt_pdf
from booking.serializers import BookingSerializer, InvoiceSerializer
from booking.services import record_payment
from booking.tasks import send_booking_confirmation_sms_task


class BookingCodeTests(TestCase):
    def setUp(self):
        self.hotel = Hotel.objects.create(core_business_id=987654321, name="Code Test Hotel")

    def create_booking(self, reference):
        return Booking.objects.create(
            reference=reference,
            hotel=self.hotel,
            check_in=date(2026, 9, 1),
            check_out=date(2026, 9, 2),
            contact_name="Test Guest",
            contact_phone="09123456789",
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

        self.assertEqual(booking.booking_code, "V77H-A00000001")
        self.assertEqual(BookingSerializer(booking).data["booking_code"], booking.booking_code)

        invoice = Invoice.objects.create(
            booking=booking,
            invoice_number="TEST-INVOICE-1",
            currency="MMK",
        )
        invoice_data = InvoiceSerializer(invoice).data
        self.assertEqual(invoice_data["booking_code"], booking.booking_code)
        self.assertEqual(invoice_data["invoice_details"]["booking_code"], booking.booking_code)

    def test_codes_increment(self):
        first = self.create_booking("TEST-CODE-1")
        second = self.create_booking("TEST-CODE-2")

        self.assertEqual(first.booking_code, "V77H-A00000001")
        self.assertEqual(second.booking_code, "V77H-A00000002")

    def test_invoice_and_receipt_numbers_increment_independently(self):
        booking = self.create_booking("TEST-DOCUMENT-CODES")
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

        self.assertEqual(first_invoice.invoice_number, "V77-INV-A0000001")
        self.assertEqual(second_invoice.invoice_number, "V77-INV-A0000002")
        self.assertEqual(first_receipt.receipt_number, "V77-REC-A0000001")
        self.assertEqual(second_receipt.receipt_number, "V77-REC-A0000002")

    def test_document_number_series_rolls_over(self):
        self.assertEqual(format_invoice_number(DOCUMENT_CODES_PER_SERIES), "V77-INV-A9999999")
        self.assertEqual(format_invoice_number(DOCUMENT_CODES_PER_SERIES + 1), "V77-INV-B0000001")
        self.assertEqual(format_receipt_number(DOCUMENT_CODES_PER_SERIES + 1), "V77-REC-B0000001")

        booking = self.create_booking("TEST-DOCUMENT-ROLLOVER")
        InvoiceNumberSequence.objects.update_or_create(
            pk=1, defaults={"last_value": DOCUMENT_CODES_PER_SERIES}
        )
        invoice = Invoice.objects.create(booking=booking, currency="MMK")
        self.assertEqual(invoice.invoice_number, "V77-INV-B0000001")

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
        self.assertEqual(receipt.receipt_number, "V77-REC-B0000001")

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
        self.assertEqual(payment.receipt_number, "V77-REC-A0000001")

    def test_pms_payment_also_creates_a_receipt(self):
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

        self.assertEqual(payment.receipt_number, "V77-REC-A0000001")
        self.assertEqual(payment.receipt_snapshot["booking"]["booking_code"], booking.booking_code)
        self.assertEqual(payment.receipt_snapshot["provider"], Payment.Provider.CASH)

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

        self.assertEqual(payment.receipt_snapshot["booking"]["booking_code"], booking.booking_code)
        self.assertEqual(payment.receipt_snapshot["amount_paid"], "1000.00")
        self.assertFalse(payment.receipt_pdf)

        receipt_url = (
            f"/api/v1/public/bookings/{booking.public_token}/"
            f"receipts/{payment.id}/pdf/"
        )
        response = self.client.get(receipt_url)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "application/pdf")
        self.assertTrue(b"".join(response.streaming_content).startswith(b"%PDF"))
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
            email_class.return_value.attach.assert_called_once()
            attachment = email_class.return_value.attach.call_args.args
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
        self.assertEqual(booking.booking_code, "V77H-B00000001")

    @override_settings(BOOKING_FRONTEND_URL="https://booking.example.com")
    @patch("booking.booking_services.email.EmailMultiAlternatives")
    def test_confirmation_email_uses_booking_code(self, email_class_mock):
        booking = self.create_booking("INTERNAL-EMAIL-REFERENCE")
        self.hotel.phone = '["012312", "123123"]'
        self.hotel.save(update_fields=["phone"])
        self.add_confirmation_rooms(booking)
        Guest.objects.create(
            booking=booking,
            name="Email Guest",
            email="guest@example.com",
            is_primary=True,
        )

        send_booking_confirmation_email(booking)
        email_class_mock.assert_called_once()
        email_class_mock.return_value.send.assert_called_once_with(fail_silently=False)
        email_class_mock.return_value.attach_alternative.assert_called_once()
        message = email_class_mock.call_args.kwargs["body"]
        html_message = email_class_mock.return_value.attach_alternative.call_args.args[0]
        self.assertIn(f"Booking ID: {booking.booking_code}", message)
        self.assertIn(booking.booking_code, html_message)
        self.assertIn(f"/bookings/{booking.public_token}", html_message)
        self.assertIn('href="tel:012312"', html_message)
        self.assertIn('href="tel:123123"', html_message)
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
