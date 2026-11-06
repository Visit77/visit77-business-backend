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

    @override_settings(BOOKING_ADMIN_API_KEY='key')
    def test_extra_bed_price_setup_and_scoping(self):
        url = f'/api/v1/admin/room-types/{self.room_type.id}/extra-bed-price/'
        response = self.admin_client().put(url, {
            'extra_bed_local_base_price': '20000', 'extra_bed_local_usd_price': '10',
            'extra_bed_foreign_base_price': '40000', 'extra_bed_foreign_usd_price': '20',
        }, format='json')
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data['data']['base_currency'], 'MMK')
        self.assertEqual(response.data['data']['extra_bed_local_base_price'], 20000)
        response = self.admin_client().patch(url, {'extra_bed_foreign_base_price': None}, format='json')
        self.assertEqual(response.status_code, 200, response.data)
        self.assertIsNone(response.data['data']['extra_bed_foreign_base_price'])
        self.room_type.refresh_from_db()
        self.assertEqual(self.room_type.extra_bed_foreign_usd_price, 20)
        self.assertEqual(self.admin_client('999').patch(url, {'extra_bed_local_mmk_price': 1}, format='json').status_code, 404)
        self.assertEqual(self.admin_client().patch(url, {'extra_bed_local_mmk_price': -1}, format='json').status_code, 400)
        self.room_type.room_price_setup = False
        self.room_type.save()
        self.assertEqual(self.admin_client().patch(url, {'extra_bed_local_mmk_price': 1}, format='json').status_code, 400)

    def test_extra_bed_market_fallback_zero_and_display_currency(self):
        from booking.room_pricing import extra_bed_amounts, extra_bed_price_payload
        self.room_type.extra_bed_local_mmk_price = Decimal('20000')
        self.room_type.extra_bed_foreign_usd_price = Decimal('20')
        self.assertEqual(extra_bed_amounts(self.room_type, 'foreign'), (Decimal('20000'), Decimal('20')))
        self.assertEqual(extra_bed_price_payload(self.room_type, 'local', 'USD')['display_currency'], 'MMK')
        self.room_type.extra_bed_foreign_mmk_price = Decimal('0')
        self.assertEqual(extra_bed_amounts(self.room_type, 'foreign')[0], 0)
        self.room_type.extra_bed_local_mmk_price = None
        self.assertEqual(extra_bed_amounts(self.room_type, 'local'), (Decimal('0'), None))

    def test_extra_bed_estimate_booking_and_invoice_use_room_type_price(self):
        self.room_type.extra_bed_local_mmk_price = Decimal('20000')
        self.room_type.extra_bed_local_usd_price = Decimal('10')
        self.payload['display_currency'] = 'USD'
        self.room_type.core_snapshot = {'extra_bed_available': True, 'extra_bed_quantity': 2}
        self.room_type.save()
        self.plan.extra_bed_base_price = 999
        self.plan.save()
        self.payload['rooms'][0]['extra_beds'] = 2
        ensure_daily_inventory_for_room_type(self.room_type, self.start, days=2)
        estimate = estimate_booking(self.payload)
        row = next(item for item in estimate['summary_items'] if item['type'] == 'extra_bed')
        self.assertEqual(row['amount'], Decimal('80000'))
        self.assertEqual(row['display_amount'], Decimal('80000'))
        self.assertEqual(row['display_currency'], 'MMK')
        booking, _ = create_booking(self.payload)
        room = booking.rooms.first()
        self.assertEqual(room.rate_plan_snapshot['extra_bed_base_price'], '20000.00')
        self.assertEqual(sum(n.extra_bed_total for n in room.nights.all()), Decimal('80000'))
        from booking.services import ensure_initial_invoice
        invoice = ensure_initial_invoice(booking)
        self.assertEqual(sum(line.total for line in invoice.lines.all() if line.metadata.get('line_type') == 'extra_bed'), Decimal('80000'))

    def test_extra_bed_migration_backfills_existing_default_rates(self):
        migration = import_module('booking.migrations.0078_room_type_extra_bed_prices')
        self.plan.extra_bed_base_price = 20000
        self.plan.extra_bed_usd_display_price = 10
        self.plan.save()
        migration.backfill_extra_bed_prices(apps, SimpleNamespace(connection=connection))
        self.room_type.refresh_from_db()
        self.assertEqual(self.room_type.extra_bed_local_mmk_price, 20000)
        self.assertEqual(self.room_type.extra_bed_local_usd_price, 10)

    def test_extra_bed_public_catalog_and_availability_label_fallback_currency(self):
        self.room_type.extra_bed_local_mmk_price = 20000
        self.room_type.save()
        room = self.guest_availability(currency='USD')
        self.assertEqual(room['default_price']['display_currency'], 'USD')
        self.assertEqual(room['extra_bed_price']['display_currency'], 'MMK')
        self.assertEqual(room['extra_bed_price']['display_price'], 20000)
        response = APIClient().get('/api/v1/public/room-types/', {
            'business_id': 77, 'guest_market': 'local', 'display_currency': 'USD',
        })
        plan = response.data['data']['room_types'][0]['rate_plans'][0]
        self.assertEqual(plan['extra_bed_display_currency'], 'MMK')
        self.assertEqual(plan['extra_bed_display_price'], 20000)

    def test_alternate_currency_only_extra_bed_cannot_silently_become_free(self):
        from booking.room_pricing import validate_extra_bed_settlement
        self.room_type.extra_bed_local_usd_price = Decimal('10')
        with self.assertRaises(ValidationError):
            validate_extra_bed_settlement(self.room_type, 'local', 1)
        validate_extra_bed_settlement(self.room_type, 'local', 0)

    def test_usd_base_hotel_cannot_charge_an_mmk_extra_bed_as_usd(self):
        from booking.room_pricing import extra_bed_price_payload, validate_extra_bed_settlement
        self.hotel.base_currency = 'USD'
        self.room_type.extra_bed_local_mmk_price = Decimal('20000')
        self.room_type.extra_bed_local_usd_price = Decimal('10')
        price = extra_bed_price_payload(self.room_type, 'local', 'USD')
        self.assertEqual(price['base_currency'], 'MMK')
        self.assertEqual(price['base_price'], Decimal('20000'))
        self.assertEqual(price['usd_display_price'], Decimal('10'))
        with self.assertRaises(ValidationError):
            validate_extra_bed_settlement(self.room_type, 'local', 1)

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

    def test_availability_usd_promo_is_independent_of_selected_currency(self):
        self.discount(local_usd_price='90')
        room = self.guest_availability(currency='MMK')
        plan = room['rate_plans'][0]
        for payload in (room['default_price'], plan, plan['nightly_prices'][0]):
            self.assertTrue(payload['usd_has_discount'])
            self.assertEqual(payload['usd_original_price'], 100)
            self.assertEqual(payload['usd_discount_price'], 90)
            self.assertEqual(payload['usd_discount_percentage'], 10)
        second = plan['nightly_prices'][1]
        self.assertFalse(second['usd_has_discount'])
        self.assertIsNone(second['usd_discount_price'])

    def test_availability_unconfigured_usd_has_no_discount(self):
        self.plan.usd_display_price = None
        self.plan.save()
        plan = self.guest_availability()['rate_plans'][0]
        self.assertFalse(plan['usd_has_discount'])
        self.assertIsNone(plan['usd_original_price'])
        self.assertEqual(plan['usd_discount_percentage'], 0)
        self.assertIsNone(plan['usd_discount_price'])

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

    def test_guest_promo_keeps_base_and_usd_separate_when_displaying_usd(self):
        self.discount(local_usd_price="90", foreign_usd_price="160", foreign_mmk_price="150000")
        room = self.guest_availability(currency="USD")
        for payload in (room['default_price'], room['default_rate_plan'], room['rate_plans'][0], room['rate_plans'][0]['nightly_prices'][0]):
            self.assertEqual(payload['original_price'], Decimal('100000'))
            self.assertEqual(payload['discount_price'], Decimal('80000'))
            self.assertEqual(payload['discount_percentage'], Decimal('20'))
            self.assertEqual(payload['usd_original_price'], Decimal('100'))
            self.assertEqual(payload['usd_discount_price'], Decimal('90'))
            self.assertEqual(payload['usd_discount_percentage'], Decimal('10'))
        self.assertEqual(room['default_price']['display_price'], Decimal('90'))
        RatePlan.objects.create(room_type=self.room_type, code="foreign", name="Foreign", guest_market="foreign",
                               is_default=True, default_price=200000, usd_display_price=200)
        room = self.guest_availability(market="foreign", currency="USD")
        self.assertEqual(room["default_price"]["original_price"], Decimal("200000"))
        self.assertEqual(room["default_price"]["discount_price"], Decimal("150000"))
        self.assertEqual(room["default_price"]["discount_percentage"], Decimal("25"))
        self.assertEqual(room['default_price']['usd_discount_price'], Decimal('160'))
        self.assertEqual(room['default_price']['usd_discount_percentage'], Decimal('20'))

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

    def admin_client(self, business_id="77"):
        client = APIClient()
        client.credentials(HTTP_X_BOOKING_ADMIN_KEY="key", HTTP_X_BOOKING_BUSINESS_ID=business_id)
        return client

    @override_settings(BOOKING_ADMIN_API_KEY="key")
    def test_admin_room_list_detail_and_price_setup_include_discounts(self):
        discount = self.discount(timing_mode="manual", local_usd_price="20")
        client = self.admin_client()
        root = f"/api/v1/admin/room-types/{self.room_type.id}"
        for url in (root + "/", root + "/room-price/"):
            response = client.get(url)
            self.assertEqual(response.status_code, 200, response.data)
            row = response.data["data"]["discounts"][0]
            self.assertEqual(row["id"], discount.id)
            self.assertEqual(row["status"], "active")
            self.assertEqual(Decimal(str(row["local_mmk_price"])), Decimal("80000"))
            self.assertEqual(Decimal(str(row["local_mmk_percentage"])), Decimal("20"))
        response = client.get("/api/v1/admin/room-types/")
        rows = response.data["data"]
        if isinstance(rows, dict):
            rows = rows["results"]
        self.assertEqual(rows[0]["discounts"][0]["id"], discount.id)
        # No campaigns is an explicit empty array for rendering Add Discount.
        discount.delete()
        self.assertEqual(client.get(root + "/").data["data"]["discounts"], [])

    @override_settings(BOOKING_ADMIN_API_KEY="key")
    def test_admin_can_edit_discount_amounts_and_recalculate_percentage(self):
        discount = self.discount(timing_mode="manual", local_usd_price="20")
        discount.start_date = timezone.localdate() - timedelta(days=2)
        discount.save(update_fields=["start_date"])
        original_start = discount.start_date
        client = self.admin_client()
        url = f"/api/v1/admin/room-types/{self.room_type.id}/discounts/{discount.id}/"
        self.assertEqual(client.get(url).data["data"]["id"], discount.id)
        response = client.patch(url, {"local_mmk_price": "75000", "local_usd_price": None}, format="json")
        self.assertEqual(response.status_code, 200, response.data)
        discount.refresh_from_db()
        self.assertEqual(discount.local_mmk_price, Decimal("75000"))
        self.assertEqual(discount.local_mmk_percentage, Decimal("25"))
        self.assertIsNone(discount.local_usd_price)
        self.assertIsNone(discount.local_usd_percentage)
        self.assertEqual(discount.start_date, original_start)
        self.assertEqual(response.data["data"]["status"], "active")
        bad = client.patch(url, {"local_mmk_price": None}, format="json")
        self.assertEqual(bad.status_code, 400)
        discount.refresh_from_db()
        self.assertEqual(discount.local_mmk_price, Decimal("75000"))

    @override_settings(BOOKING_ADMIN_API_KEY="key")
    def test_edit_scheduled_dates_scope_and_ended_campaign(self):
        discount = self.discount()
        client = self.admin_client()
        root = f"/api/v1/admin/room-types/{self.room_type.id}"
        url = root + f"/discounts/{discount.id}/"
        response = client.patch(url, {"end_date": str(self.end), "local_mmk_price": "70000"}, format="json")
        self.assertEqual(response.status_code, 200, response.data)
        discount.refresh_from_db()
        self.assertEqual(discount.end_date, self.end)
        self.assertEqual(discount.local_mmk_percentage, Decimal("30"))
        self.assertEqual(client.patch(url, {"end_date": str(self.start - timedelta(days=1))}, format="json").status_code, 400)
        self.assertEqual(self.admin_client("999").get(url).status_code, 404)
        other = RoomType.objects.create(hotel=self.hotel, core_room_type_id=302, name="Other")
        other_url = f"/api/v1/admin/room-types/{other.id}/discounts/{discount.id}/"
        self.assertEqual(client.get(other_url).status_code, 404)
        client.post(root + f"/discounts/{discount.id}/end/", {}, format="json")
        self.assertEqual(client.patch(url, {"local_mmk_price": "60000"}, format="json").status_code, 400)
        self.assertEqual(client.get(root + "/").data["data"]["discounts"], [])
        self.assertTrue(RoomPriceDiscount.objects.filter(pk=discount.pk, ended_at__isnull=False).exists())

    @override_settings(BOOKING_ADMIN_API_KEY="key")
    def test_admin_room_list_prefetches_discounts_in_one_query(self):
        from django.test.utils import CaptureQueriesContext
        self.discount()
        other = RoomType.objects.create(hotel=self.hotel, core_room_type_id=302, name="Other")
        RoomPriceDiscount.objects.create(room_type=other, timing_mode="manual", start_date=timezone.localdate(), local_mmk_price=10)
        with CaptureQueriesContext(connection) as queries:
            response = self.admin_client().get("/api/v1/admin/room-types/")
            self.assertEqual(response.status_code, 200)
        discount_queries = [query for query in queries if 'FROM "booking_roompricediscount"' in query["sql"]]
        self.assertEqual(len(discount_queries), 1)

    def test_public_catalog_returns_discounts_in_default_price_and_both_markets(self):
        self.room_type.core_snapshot={'local_base_price':1,'foreign_base_price':2,'local_usd_display_price':0}
        self.room_type.save(update_fields=['core_snapshot'])
        self.discount(timing_mode='manual',local_usd_price='90',foreign_mmk_price='150000',foreign_usd_price='160')
        RatePlan.objects.create(room_type=self.room_type,code='foreign',name='Foreign',guest_market='foreign',is_default=True,default_price=200000,usd_display_price=200)
        for url in ('/api/v1/public/room-types/', '/api/v1/public/hotels/77/room-types/', '/api/v1/public/hotels/77/ota-room-types/'):
            response=APIClient().get(url,{'business_id':77,'guest_market':'local','display_currency':'USD'})
            self.assertEqual(response.status_code,200,response.data)
            room=response.json()['data']['room_types'][0]
            self.assertTrue(room['default_price']['has_discount'])
            self.assertEqual(room['default_price']['original_price'],100000)
            self.assertEqual(room['default_price']['discount_price'],80000)
            self.assertEqual(room['default_price']['discount_percentage'],20)
            for payload in (room['default_price'], room['rate_plans'][0]):
                self.assertEqual(payload['original_price'], 100000)
                self.assertEqual(payload['discount_price'], 80000)
                self.assertEqual(payload['discount_percentage'], 20)
                self.assertTrue(payload['usd_has_discount'])
                self.assertEqual(payload['usd_original_price'], 100)
                self.assertEqual(payload['usd_discount_price'], 90)
                self.assertEqual(payload['usd_discount_percentage'], 10)
            local=room['default_prices']['local']; foreign=room['default_prices']['foreign']
            self.assertEqual(local['base_price'],100000)
            self.assertEqual(local['discount_price'],80000)
            self.assertEqual(local['discount_percentage'],20)
            self.assertEqual(local['usd_display_price'],100)
            self.assertEqual(local['usd_discount_price'],90)
            self.assertEqual(local['usd_discount_percentage'],10)
            self.assertEqual(foreign['discount_price'],150000)
            self.assertEqual(foreign['discount_percentage'],25)
            self.assertEqual(foreign['usd_discount_price'],160)
            self.assertEqual(foreign['usd_discount_percentage'],20)

    def test_availability_default_prices_use_stay_date_for_both_markets(self):
        self.discount(foreign_mmk_price='150000')
        room=self.guest_availability()
        self.assertEqual(room['default_prices']['local']['discount_price'],Decimal('80000'))
        self.assertEqual(room['default_prices']['foreign']['discount_price'],Decimal('150000'))
        self.assertEqual(room['default_prices']['local']['price_date'],str(self.start))
        # A future campaign must not appear in today's catalogue.
        response=APIClient().get('/api/v1/public/room-types/',{'business_id':77,'guest_market':'local'})
        today=response.json()['data']['room_types'][0]
        self.assertFalse(today['default_price']['has_discount'])
        self.assertFalse(today['default_prices']['local']['has_discount'])
        self.assertIsNone(today['default_prices']['foreign']['discount_price'])

    def test_catalog_no_discount_ended_and_null_prices(self):
        self.room_type.local_usd_price=None
        self.room_type.foreign_mmk_price=None
        self.room_type.foreign_usd_price=None
        self.room_type.save()
        discount=self.discount(timing_mode='manual')
        discount.ended_at=timezone.now()
        discount.save()
        room=APIClient().get('/api/v1/public/room-types/',{'business_id':77}).json()['data']['room_types'][0]
        self.assertFalse(room['default_price']['has_discount'])
        self.assertIsNone(room['default_price']['discount_price'])
        self.assertIsNone(room['default_prices']['local']['usd_discount_price'])
        self.assertFalse(room['default_prices']['local']['usd_has_discount'])
        self.assertIsNone(room['default_prices']['foreign']['original_price'])
        self.assertFalse(room['default_prices']['foreign']['has_discount'])

    def test_public_catalog_prefetches_discounts_once(self):
        from django.test.utils import CaptureQueriesContext
        self.discount(timing_mode='manual')
        RoomType.objects.create(hotel=self.hotel,core_room_type_id=999,name='Other')
        with CaptureQueriesContext(connection) as queries:
            response=APIClient().get('/api/v1/public/room-types/',{'business_id':77})
            self.assertEqual(response.status_code,200)
        self.assertEqual(sum('FROM "booking_roompricediscount"' in query['sql'] for query in queries),1)

    def test_catalog_campaign_date_uses_hotel_timezone(self):
        from datetime import date, datetime, timezone as dt_timezone
        from unittest.mock import patch
        self.hotel.timezone='Asia/Yangon'
        self.hotel.save(update_fields=['timezone'])
        self.discount(start_date=date(2026,11,3),end_date=date(2026,11,3))
        with patch('django.utils.timezone.now',return_value=datetime(2026,11,2,20,tzinfo=dt_timezone.utc)):
            row=APIClient().get('/api/v1/public/room-types/',{'business_id':77}).json()['data']['room_types'][0]
            self.assertEqual(row['default_prices']['local']['price_date'],'2026-11-03')
            self.assertTrue(row['default_price']['has_discount'])
            self.hotel.timezone='America/New_York'
            self.hotel.save(update_fields=['timezone'])
            row=APIClient().get('/api/v1/public/room-types/',{'business_id':77}).json()['data']['room_types'][0]
            self.assertEqual(row['default_prices']['local']['price_date'],'2026-11-02')
            self.assertFalse(row['default_price']['has_discount'])


    @override_settings(BOOKING_ADMIN_API_KEY="key")
    def test_admin_discount_arrays_only_include_active_and_scheduled(self):
        active=self.discount(timing_mode="manual")
        scheduled=self.discount()
        ended=self.discount(timing_mode="manual")
        ended.ended_at=timezone.now()
        ended.save()
        expired=self.discount(start_date=timezone.localdate()-timedelta(days=2),end_date=timezone.localdate()-timedelta(days=1))
        client=self.admin_client()
        root=f"/api/v1/admin/room-types/{self.room_type.id}"
        for url in (root+'/',root+'/room-price/',root+'/discounts/'):
            response=client.get(url)
            self.assertEqual(response.status_code,200,response.data)
            discounts=response.data['data']['discounts']
            self.assertEqual({item['id'] for item in discounts},{active.pk,scheduled.pk})
            self.assertEqual({item['status'] for item in discounts},{'active','scheduled'})
        response=client.get('/api/v1/admin/room-types/')
        rows=response.data['data']
        if isinstance(rows,dict):rows=rows['results']
        self.assertEqual({item['id'] for item in rows[0]['discounts']},{active.pk,scheduled.pk})
        self.assertEqual(self.room_type.price_discounts.count(),4)
