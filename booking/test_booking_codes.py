from datetime import date

from django.test import TestCase

from booking.models import (
    BOOKING_CODES_PER_SERIES,
    Booking,
    BookingCodeSequence,
    Hotel,
    format_booking_code,
)
from booking.serializers import BookingSerializer


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

    def test_code_is_allocated_and_serialized(self):
        booking = self.create_booking("TEST-CODE-1")

        self.assertEqual(booking.booking_code, "V77H-A00000001")
        self.assertEqual(BookingSerializer(booking).data["booking_code"], booking.booking_code)

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
