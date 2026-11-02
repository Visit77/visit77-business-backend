from datetime import timedelta
from decimal import Decimal
from importlib import import_module
from types import SimpleNamespace

from django.apps import apps
from django.db import connection
from django.template.loader import render_to_string
from django.test import TestCase, override_settings
from django.utils import timezone
from rest_framework.exceptions import ValidationError
from rest_framework.test import APIClient

from booking.integrations.core import sync_business_from_core
from booking.models import Booking, DailyRate, Hotel, RatePlan, RoomPriceDiscount, RoomType
from booking.room_pricing_serializers import PRICE_FIELDS, RoomPriceDiscountSerializer, RoomPriceSetupSerializer
from booking.services import _rate_amounts, availability_for_hotel, create_booking, ensure_daily_inventory_for_room_type, estimate_booking


class RoomPricingTests(TestCase):
    def setUp(self):
        self.hotel = Hotel.objects.create(core_business_id=77, name="Hotel", package="ota_pms", inventory_mode="room_type_count")
        self.room_type = RoomType.objects.create(hotel=self.hotel, core_room_type_id=301, name="Room", default_inventory=5,
            room_type_count_inventory=5, room_price_setup=True, local_mmk_price=100000,
            local_usd_price=100, foreign_mmk_price=200000, foreign_usd_price=200)
        self.plan = RatePlan.objects.create(room_type=self.room_type, code="local", name="Local", guest_market="local",
            is_default=True, default_price=100000, usd_display_price=100)
        self.start = timezone.localdate() + timedelta(days=10)
        self.end = self.start + timedelta(days=2)
        self.payload = {"core_business_id": 77, "check_in": self.start, "check_out": self.end,
            "contact_name": "Guest", "contact_phone": "091111111", "guest_market": "local",
            "rooms": [{"core_room_type_id": 301, "rate_plan_id": self.plan.id, "quantity": 1, "adults": 1, "children": 0, "extra_beds": 0}]}

    def discount(self, **changes):
        data = {"timing_mode": "scheduled", "start_date": self.start, "end_date": self.start,
                "local_mmk_price": "80000", **changes}
        serializer = RoomPriceDiscountSerializer(data=data, context={"room_type": self.room_type})
        self.assertTrue(serializer.is_valid(), serializer.errors)
        return serializer.save()

    def test_all_four_prices_store_calculated_percentage(self):
        discount = self.discount(local_usd_price="80", foreign_mmk_price="160000", foreign_usd_price="160")
        discount.refresh_from_db()
        for field in PRICE_FIELDS:
            self.assertEqual(getattr(discount, field.replace("_price", "_percentage")), Decimal("20"))

    def test_manual_starts_today_ignores_dates_and_can_end(self):
        discount = self.discount(timing_mode="manual", start_date=self.end, end_date=self.end)
        self.assertEqual(discount.start_date, timezone.localdate())
        self.assertIsNone(discount.end_date)
        self.assertEqual(RoomPriceDiscountSerializer(discount).data["status"], "active")
        discount.ended_at = timezone.now()
        discount.save()
        self.assertEqual(RoomPriceDiscountSerializer(discount).data["status"], "ended")

    def test_discount_requires_one_valid_price_and_scheduled_dates(self):
        for data in (
            {"timing_mode": "manual"},
            {"timing_mode": "manual", "local_mmk_price": "100000"},
            {"timing_mode": "manual", "local_mmk_price": "-1"},
            {"timing_mode": "scheduled", "local_mmk_price": "80000"},
            {"timing_mode": "scheduled", "local_mmk_price": "80000", "start_date": self.end, "end_date": self.start},
        ):
            with self.subTest(data=data):
                serializer = RoomPriceDiscountSerializer(data=data, context={"room_type": self.room_type})
                self.assertFalse(serializer.is_valid())
        self.room_type.room_price_setup = False
        serializer = RoomPriceDiscountSerializer(data={"timing_mode": "manual", "local_mmk_price": "80000"}, context={"room_type": self.room_type})
        self.assertFalse(serializer.is_valid())

    def test_each_currency_can_be_the_only_supplied_discount(self):
        for field, amount in zip(PRICE_FIELDS, (80000, 80, 160000, 160)):
            serializer = RoomPriceDiscountSerializer(data={"timing_mode": "manual", field: amount}, context={"room_type": self.room_type})
            self.assertTrue(serializer.is_valid(), serializer.errors)

    def test_unconfigured_rooms_are_hidden_and_rejected(self):
        self.room_type.room_price_setup = False
        self.room_type.save()
        ensure_daily_inventory_for_room_type(self.room_type, self.start, days=2)
        self.assertEqual(availability_for_hotel(self.hotel, self.start, self.end), [])
        for operation in (estimate_booking, create_booking):
            with self.assertRaises(ValidationError):
                operation(self.payload)
        self.assertFalse(Booking.objects.exists())

    def test_quote_booking_invoice_and_email_use_net_room_amount(self):
        from booking.booking_services.email import build_booking_confirmation_context
        discount = self.discount()
        ensure_daily_inventory_for_room_type(self.room_type, self.start, days=2)
        available = availability_for_hotel(self.hotel, self.start, self.end)
        self.assertEqual(available[0]["rate_plans"][0]["total"], Decimal("180000"))
        self.assertEqual(available[0]["default_price"]["base_price"], Decimal("80000"))
        self.assertEqual(estimate_booking(self.payload)["room_total"], Decimal("180000"))
        booking, _ = create_booking(self.payload)
        self.assertEqual(list(booking.rooms.get().nights.order_by("stay_date").values_list("unit_price", flat=True)), [Decimal("80000"), Decimal("100000")])
        invoice = booking.invoices.first()
        self.assertEqual(invoice.discount_total, 0)
        self.assertEqual(invoice.lines.get(metadata__line_type="room").total, Decimal("180000"))
        from booking.booking_services.receipt import build_invoice_snapshot, build_receipt_snapshot
        from booking.services import record_payment
        snapshot = build_invoice_snapshot(invoice)
        self.assertEqual(Decimal(snapshot["invoice"]["room_charge_total"]), Decimal("180000"))
        payment = record_payment(booking, {"provider": "cash", "amount": Decimal("180000"), "status": "paid"})
        receipt = build_receipt_snapshot(payment)
        self.assertEqual(Decimal(receipt["invoice"]["room_charge_total"]), Decimal("180000"))
        context = build_booking_confirmation_context(booking, SimpleNamespace(name="Guest"))
        self.assertNotIn("Discount", render_to_string("emails/booking_confirmation.html", context))
        discount.ended_at = timezone.now()
        discount.save()
        self.assertEqual(estimate_booking(self.payload)["room_total"], Decimal("200000"))
        self.assertEqual(booking.rooms.get().nights.get(stay_date=self.start).unit_price, Decimal("80000"))

    def test_campaign_boundaries_markets_overrides_and_latest_currency(self):
        self.discount(local_usd_price="80")
        self.discount(local_mmk_price="70000")
        discounts = list(self.room_type.price_discounts.all())
        self.assertEqual(_rate_amounts(None, self.plan, self.start, "local", discounts), (Decimal("70000"), Decimal("80")))
        for day, market in ((self.start - timedelta(days=1), "local"), (self.start + timedelta(days=1), "local"), (self.start, "foreign")):
            self.assertEqual(_rate_amounts(None, self.plan, day, market, discounts)[0], Decimal("100000"))
        rule = DailyRate.objects.create(rate_plan=self.plan, stay_date=self.start, price=120000)
        self.assertEqual(_rate_amounts(rule, self.plan, self.start, "local", discounts)[0], Decimal("70000"))
        rule.base_price = Decimal("60000")
        self.assertEqual(_rate_amounts(rule, self.plan, self.start, "local", discounts)[0], Decimal("60000"))

    @override_settings(BOOKING_ADMIN_API_KEY="key")
    def test_api_setup_discount_end_and_business_scope(self):
        client = APIClient()
        client.credentials(HTTP_X_BOOKING_ADMIN_KEY="key", HTTP_X_BOOKING_BUSINESS_ID="77")
        url = f"/api/v1/admin/room-types/{self.room_type.id}"
        response = client.patch(url + "/room-price/", {"local_mmk_price": "120000"}, format="json")
        self.assertEqual(response.status_code, 200, response.data)
        self.plan.refresh_from_db()
        self.assertEqual(self.plan.base_price, Decimal("120000"))
        response = client.post(url + "/discounts/", {"timing_mode": "manual", "local_mmk_price": "96000"}, format="json")
        self.assertEqual(response.data["status_code"], 201, response.data)
        discount_id = response.data["data"]["id"]
        for _ in range(2):
            response = client.post(url + f"/discounts/{discount_id}/end/", {}, format="json")
            self.assertEqual(response.data["data"]["status"], "ended")
        self.assertEqual(client.post("/api/v1/admin/room-types/", {}, format="json").status_code, 405)
        client.credentials(HTTP_X_BOOKING_ADMIN_KEY="key", HTTP_X_BOOKING_BUSINESS_ID="999")
        self.assertEqual(client.get(url + "/discounts/").status_code, 404)

    def test_setup_requires_at_least_one_amount(self):
        self.room_type.local_mmk_price = self.room_type.local_usd_price = None
        self.room_type.foreign_mmk_price = self.room_type.foreign_usd_price = None
        serializer = RoomPriceSetupSerializer(data={}, context={"room_type": self.room_type})
        self.assertFalse(serializer.is_valid())
        for field in PRICE_FIELDS:
            serializer = RoomPriceSetupSerializer(data={field: "100"}, context={"room_type": self.room_type})
            self.assertTrue(serializer.is_valid(), serializer.errors)

    def test_display_only_price_does_not_publish_a_free_base_currency_plan(self):
        from booking.room_pricing import sync_market_price
        self.room_type.local_mmk_price = None
        plan = sync_market_price(self.room_type, "local")
        self.assertFalse(plan.is_active)
        self.assertEqual(plan.usd_display_price, Decimal("100"))
        self.assertTrue(self.room_type.room_price_setup)

    @override_settings(BOOKING_INVENTORY_WINDOW_DAYS=2)
    def test_core_sync_creates_unpriced_room_and_preserves_configured_prices(self):
        class Client:
            def provisioning_bundle(client, business_id):
                return {"business": {"id": business_id, "name": "Hotel"}, "access": {"status": "active", "package": "ota_pms"},
                    "room_types": [{"id": 301, "name": "Updated", "rate_plans": [{"code": "core-local", "guest_market": "local", "base_price": 1}]},
                                   {"id": 999, "name": "Unpriced"}]}
        sync_business_from_core(77, client=Client())
        self.room_type.refresh_from_db()
        self.assertEqual(self.room_type.local_mmk_price, Decimal("100000"))
        self.assertEqual(self.room_type.rate_plans.get(code="core-local").base_price, Decimal("100000"))
        self.assertFalse(RoomType.objects.get(core_room_type_id=999).room_price_setup)

    def test_migration_backfills_legacy_prices(self):
        self.room_type.room_price_setup = False
        self.room_type.local_mmk_price = None
        self.room_type.save()
        migration = import_module("booking.migrations.0076_room_price_setup_and_discounts")
        migration.backfill_room_prices(apps, SimpleNamespace(connection=connection))
        self.room_type.refresh_from_db()
        self.assertTrue(self.room_type.room_price_setup)
        self.assertEqual(self.room_type.local_mmk_price, Decimal("100000"))

    def guest_availability(self, market="local", currency="MMK"):
        from booking.services import availability_for_hotel_with_display
        ensure_daily_inventory_for_room_type(self.room_type, self.start, days=2)
        return availability_for_hotel_with_display(
            self.hotel, self.start, self.end, guest_market=market, display_currency=currency,
        )[0]

    def test_guest_promo_fields_match_each_night_and_first_night_card(self):
        self.discount()
        room = self.guest_availability()
        expected = {"has_discount": True, "original_price": Decimal("100000"),
                    "discount_percentage": Decimal("20"), "discount_price": Decimal("80000")}
        for payload in (room["default_price"], room["rate_plans"][0], room["rate_plans"][0]["nightly_prices"][0]):
            for field, value in expected.items():
                self.assertEqual(payload[field], value)
        self.assertEqual(room["default_price"]["display_currency"], "MMK")
        for field in ("has_discount", "original_price", "discount_percentage", "discount_price", "price_currency", "price_date"):
            self.assertNotIn(field, room)
        second = room["rate_plans"][0]["nightly_prices"][1]
        self.assertFalse(second["has_discount"])
        self.assertIsNone(second["discount_price"])
        self.assertEqual(second["discount_percentage"], 0)
        self.assertEqual(second["original_price"], Decimal("100000"))
        self.assertEqual(room["rate_plans"][0]["total"], Decimal("180000"))

    def test_guest_without_campaign_has_no_promo_fields_or_badge(self):
        room = self.guest_availability()
        self.assertFalse(room["default_price"]["has_discount"])
        self.assertIsNone(room["default_price"]["discount_price"])
        self.assertEqual(room["default_price"]["original_price"], Decimal("100000"))
        self.assertEqual(room["default_price"]["discount_percentage"], 0)
        # A lower DailyRate alone is not a campaign discount.
        DailyRate.objects.create(rate_plan=self.plan, stay_date=self.start, price=60000)
        room = self.guest_availability()
        self.assertFalse(room["default_price"]["has_discount"])
        self.assertEqual(room["default_price"]["original_price"], Decimal("60000"))

    def test_guest_promo_uses_selected_currency_and_market(self):
        self.discount(local_usd_price="90", foreign_usd_price="160", foreign_mmk_price="150000")
        room = self.guest_availability(currency="USD")
        self.assertEqual(room["default_price"]["original_price"], Decimal("100"))
        self.assertEqual(room["default_price"]["discount_price"], Decimal("90"))
        self.assertEqual(room["default_price"]["discount_percentage"], Decimal("10"))
        RatePlan.objects.create(room_type=self.room_type, code="foreign", name="Foreign", guest_market="foreign",
                               is_default=True, default_price=200000, usd_display_price=200)
        room = self.guest_availability(market="foreign", currency="USD")
        self.assertEqual(room["default_price"]["original_price"], Decimal("200"))
        self.assertEqual(room["default_price"]["discount_price"], Decimal("160"))
        self.assertEqual(room["default_price"]["discount_percentage"], Decimal("20"))

    def test_guest_promo_excludes_ended_future_and_cheaper_rates(self):
        discount = self.discount()
        DailyRate.objects.create(rate_plan=self.plan, stay_date=self.start, price=70000)
        room = self.guest_availability()
        self.assertFalse(room["default_price"]["has_discount"])
        self.assertEqual(room["default_price"]["original_price"], Decimal("70000"))
        discount.ended_at = timezone.now()
        discount.save()
        self.assertFalse(self.guest_availability()["default_price"]["has_discount"])
        self.discount(start_date=self.start + timedelta(days=1), end_date=self.end)
        room = self.guest_availability()
        self.assertFalse(room["default_price"]["has_discount"])
        self.assertTrue(room["rate_plans"][0]["nightly_prices"][1]["has_discount"])

    def test_guest_percentage_matches_actual_rate_before_campaign(self):
        self.discount()
        DailyRate.objects.create(rate_plan=self.plan, stay_date=self.start, price=120000)
        room = self.guest_availability()
        self.assertEqual(room["default_price"]["original_price"], Decimal("120000"))
        self.assertEqual(room["default_price"]["discount_price"], Decimal("80000"))
        self.assertEqual(room["default_price"]["discount_percentage"], Decimal("33.33"))

    def test_public_availability_serializes_guest_promo_fields(self):
        self.discount()
        ensure_daily_inventory_for_room_type(self.room_type, self.start, days=2)
        response = APIClient().get("/api/v1/public/hotels/77/availability/", {
            "check_in": str(self.start), "check_out": str(self.end), "adults": 1,
            "guest_market": "local", "display_currency": "MMK",
        })
        self.assertEqual(response.status_code, 200, response.data)
        room = response.json()["data"]["room_types"][0]
        self.assertTrue(room["default_price"]["has_discount"])
        self.assertEqual(room["default_price"]["original_price"], 100000)
        self.assertEqual(room["default_price"]["discount_percentage"], 20)
        self.assertEqual(room["default_price"]["discount_price"], 80000)
        self.assertIsNone(room["rate_plans"][0]["nightly_prices"][1]["discount_price"])
