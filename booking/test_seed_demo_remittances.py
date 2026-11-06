from datetime import date
from io import BytesIO, StringIO
from types import SimpleNamespace
from zipfile import ZipFile

from django.core.management import call_command
from django.test import TestCase, override_settings
from rest_framework.test import APIClient

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

        record = statements.first()
        client = APIClient()
        url = f"/api/v1/admin/remittances/{record.id}/export/"
        with override_settings(BOOKING_ADMIN_API_KEY="test-key"):
            client.credentials(HTTP_X_BOOKING_ADMIN_KEY="test-key", HTTP_X_BOOKING_BUSINESS_ID="7475")
            hotel_export = client.get(url)
            self.assertEqual(hotel_export.status_code, 200)
            hotel_bytes = b"".join(hotel_export.streaming_content)
            other_hotel = Hotel.objects.create(core_business_id=7476, name="Other")
            client.credentials(HTTP_X_BOOKING_ADMIN_KEY="test-key",
                               HTTP_X_BOOKING_BUSINESS_ID=str(other_hotel.core_business_id))
            self.assertEqual(client.get(url).status_code, 400)

        client.credentials()
        client.force_authenticate(user=SimpleNamespace(is_authenticated=True, is_superuser=True))
        admin_export = client.get(f"/api/v1/superadmin/remittances/{record.id}/export/")
        self.assertEqual(admin_export.status_code, 200)
        for content in (hotel_bytes, b"".join(admin_export.streaming_content)):
            with ZipFile(BytesIO(content)) as workbook:
                sheet = workbook.read("xl/worksheets/sheet1.xml").decode()
                self.assertIn("Remittance Details - " + record.reference, sheet)
                self.assertIn("Booking Collection", sheet)
                self.assertIn('r="G3"><v>100000.00</v>', sheet)
