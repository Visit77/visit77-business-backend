from datetime import date
from io import StringIO

from django.core.management import call_command
from django.test import TestCase

from booking.models import Booking, BookingRoom, Hotel, OTARemittance, Payment, RatePlan, RoomType


class SeedDemoRemittancesTests(TestCase):
    def test_preview_and_apply_use_existing_rooms_and_skip_occupied_periods(self):
        hotel = Hotel.objects.create(core_business_id=7475, name="Test Hotel", package="ota")
        rooms = []
        for index in range(2):
            room = RoomType.objects.create(
                hotel=hotel, core_room_type_id=100 + index, name=f"Room {index}", room_price_setup=True,
            )
            rooms.append(room)
            RatePlan.objects.create(room_type=room, code="local", name="Local", default_price=100000)

        occupied = OTARemittance.objects.create(
            hotel=hotel, period_from=date(2026, 11, 4), period_to=date(2026, 11, 5),
            status=OTARemittance.Status.PAID, net_remittance_amount=123,
        )
        options = dict(business_id=7475, as_of=date(2026, 11, 6), stdout=StringIO())
        call_command("seed_demo_remittances", **options)
        self.assertEqual(Booking.objects.count(), 0)
        self.assertIn("Preview only", options["stdout"].getvalue())

        call_command("seed_demo_remittances", apply=True, **options)
        self.assertEqual(Booking.objects.filter(status="checked_out").count(), 10)
        self.assertEqual(Payment.objects.filter(provider="demo", status="paid").count(), 10)
        self.assertEqual(set(BookingRoom.objects.values_list("room_type_id", flat=True)), {room.id for room in rooms})
        statements = OTARemittance.objects.exclude(pk=occupied.pk).order_by("-period_to")
        self.assertEqual(statements.count(), 5)
        self.assertEqual(statements.first().period_to, date(2026, 11, 3))
        for statement in statements:
            self.assertEqual(statement.status, OTARemittance.Status.PENDING)
            self.assertEqual(statement.items.count(), 2)
            self.assertEqual(statement.net_remittance_amount, 190000)
        occupied.refresh_from_db()
        self.assertEqual(occupied.net_remittance_amount, 123)

        call_command("seed_demo_remittances", apply=True, **options)
        self.assertEqual(Booking.objects.count(), 10)
        self.assertEqual(statements.count(), 5)
