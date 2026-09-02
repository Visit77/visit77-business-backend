from datetime import date
from unittest.mock import patch

from django.test import TestCase, override_settings

from booking.models import (
    BOOKING_CODES_PER_SERIES,
    Booking,
    BookingRoom,
    BookingCodeSequence,
    Guest,
    Hotel,
    Invoice,
    RatePlan,
    RoomType,
    format_booking_code,
)
from booking.booking_services.email import send_booking_confirmation_email
from booking.serializers import BookingSerializer, InvoiceSerializer
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

    def test_series_rolls_over_at_ten_million(self):
        self.assertEqual(format_booking_code(BOOKING_CODES_PER_SERIES), "V77H-A09999999")
        self.assertEqual(format_booking_code(BOOKING_CODES_PER_SERIES + 1), "V77H-B00000001")

        BookingCodeSequence.objects.update_or_create(
            pk=1, defaults={"last_value": BOOKING_CODES_PER_SERIES}
        )
        booking = self.create_booking("TEST-CODE-ROLLOVER")
        self.assertEqual(booking.booking_code, "V77H-B00000001")

    @override_settings(BOOKING_FRONTEND_URL="https://booking.example.com")
    @patch("booking.booking_services.email.send_mail")
    def test_confirmation_email_uses_booking_code(self, send_mail_mock):
        booking = self.create_booking("INTERNAL-EMAIL-REFERENCE")
        self.add_confirmation_rooms(booking)
        Guest.objects.create(
            booking=booking,
            name="Email Guest",
            email="guest@example.com",
            is_primary=True,
        )

        send_booking_confirmation_email(booking)
        send_mail_mock.assert_called_once()
        message = send_mail_mock.call_args.kwargs["message"]
        self.assertIn(f"Booking ID: {booking.booking_code}", message)
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
