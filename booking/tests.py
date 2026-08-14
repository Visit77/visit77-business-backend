from datetime import date, datetime, timedelta
from decimal import Decimal
from io import StringIO
import uuid
from unittest.mock import patch

from django.test import TestCase, override_settings
from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.management import call_command
from django.utils import timezone
from rest_framework.test import APIClient
from rest_framework_simplejwt.backends import TokenBackend

from booking.models import AddOn, AddOnTemplate, AddOnTemplateRequest, Booking, CoreIntegrationEvent, DailyInventory, DailyRate, Hotel, MealPlan, Payment, PhysicalRoom, PhysicalRoomBlock, RatePlan, RatePeriod, RoomAssignment, RoomType, RoomTypeMealPlan
from booking.integrations.core import sync_business_from_core
from booking.services import availability_for_hotel, cancel_booking, create_admin_reservation, create_booking, create_walk_in_booking, ensure_daily_inventory_for_room_type, record_payment, refund_payment, refund_quote


class BookingServiceTests(TestCase):
    def setUp(self):
        self.hotel = Hotel.objects.create(core_business_id=77, name="Boston Properties")
        self.room_type = RoomType.objects.create(
            hotel=self.hotel,
            core_room_type_id=301,
            name="Double Room",
            max_adults=2,
            max_children=1,
            max_occupancy=3,
            default_inventory=3,
        )
        self.rate_plan = RatePlan.objects.create(
            room_type=self.room_type,
            code="local-standard",
            name="Local Standard",
            guest_market=RatePlan.GuestMarket.LOCAL,
            currency="MMK",
            default_price=Decimal("80000"),
            breakfast_included=True,
        )
        self.check_in = date.today() + timedelta(days=10)
        self.check_out = self.check_in + timedelta(days=2)

    def payload(self):
        return {
            "core_business_id": self.hotel.core_business_id,
            "check_in": self.check_in,
            "check_out": self.check_out,
            "contact_name": "Myo Myo",
            "contact_phone": "09112233445",
            "guest_market": "local",
            "rooms": [{
                "core_room_type_id": self.room_type.core_room_type_id,
                "rate_plan_id": self.rate_plan.id,
                "quantity": 2,
                "adults": 4,
                "children": 0,
                "extra_beds": 1,
            }],
        }

    def test_create_booking_holds_each_night_and_calculates_total(self):
        booking, created = create_booking(self.payload(), "checkout-1")
        self.assertTrue(created)
        self.assertEqual(booking.status, Booking.Status.PENDING_PAYMENT)
        self.assertEqual(booking.grand_total, Decimal("320000"))
        self.assertEqual(list(DailyInventory.objects.values_list("held_rooms", flat=True)), [2, 2])

    def test_idempotency_returns_original_booking_without_second_hold(self):
        first, _ = create_booking(self.payload(), "same-key")
        second, created = create_booking(self.payload(), "same-key")
        self.assertFalse(created)
        self.assertEqual(first.id, second.id)
        self.assertEqual(list(DailyInventory.objects.values_list("held_rooms", flat=True)), [2, 2])

    def test_paid_payment_commits_inventory(self):
        room_801 = PhysicalRoom.objects.create(hotel=self.hotel, room_type=self.room_type, room_number="801", floor="8")
        room_g01 = PhysicalRoom.objects.create(hotel=self.hotel, room_type=self.room_type, room_number="G01", floor="G")
        PhysicalRoom.objects.create(hotel=self.hotel, room_type=self.room_type, room_number="101", floor="1")
        booking, _ = create_booking(self.payload())
        record_payment(booking, {"provider": "mmqr", "amount": booking.grand_total, "status": Payment.Status.PAID})
        booking.refresh_from_db()
        self.assertEqual(booking.status, Booking.Status.CONFIRMED)
        self.assertEqual(list(DailyInventory.objects.values_list("held_rooms", flat=True)), [0, 0])
        self.assertEqual(list(DailyInventory.objects.values_list("reserved_rooms", flat=True)), [2, 2])
        assigned_room_numbers = list(
            RoomAssignment.objects.filter(booking_room__booking=booking)
            .order_by("assigned_at")
            .values_list("physical_room__room_number", flat=True)
        )
        self.assertEqual(assigned_room_numbers, ["G01", "101"])
        room_801.refresh_from_db()
        self.assertEqual(room_801.status, PhysicalRoom.Status.VACANT)

    def test_walk_in_booking_checks_in_immediately_and_occupies_room(self):
        physical_room = PhysicalRoom.objects.create(
            hotel=self.hotel,
            room_type=self.room_type,
            room_number="803",
            floor="8",
        )

        booking = create_walk_in_booking(
            {
                "physical_room_id": physical_room.id,
                "check_in": self.check_in,
                "check_out": self.check_in + timedelta(days=1),
                "contact_name": "Walk In Guest",
                "contact_phone": "09123456789",
                "guest_market": RatePlan.GuestMarket.LOCAL,
                "adults": 2,
                "children": 0,
                "payment": {
                    "provider": Payment.Provider.CASH,
                    "status": Payment.Status.PAID,
                },
            },
            core_business_id=self.hotel.core_business_id,
        )

        booking.refresh_from_db()
        physical_room.refresh_from_db()
        self.assertEqual(booking.status, Booking.Status.CHECKED_IN)
        self.assertEqual(booking.source, Booking.Source.WALK_IN)
        self.assertEqual(booking.amount_paid, booking.grand_total)
        self.assertEqual(physical_room.status, PhysicalRoom.Status.OCCUPIED)
        self.assertTrue(
            RoomAssignment.objects.filter(
                booking_room__booking=booking,
                physical_room=physical_room,
                released_at__isnull=True,
            ).exists()
        )
        inventory = DailyInventory.objects.get(room_type=self.room_type, stay_date=self.check_in)
        self.assertEqual(inventory.held_rooms, 0)
        self.assertEqual(inventory.reserved_rooms, 1)

    def test_payment_auto_assigns_preferred_room_before_lower_floor_fallback(self):
        self.room_type.core_snapshot = {
            "allow_guest_bed_preference": True,
            "beds": [
                {
                    "bed_type": {"id": 1, "name": "King Bed"},
                    "quantity": 1,
                    "is_guest_selectable": True,
                    "is_guaranteed": True,
                    "extra_base_price": 0,
                }
            ],
        }
        self.room_type.save(update_fields=["core_snapshot"])
        PhysicalRoom.objects.create(
            hotel=self.hotel,
            room_type=self.room_type,
            room_number="101",
            floor="1",
            core_snapshot={"beds": [{"bed_type": {"id": 2, "name": "Queen Bed"}}]},
        )
        preferred_room = PhysicalRoom.objects.create(
            hotel=self.hotel,
            room_type=self.room_type,
            room_number="801",
            floor="8",
            core_snapshot={"beds": [{"bed_type": {"id": 1, "name": "King Bed"}}]},
        )
        payload = self.payload()
        payload["rooms"][0]["quantity"] = 1
        payload["rooms"][0]["adults"] = 2
        payload["rooms"][0]["preferences"] = {"core_bed_type_id": 1}

        booking, _ = create_booking(payload)
        record_payment(booking, {"provider": "cash", "amount": booking.grand_total, "status": Payment.Status.PAID})

        assignment = RoomAssignment.objects.get(booking_room__booking=booking)
        self.assertEqual(assignment.physical_room_id, preferred_room.id)

    def test_cancel_pending_booking_returns_held_inventory(self):
        booking, _ = create_booking(self.payload())
        cancel_booking(booking)
        booking.refresh_from_db()
        self.assertEqual(booking.status, Booking.Status.CANCELED)
        self.assertEqual(list(DailyInventory.objects.values_list("held_rooms", flat=True)), [0, 0])

    def test_extra_bed_is_priced_for_each_night(self):
        self.rate_plan.extra_bed_price = Decimal("30000")
        self.rate_plan.save(update_fields=["extra_bed_price"])
        booking, _ = create_booking(self.payload())
        self.assertEqual(booking.grand_total, Decimal("380000"))

    def test_selected_paid_meal_plan_is_priced_for_each_room_night(self):
        meal_plan = MealPlan.objects.create(
            hotel=self.hotel,
            core_meal_plan_id=401,
            name="Breakfast",
            included_meals=["breakfast"],
            local_base_price=Decimal("20000"),
            foreign_base_price=Decimal("30000"),
        )
        link = RoomTypeMealPlan.objects.create(
            room_type=self.room_type,
            meal_plan=meal_plan,
            is_included=False,
            is_default=False,
            is_guest_selectable=True,
        )
        payload = self.payload()
        payload["rooms"][0]["meal_plan_link_id"] = link.id

        booking, _ = create_booking(payload)

        booking_room = booking.rooms.get()
        self.assertEqual(booking.grand_total, Decimal("400000"))
        self.assertEqual(booking_room.meal_plan_link_id, link.id)
        self.assertEqual(booking_room.meal_plan_total, Decimal("80000"))
        self.assertEqual(booking_room.meal_plan_snapshot["name"], "Breakfast")
        self.assertEqual(list(booking_room.nights.values_list("meal_plan_total", flat=True)), [Decimal("40000"), Decimal("40000")])

    def test_selected_custom_breakfast_uses_local_price_and_is_snapshotted(self):
        MealPlan.objects.create(
            hotel=self.hotel,
            core_meal_plan_id=450,
            name="Default Breakfast",
            included_meals=["breakfast"],
            local_base_price=Decimal("10000"),
            foreign_base_price=Decimal("20000"),
            is_default_for_room_type_breakfast=True,
        )
        self.room_type.breakfast_plan_type = RoomType.BreakfastPlanType.CUSTOM_PRICE
        self.room_type.breakfast_custom_local_base_price = Decimal("12000")
        self.room_type.breakfast_custom_local_usd_display_price = Decimal("3")
        self.room_type.breakfast_custom_foreign_base_price = Decimal("25000")
        self.room_type.breakfast_custom_foreign_usd_display_price = Decimal("6")
        self.room_type.save()
        payload = self.payload()
        payload["rooms"][0]["breakfast_selected"] = True

        booking, _ = create_booking(payload)

        booking_room = booking.rooms.get()
        self.assertEqual(booking.grand_total, Decimal("368000"))
        self.assertEqual(booking_room.breakfast_total, Decimal("48000"))
        self.assertEqual(booking_room.breakfast_snapshot["type"], "custom_price")
        self.assertEqual(booking_room.breakfast_snapshot["base_price"], "12000.00")
        self.assertEqual(
            list(booking_room.nights.values_list("breakfast_total", flat=True)),
            [Decimal("24000"), Decimal("24000")],
        )

    def test_default_included_meal_plan_is_attached_without_charge(self):
        meal_plan = MealPlan.objects.create(
            hotel=self.hotel,
            core_meal_plan_id=402,
            name="Breakfast Included",
            included_meals=["breakfast"],
            local_base_price=Decimal("20000"),
            foreign_base_price=Decimal("30000"),
        )
        link = RoomTypeMealPlan.objects.create(
            room_type=self.room_type,
            meal_plan=meal_plan,
            is_included=True,
            is_default=True,
            is_guest_selectable=True,
        )

        booking, _ = create_booking(self.payload())

        booking_room = booking.rooms.get()
        self.assertEqual(booking.grand_total, Decimal("320000"))
        self.assertEqual(booking_room.meal_plan_link_id, link.id)
        self.assertEqual(booking_room.meal_plan_total, Decimal("0"))
        self.assertTrue(booking_room.meal_plan_snapshot["is_included"])
        self.assertIn("pricing_mode", booking_room.meal_plan_snapshot)

    def test_deposit_confirms_and_refund_updates_paid_balance(self):
        self.rate_plan.cancellation_policy = {"type": "free_full_refund"}
        self.rate_plan.save(update_fields=["cancellation_policy"])
        booking, _ = create_booking(self.payload())
        payment = record_payment(booking, {"provider": "cash", "amount": Decimal("50000"), "status": Payment.Status.PAID})
        booking.refresh_from_db()
        self.assertEqual(booking.status, Booking.Status.CONFIRMED)
        refund_payment(payment, Decimal("20000"), "refund-1")
        booking.refresh_from_db()
        payment.refresh_from_db()
        self.assertEqual(booking.amount_paid, Decimal("30000"))
        self.assertEqual(payment.status, Payment.Status.PARTIALLY_REFUNDED)

    def test_refund_quote_uses_less_than_and_highest_more_than_rule(self):
        self.rate_plan.cancellation_policy = {
            "type": "partial_refund",
            "less_than_rule": {"hours_before_check_in": 24, "refund_percent": 0},
            "more_than_rules": [
                {"hours_before_check_in": 24, "refund_percent": 50},
                {"hours_before_check_in": 72, "refund_percent": 100},
            ],
            "description": "Refunds return to the original payment method.",
        }
        self.rate_plan.save(update_fields=["cancellation_policy"])
        booking, _ = create_booking(self.payload())
        record_payment(booking, {"provider": "cash", "amount": booking.grand_total, "status": Payment.Status.PAID})
        check_in_at = timezone.make_aware(datetime.combine(self.check_in, self.hotel.check_in_time or datetime.min.time()))

        within_24 = refund_quote(booking, at=check_in_at - timedelta(hours=12))
        between_24_and_72 = refund_quote(booking, at=check_in_at - timedelta(hours=48))
        over_72 = refund_quote(booking, at=check_in_at - timedelta(hours=96))

        self.assertEqual(within_24["rooms"][0]["refund_percent"], Decimal("0"))
        self.assertEqual(between_24_and_72["rooms"][0]["refund_percent"], Decimal("50"))
        self.assertEqual(over_72["rooms"][0]["refund_percent"], Decimal("100"))
        self.assertEqual(between_24_and_72["refundable_remaining"], Decimal("160000.00"))
        self.assertEqual(
            between_24_and_72["rooms"][0]["description"],
            "Refunds return to the original payment method.",
        )

    def test_effective_rate_period_and_daily_override_price_each_stay_date(self):
        RatePeriod.objects.create(
            rate_plan=self.rate_plan,
            name="High Season",
            start_date=self.check_in,
            end_date=self.check_out - timedelta(days=1),
            price=Decimal("120000"),
        )
        DailyRate.objects.create(
            rate_plan=self.rate_plan,
            stay_date=self.check_in + timedelta(days=1),
            price=Decimal("150000"),
        )
        available = availability_for_hotel(self.hotel, self.check_in, self.check_out, adults=2, guest_market="local")
        prices = available[0]["rate_plans"][0]["nightly_prices"]
        self.assertEqual([item["price"] for item in prices], [Decimal("120000"), Decimal("150000")])

        booking, _ = create_booking(self.payload())
        self.assertEqual(booking.grand_total, Decimal("540000"))

    def test_foreigner_uses_foreigner_period_and_daily_prices(self):
        foreign_rate_plan = RatePlan.objects.create(
            room_type=self.room_type,
            code="foreign-standard",
            name="Foreign Standard",
            guest_market=RatePlan.GuestMarket.FOREIGN,
            currency="USD",
            default_price=Decimal("40"),
        )
        RatePeriod.objects.create(
            rate_plan=foreign_rate_plan,
            name="High Season",
            start_date=self.check_in,
            end_date=self.check_out - timedelta(days=1),
            price=Decimal("55"),
        )
        DailyRate.objects.create(
            rate_plan=foreign_rate_plan,
            stay_date=self.check_in + timedelta(days=1),
            price=Decimal("65"),
        )
        available = availability_for_hotel(
            self.hotel,
            self.check_in,
            self.check_out,
            adults=2,
            guest_market="foreign",
        )
        prices = available[0]["rate_plans"][0]["nightly_prices"]
        self.assertEqual([item["price"] for item in prices], [Decimal("55"), Decimal("65")])

        payload = self.payload()
        payload["guest_market"] = "foreign"
        payload["rooms"][0]["rate_plan_id"] = foreign_rate_plan.id
        booking, _ = create_booking(payload)
        self.assertEqual(booking.grand_total, Decimal("240"))

    @override_settings(BOOKING_INVENTORY_WINDOW_DAYS=2)
    def test_core_sync_updates_default_plans_inventory_and_preserves_custom_plans(self):
        class StubCoreClient:
            local_price = 80000

            def provisioning_bundle(self, core_business_id):
                return {
                    "access": {
                        "status": "active",
                        "package": "ota_pms",
                        "features": {
                            "online_booking": True,
                            "public_availability": True,
                            "room_assignment": True,
                            "walk_in_booking": True,
                        },
                    },
                    "business": {"id": core_business_id, "name": "Seed Hotel", "status": True},
                    "meal_plans": [
                        {
                            "id": 501,
                            "name": "Breakfast",
                            "description": "Breakfast buffet.",
                            "included_meals": ["breakfast"],
                            "meal_windows": {"breakfast": {"start": "06:30", "end": "10:00"}},
                            "availability": "guest_only",
                            "local_base_price": 20000,
                            "local_usd_display_price": 10,
                            "foreign_base_price": 30000,
                            "foreign_usd_display_price": 15,
                            "is_active": True,
                        }
                    ],
                    "room_types": [{
                        "id": 901,
                        "name": "Seed Double",
                        "max_adults": 2,
                        "max_children": 1,
                        "max_occupancy": 3,
                        "is_active": True,
                        "rate_plans": [
                            {
                                "core_rate_plan_id": "room-901-local",
                                "code": "local-standard",
                                "name": "Local Standard Rate",
                                "guest_market": "local",
                                "currency": "MMK",
                                "base_price": self.local_price,
                                "usd_display_price": 40,
                                "refundable": None,
                            },
                            {
                                "core_rate_plan_id": "room-901-foreign",
                                "code": "foreign-standard",
                                "name": "Foreign Standard Rate",
                                "guest_market": "foreign",
                                "currency": "MMK",
                                "base_price": 100000,
                                "usd_display_price": 50,
                            },
                        ],
                        "meal_plans": [
                            {
                                "meal_plan": {
                                    "id": 501,
                                    "included_meals": ["breakfast"],
                                    "meal_windows": {"breakfast": {"start": "06:30", "end": "10:00"}},
                                },
                                "is_included": True,
                                "is_default": True,
                                "is_guest_selectable": True,
                                "use_hotel_default_price": True,
                            }
                        ],
                    }],
                    "physical_rooms": [
                        {
                            "id": 9901,
                            "room_type_id": 901,
                            "room_no": "801",
                            "floor": "8",
                            "floor_data": {"id": 8008, "name": "8"},
                            "building": "Main Building",
                            "building_data": {"id": 7001, "name": "Main Building"},
                            "is_active": True,
                        },
                        {
                            "id": 9902,
                            "room_type_id": 901,
                            "room_no": "802",
                            "floor": "8",
                            "floor_data": {"id": 8008, "name": "8"},
                            "building": "Main Building",
                            "building_data": {"id": 7001, "name": "Main Building"},
                            "is_active": True,
                        },
                    ],
                }

        client = StubCoreClient()
        sync_business_from_core(99, client=client)
        hotel = Hotel.objects.get(core_business_id=99)
        self.assertEqual(hotel.package, Hotel.Package.OTA_PMS)
        self.assertTrue(hotel.has_feature("online_booking"))
        self.assertTrue(hotel.has_feature("room_assignment"))
        room_type = RoomType.objects.get(hotel__core_business_id=99, core_room_type_id=901)
        self.assertEqual(room_type.default_inventory, 2)
        self.assertEqual(DailyInventory.objects.filter(room_type=room_type, total_rooms=2).count(), 3)
        room = PhysicalRoom.objects.get(core_physical_room_id=9901)
        self.assertEqual(room.core_building_id, 7001)
        self.assertEqual(room.core_floor_id, 8008)
        custom = RatePlan.objects.create(
            room_type=room_type,
            code="local-saver",
            name="Local Saver",
            guest_market="local",
            currency="MMK",
            default_price=Decimal("70000"),
            refundable=False,
        )
        client.local_price = 90000
        sync_business_from_core(99, client=client)

        local_default = RatePlan.objects.get(core_rate_plan_id="room-901-local")
        custom.refresh_from_db()
        self.assertEqual(local_default.default_price, Decimal("90000"))
        self.assertEqual(local_default.base_price, Decimal("90000"))
        self.assertEqual(local_default.usd_display_price, Decimal("40"))
        self.assertTrue(local_default.refundable)
        foreign_default = RatePlan.objects.get(core_rate_plan_id="room-901-foreign")
        self.assertEqual(foreign_default.base_price, Decimal("100000"))
        self.assertEqual(foreign_default.usd_display_price, Decimal("50"))
        meal_plan = MealPlan.objects.get(hotel=hotel, core_meal_plan_id=501)
        self.assertEqual(meal_plan.name, "Breakfast")
        self.assertEqual(meal_plan.meal_windows["breakfast"]["start"], "06:30")
        self.assertTrue(meal_plan.includes_breakfast)
        room_type_meal_plan = RoomTypeMealPlan.objects.get(room_type=room_type, meal_plan=meal_plan)
        self.assertTrue(room_type_meal_plan.is_included)
        self.assertEqual(room_type_meal_plan.pricing_mode, RoomTypeMealPlan.PricingMode.INCLUDED_IN_ROOM_PRICE)
        self.assertEqual(room_type_meal_plan.effective_local_base_price, Decimal("0"))
        self.assertEqual(local_default.source, RatePlan.Source.CORE)
        self.assertTrue(local_default.is_default)
        self.assertEqual(custom.default_price, Decimal("70000"))
        self.assertEqual(custom.source, RatePlan.Source.BOOKING)
        self.assertEqual(room_type.rate_plans.count(), 3)

    @override_settings(BOOKING_INVENTORY_WINDOW_DAYS=2)
    def test_daily_inventory_auto_seed_adjusts_with_physical_room_count(self):
        PhysicalRoom.objects.create(hotel=self.hotel, room_type=self.room_type, room_number="801")
        room_802 = PhysicalRoom.objects.create(hotel=self.hotel, room_type=self.room_type, room_number="802")

        created = ensure_daily_inventory_for_room_type(self.room_type, start_date=self.check_in)
        self.assertEqual(created["created"], 3)
        self.assertEqual(self.room_type.daily_inventory.filter(total_rooms=2).count(), 3)
        self.room_type.refresh_from_db()
        self.assertEqual(self.room_type.default_inventory, 2)

        first = DailyInventory.objects.get(room_type=self.room_type, stay_date=self.check_in)
        first.reserved_rooms = 2
        first.save(update_fields=["reserved_rooms"])
        room_802.status = PhysicalRoom.Status.OUT_OF_SERVICE
        room_802.save(update_fields=["status"])

        adjusted = ensure_daily_inventory_for_room_type(self.room_type, start_date=self.check_in)
        self.assertEqual(adjusted["created"], 0)
        first.refresh_from_db()
        self.assertEqual(first.total_rooms, 2)
        self.assertEqual(
            list(self.room_type.daily_inventory.exclude(id=first.id).values_list("total_rooms", flat=True)),
            [1, 1],
        )


@override_settings(BOOKING_ADMIN_API_KEY="test-admin-key", CORE_JWT_SIGNING_KEY="test-core-jwt-key")
class BookingApiTests(BookingServiceTests):
    def test_room_block_date_range_updates_board_inventory_and_prevents_walk_in(self):
        room = PhysicalRoom.objects.create(hotel=self.hotel, room_type=self.room_type, room_number="VIP-01")
        headers = {
            "HTTP_X_BOOKING_ADMIN_KEY": "test-admin-key",
            "HTTP_X_BOOKING_BUSINESS_ID": str(self.hotel.core_business_id),
        }
        created = self.client.post(
            "/api/v1/admin/room-blocks/",
            {
                "physical_room": room.id,
                "start_date": str(self.check_in),
                "end_date": str(self.check_out),
                "note": "Held for VIP guest",
            },
            format="json",
            **headers,
        )
        self.assertEqual(created.status_code, 201, created.data)
        board = self.client.get("/api/v1/admin/room-board/", {"date": str(self.check_in)}, **headers)
        board_room = next(item for item in board.data["data"]["rooms"] if item["id"] == room.id)
        self.assertEqual(board_room["display_status"], "blocked")
        self.assertEqual(board_room["block"]["note"], "Held for VIP guest")
        self.assertEqual(board.data["data"]["summary"]["blocked"], 1)
        inventory = DailyInventory.objects.get(room_type=self.room_type, stay_date=self.check_in)
        self.assertEqual(inventory.total_rooms, 0)

        unavailable = self.client.post(
            "/api/v1/admin/walk-in-bookings/",
            {
                "physical_room_id": room.id,
                "rate_plan_id": self.rate_plan.id,
                "check_in": str(self.check_in),
                "check_out": str(self.check_out),
                "contact_name": "Blocked Guest",
                "contact_phone": "091111111",
                "guest_market": "local",
                "adults": 1,
                "children": 0,
                "guests": [{"name": "Blocked Guest", "is_primary": True}],
            },
            format="json",
            **headers,
        )
        self.assertEqual(unavailable.status_code, 400, unavailable.data)

        unblocked = self.client.post(
            f"/api/v1/admin/room-blocks/{created.data['data']['id']}/unblock/",
            {},
            format="json",
            **headers,
        )
        self.assertEqual(unblocked.status_code, 200, unblocked.data)
        self.assertFalse(unblocked.data["data"]["is_active"])

        board = self.client.get("/api/v1/admin/room-board/", {"date": str(self.check_in)}, **headers)
        board_room = next(item for item in board.data["data"]["rooms"] if item["id"] == room.id)
        self.assertEqual(board_room["display_status"], "available")
        self.assertEqual(board.data["data"]["summary"]["blocked"], 0)
        inventory.refresh_from_db()
        self.assertEqual(inventory.total_rooms, 1)

    def test_room_block_rejects_reserved_or_occupied_date_range(self):
        room = PhysicalRoom.objects.create(hotel=self.hotel, room_type=self.room_type, room_number="O01")
        booking = create_walk_in_booking(
            {
                "physical_room_id": room.id,
                "rate_plan_id": self.rate_plan.id,
                "check_in": self.check_in,
                "check_out": self.check_out,
                "contact_name": "Current Guest",
                "contact_phone": "091111111",
                "guest_market": "local",
                "adults": 1,
                "children": 0,
                "guests": [{"name": "Current Guest", "is_primary": True}],
            },
            core_business_id=self.hotel.core_business_id,
        )
        self.assertEqual(booking.status, Booking.Status.CHECKED_IN)
        response = self.client.post(
            "/api/v1/admin/room-blocks/",
            {
                "physical_room": room.id,
                "start_date": str(self.check_in),
                "end_date": str(self.check_out),
                "note": "Conflicting VIP block",
            },
            format="json",
            HTTP_X_BOOKING_ADMIN_KEY="test-admin-key",
            HTTP_X_BOOKING_BUSINESS_ID=str(self.hotel.core_business_id),
        )
        self.assertEqual(response.status_code, 400, response.data)
        error_message = " ".join(response.data["error"])
        self.assertIn(booking.reference, error_message)
        self.assertIn("occupied", error_message)
        self.assertIn(str(booking.check_in), error_message)
        self.assertIn(str(booking.check_out), error_message)
        conflicts = response.data["data"]["conflict_bookings"]
        self.assertEqual(len(conflicts), 1)
        self.assertEqual(conflicts[0]["booking_id"], str(booking.id))
        self.assertEqual(conflicts[0]["reference"], booking.reference)
        self.assertEqual(conflicts[0]["status"], "occupied")
        self.assertEqual(conflicts[0]["check_in"], str(booking.check_in))
        self.assertEqual(conflicts[0]["check_out"], str(booking.check_out))
        self.assertFalse(PhysicalRoomBlock.objects.exists())

    def test_room_block_conflict_message_identifies_reserved_booking(self):
        room = PhysicalRoom.objects.create(hotel=self.hotel, room_type=self.room_type, room_number="R01")
        booking = create_walk_in_booking(
            {
                "physical_room_id": room.id,
                "rate_plan_id": self.rate_plan.id,
                "check_in": self.check_in,
                "check_out": self.check_out,
                "contact_name": "Reserved Guest",
                "contact_phone": "091111111",
                "guest_market": "local",
                "adults": 1,
                "children": 0,
                "guests": [{"name": "Reserved Guest", "is_primary": True}],
            },
            core_business_id=self.hotel.core_business_id,
            check_in_immediately=False,
        )
        response = self.client.post(
            "/api/v1/admin/room-blocks/",
            {
                "physical_room": room.id,
                "start_date": str(self.check_in),
                "end_date": str(self.check_out),
            },
            format="json",
            HTTP_X_BOOKING_ADMIN_KEY="test-admin-key",
            HTTP_X_BOOKING_BUSINESS_ID=str(self.hotel.core_business_id),
        )

        self.assertEqual(response.status_code, 400, response.data)
        error_message = " ".join(response.data["error"])
        self.assertIn(booking.reference, error_message)
        self.assertIn("reserved", error_message)
        conflicts = response.data["data"]["conflict_bookings"]
        self.assertEqual(conflicts[0]["status"], "reserved")
        self.assertEqual(conflicts[0]["booking_status"], Booking.Status.CONFIRMED)

    def test_room_board_exposes_upcoming_and_current_block_state(self):
        room = PhysicalRoom.objects.create(hotel=self.hotel, room_type=self.room_type, room_number="VIP-02")
        future_start = self.check_in + timedelta(days=2)
        future_end = future_start + timedelta(days=5)
        block = PhysicalRoomBlock.objects.create(
            physical_room=room,
            start_date=future_start,
            end_date=future_end,
            note="Future VIP hold",
        )
        headers = {
            "HTTP_X_BOOKING_ADMIN_KEY": "test-admin-key",
            "HTTP_X_BOOKING_BUSINESS_ID": str(self.hotel.core_business_id),
        }

        today_board = self.client.get("/api/v1/admin/room-board/", {"date": str(self.check_in)}, **headers)
        today_room = next(item for item in today_board.data["data"]["rooms"] if item["id"] == room.id)
        self.assertEqual(today_room["display_status"], "available")
        self.assertEqual(today_room["block_status"], "upcoming_block")
        self.assertIsNone(today_room["current_block"])
        self.assertEqual(today_room["upcoming_block"]["id"], block.id)
        self.assertEqual(today_room["block_timeline"]["days_until_block"], 2)
        self.assertEqual(today_room["block_timeline"]["blocked_days"], 6)

        future_board = self.client.get("/api/v1/admin/room-board/", {"date": str(future_start)}, **headers)
        future_room = next(item for item in future_board.data["data"]["rooms"] if item["id"] == room.id)
        self.assertEqual(future_room["display_status"], "blocked")
        self.assertEqual(future_room["block_status"], "currently_blocked")
        self.assertEqual(future_room["current_block"]["id"], block.id)

    def test_check_in_form_accepts_deposit_then_remaining_balance_payment(self):
        booking, _ = create_booking(self.payload())
        headers = {
            "HTTP_X_BOOKING_ADMIN_KEY": "test-admin-key",
            "HTTP_X_BOOKING_BUSINESS_ID": str(self.hotel.core_business_id),
        }
        deposit = self.client.post(
            f"/api/v1/admin/bookings/{booking.id}/payment/",
            {
                "payment_type": "deposit",
                "provider": "cash",
                "amount": "50000.00",
                "provider_reference": "DEP-001",
                "status": "paid",
            },
            format="json",
            **headers,
        )
        self.assertEqual(deposit.status_code, 201, deposit.data)
        self.assertEqual(deposit.data["data"]["payment_type"], "deposit")

        form = self.client.get(f"/api/v1/admin/bookings/{booking.id}/check-in-form/", **headers)
        self.assertEqual(form.status_code, 200, form.data)
        summary = form.data["data"]["payment_summary"]
        self.assertEqual(summary["amount_paid"], Decimal("50000.00"))
        self.assertEqual(summary["amount_due"], booking.grand_total - Decimal("50000.00"))
        self.assertEqual(summary["payment_status"], "partially_paid")

        overpayment = self.client.post(
            f"/api/v1/admin/bookings/{booking.id}/payment/",
            {"payment_type": "deposit", "provider": "cash", "amount": str(booking.grand_total)},
            format="json",
            **headers,
        )
        self.assertEqual(overpayment.status_code, 400, overpayment.data)

        balance = self.client.post(
            f"/api/v1/admin/bookings/{booking.id}/payment/",
            {"payment_type": "balance", "provider": "cash", "provider_reference": "BAL-001"},
            format="json",
            **headers,
        )
        self.assertEqual(balance.status_code, 201, balance.data)
        self.assertEqual(balance.data["data"]["payment_type"], "balance")
        booking.refresh_from_db()
        self.assertEqual(booking.amount_paid, booking.grand_total)

        refreshed = self.client.get(f"/api/v1/admin/bookings/{booking.id}/check-in-form/", **headers)
        self.assertEqual(refreshed.data["data"]["payment_summary"]["amount_due"], Decimal("0"))
        self.assertEqual(refreshed.data["data"]["payment_summary"]["payment_status"], "paid")

    def test_check_in_form_updates_reservation_dates_capacity_and_guests(self):
        room = PhysicalRoom.objects.create(hotel=self.hotel, room_type=self.room_type, room_number="303-U")
        booking = create_admin_reservation({
            **self.payload(),
            "source": Booking.Source.PHONE,
            "rooms": [{
                "core_room_type_id": self.room_type.core_room_type_id,
                "rate_plan_id": self.rate_plan.id,
                "quantity": 1,
                "adults": 1,
                "children": 0,
                "extra_beds": 0,
                "physical_room_ids": [room.id],
            }],
        })
        primary = booking.guests.get(is_primary=True)
        headers = {
            "HTTP_X_BOOKING_ADMIN_KEY": "test-admin-key",
            "HTTP_X_BOOKING_BUSINESS_ID": str(self.hotel.core_business_id),
        }
        new_check_out = self.check_out + timedelta(days=1)

        response = self.client.patch(
            f"/api/v1/admin/bookings/{booking.id}/check-in-form/",
            {
                "check_in": str(self.check_in),
                "check_out": str(new_check_out),
                "adults": 2,
                "children": 1,
                "extra_beds": 1,
                "contact_name": "Updated Contact",
                "guests": [
                    {"id": primary.id, "name": "Updated Primary", "identity_number": "NRC-1", "is_primary": True},
                    {"name": "Second Guest", "identity_number": "PP-2"},
                ],
            },
            format="json",
            **headers,
        )

        self.assertEqual(response.status_code, 200, response.data)
        booking.refresh_from_db()
        booking_room = booking.rooms.get()
        self.assertEqual(booking.check_out, new_check_out)
        self.assertEqual(booking.contact_name, "Updated Contact")
        self.assertEqual(booking_room.adults, 2)
        self.assertEqual(booking_room.children, 1)
        self.assertEqual(booking_room.extra_beds, 1)
        self.assertEqual(booking_room.nights.count(), 3)
        self.assertEqual(booking_room.assignments.get().physical_room, room)
        self.assertEqual(booking.guests.count(), 2)
        self.assertTrue(booking.guests.filter(name="Updated Primary", identity_number="NRC-1").exists())
        self.assertTrue(booking.guests.filter(name="Second Guest", identity_number="PP-2").exists())

    def test_check_in_form_accepts_unchanged_current_room_even_if_marked_occupied(self):
        room = PhysicalRoom.objects.create(hotel=self.hotel, room_type=self.room_type, room_number="303-S")
        booking = create_admin_reservation({
            **self.payload(),
            "source": Booking.Source.PHONE,
            "rooms": [{
                "core_room_type_id": self.room_type.core_room_type_id,
                "rate_plan_id": self.rate_plan.id,
                "quantity": 1,
                "adults": 1,
                "children": 0,
                "physical_room_ids": [room.id],
            }],
        })
        room.status = PhysicalRoom.Status.OCCUPIED
        room.save(update_fields=["status"])
        headers = {
            "HTTP_X_BOOKING_ADMIN_KEY": "test-admin-key",
            "HTTP_X_BOOKING_BUSINESS_ID": str(self.hotel.core_business_id),
        }

        response = self.client.patch(
            f"/api/v1/admin/bookings/{booking.id}/check-in-form/",
            {
                "physical_room_id": str(room.id),
                "rate_plan_id": str(self.rate_plan.id),
                "check_in": str(booking.check_in),
                "check_out": str(booking.check_out),
                "adults": "1",
                "children": "0",
                "extra_beds": "0",
                "contact_name": booking.contact_name,
                "contact_phone": booking.contact_phone,
                "guest[0][name]": "Updated Existing Guest",
                "guest[0][nrc_number]": "8/MAMANA(N)123465",
                "guest[0][is_primary]": "true",
            },
            format="multipart",
            **headers,
        )

        self.assertEqual(response.status_code, 200, response.data)
        booking.refresh_from_db()
        self.assertEqual(booking.rooms.get().assignments.get().physical_room, room)
        self.assertEqual(booking.guests.count(), 1)
        guest = booking.guests.get()
        self.assertEqual(guest.name, "Updated Existing Guest")
        self.assertEqual(guest.nrc_number, "8/MAMANA(N)123465")

    def test_check_in_form_rejects_date_update_overlapping_assigned_room(self):
        room = PhysicalRoom.objects.create(hotel=self.hotel, room_type=self.room_type, room_number="303-O")
        first = create_admin_reservation({
            **self.payload(),
            "source": Booking.Source.PHONE,
            "rooms": [{
                "core_room_type_id": self.room_type.core_room_type_id,
                "rate_plan_id": self.rate_plan.id,
                "quantity": 1,
                "adults": 1,
                "children": 0,
                "physical_room_ids": [room.id],
            }],
        })
        later_payload = self.payload()
        later_payload["check_in"] = self.check_out
        later_payload["check_out"] = self.check_out + timedelta(days=2)
        later_payload["source"] = Booking.Source.PHONE
        later_payload["rooms"] = [{
            "core_room_type_id": self.room_type.core_room_type_id,
            "rate_plan_id": self.rate_plan.id,
            "quantity": 1,
            "adults": 1,
            "children": 0,
            "physical_room_ids": [room.id],
        }]
        create_admin_reservation(later_payload)
        headers = {
            "HTTP_X_BOOKING_ADMIN_KEY": "test-admin-key",
            "HTTP_X_BOOKING_BUSINESS_ID": str(self.hotel.core_business_id),
        }

        response = self.client.patch(
            f"/api/v1/admin/bookings/{first.id}/check-in-form/",
            {"check_out": str(self.check_out + timedelta(days=1))},
            format="json",
            **headers,
        )

        self.assertEqual(response.status_code, 400, response.data)
        first.refresh_from_db()
        self.assertEqual(first.check_out, self.check_out)

    def test_legacy_walk_in_api_checks_in_immediately_without_identity_verification(self):
        room = PhysicalRoom.objects.create(hotel=self.hotel, room_type=self.room_type, room_number="304")
        response = self.client.post(
            "/api/v1/admin/walk-in-bookings/",
            {
                "physical_room_id": room.id,
                "rate_plan_id": self.rate_plan.id,
                "check_in": str(self.check_in),
                "check_out": str(self.check_out),
                "contact_name": "Legacy Walk In",
                "contact_phone": "091111111",
                "guest_market": "local",
                "adults": 1,
                "children": 0,
                "guests": [{"name": "Legacy Walk In", "is_primary": True}],
                "payment": {"provider": "cash", "status": "paid"},
            },
            format="json",
            HTTP_X_BOOKING_ADMIN_KEY="test-admin-key",
            HTTP_X_BOOKING_BUSINESS_ID=str(self.hotel.core_business_id),
        )
        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(response.data["data"]["status"], Booking.Status.CHECKED_IN)
        booking = Booking.objects.get(id=response.data["data"]["id"])
        room.refresh_from_db()
        self.assertEqual(booking.status, Booking.Status.CHECKED_IN)
        self.assertEqual(room.status, PhysicalRoom.Status.OCCUPIED)
        self.assertFalse(booking.guests.filter(identity_documents__isnull=False).exists())

    def setUp(self):
        super().setUp()
        self.client = APIClient()

    def core_access_token(self, *, is_superuser=True, user_id=9001):
        now = timezone.now()
        return TokenBackend(algorithm="HS256", signing_key="test-core-jwt-key").encode({
            "token_type": "access",
            "user_id": user_id,
            "email": "admin@visit77.com",
            "is_staff": True,
            "is_superuser": is_superuser,
            "iat": int(now.timestamp()),
            "exp": int((now + timedelta(minutes=10)).timestamp()),
        })

    def test_public_availability(self):
        response = self.client.get(
            f"/api/v1/public/hotels/{self.hotel.core_business_id}/availability/",
            {"check_in": self.check_in, "check_out": self.check_out, "adults": 2, "guest_market": "local"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["data"]["room_types"][0]["available_rooms"], 3)

    def test_public_booking_estimate_does_not_require_contact_info(self):
        self.rate_plan.extra_bed_base_price = Decimal("30000")
        self.rate_plan.save(update_fields=["extra_bed_base_price"])
        payload = {
            "core_business_id": self.hotel.core_business_id,
            "check_in": str(self.check_in),
            "check_out": str(self.check_out),
            "guest_market": "local",
            "rooms": [{
                "core_room_type_id": self.room_type.core_room_type_id,
                "rate_plan_id": self.rate_plan.id,
                "quantity": 2,
                "adults": 4,
                "children": 0,
                "extra_beds": 1,
            }],
        }

        response = self.client.post("/api/v1/public/bookings/estimate/", payload, format="json")

        self.assertEqual(response.status_code, 200, response.data)
        data = response.data["data"]
        self.assertEqual(data["grand_total"], Decimal("380000"))
        self.assertEqual(data["formatted_grand_total"], "MMK 380,000")
        self.assertEqual(data["summary_text"], "2 Rooms x 2 Nights x 1 Extra Bed")
        self.assertEqual(data["summary_items"][0]["label"], "2 x Double Room")
        self.assertEqual(data["summary_items"][1]["label"], "1 x Extra Bed(s)")

    def test_phone_reservation_creates_confirmed_booking_without_checking_in(self):
        room = PhysicalRoom.objects.create(hotel=self.hotel, room_type=self.room_type, room_number="305")
        response = self.client.post(
            "/api/v1/admin/reservations/",
            {
                "core_business_id": self.hotel.core_business_id,
                "source": "phone",
                "source_name": "Hotel hotline",
                "check_in": str(self.check_in),
                "check_out": str(self.check_out),
                "contact_name": "Phone Guest",
                "contact_phone": "091111111",
                "guest_market": "local",
                "rooms": [{
                    "core_room_type_id": self.room_type.core_room_type_id,
                    "rate_plan_id": self.rate_plan.id,
                    "quantity": 1,
                    "adults": 2,
                    "children": 0,
                    "physical_room_ids": [room.id],
                }],
                "guests": [{"name": "Phone Guest", "is_primary": True}],
                "payment": {"payment_type": "full_payment", "provider": "cash", "status": "paid"},
            },
            format="json",
            HTTP_X_BOOKING_ADMIN_KEY="test-admin-key",
            HTTP_X_BOOKING_BUSINESS_ID=str(self.hotel.core_business_id),
        )
        self.assertEqual(response.status_code, 201, response.data)
        booking = Booking.objects.get(id=response.data["data"]["booking"]["id"])
        room.refresh_from_db()
        self.assertEqual(booking.source, Booking.Source.PHONE)
        self.assertEqual(booking.status, Booking.Status.CONFIRMED)
        self.assertEqual(booking.amount_paid, booking.grand_total)
        self.assertEqual(booking.payments.get().payment_type, Payment.Type.FULL_PAYMENT)
        self.assertEqual(room.status, PhysicalRoom.Status.VACANT)
        self.assertFalse(response.data["data"]["verification"]["can_check_in"])

    def test_walk_in_check_in_requires_identity_number_but_not_identity_photo(self):
        room = PhysicalRoom.objects.create(hotel=self.hotel, room_type=self.room_type, room_number="306")
        headers = {
            "HTTP_X_BOOKING_ADMIN_KEY": "test-admin-key",
            "HTTP_X_BOOKING_BUSINESS_ID": str(self.hotel.core_business_id),
            "HTTP_X_CORE_USER_ID": "501",
        }
        created = self.client.post(
            "/api/v1/admin/walk-in-booking-v2/",
            {
                "physical_room_id": room.id,
                "rate_plan_id": self.rate_plan.id,
                "check_in": str(self.check_in),
                "check_out": str(self.check_out),
                "contact_name": "Walk In Guest",
                "contact_phone": "092222222",
                "guest_market": "local",
                "adults": 1,
                "children": 0,
                "guests": [{
                    "name": "Walk In Guest",
                    "nrc_number": "12/ABC(N)123456",
                    "identity_type": "nrc",
                    "identity_number": "12/ABC(N)123456",
                    "is_primary": True,
                }],
            },
            format="json",
            **headers,
        )
        self.assertEqual(created.status_code, 201, created.data)
        booking_id = created.data["data"]["booking"]["id"]
        booking = Booking.objects.get(id=booking_id)
        guest = booking.guests.get(is_primary=True)
        self.assertEqual(booking.status, Booking.Status.CONFIRMED)
        self.assertFalse(booking.payments.exists())

        checked_in = self.client.post(
            f"/api/v1/admin/bookings/{booking_id}/check-in/",
            {"verification_confirmed": True, "verification_note": "NRC checked."},
            format="json",
            **headers,
        )
        self.assertEqual(checked_in.status_code, 200, checked_in.data)
        booking.refresh_from_db()
        room.refresh_from_db()
        self.assertEqual(booking.status, Booking.Status.CHECKED_IN)
        self.assertEqual(booking.checked_in_by_core_user_id, 501)
        self.assertEqual(room.status, PhysicalRoom.Status.OCCUPIED)
        self.assertFalse(booking.guests.filter(identity_documents__isnull=False).exists())

    def test_check_in_blocks_guest_without_identity_number_even_when_photo_is_optional(self):
        room = PhysicalRoom.objects.create(hotel=self.hotel, room_type=self.room_type, room_number="306-B")
        booking = create_walk_in_booking(
            {
                "physical_room_id": room.id,
                "rate_plan_id": self.rate_plan.id,
                "check_in": self.check_in,
                "check_out": self.check_out,
                "contact_name": "No Identity Guest",
                "contact_phone": "092222222",
                "guest_market": "local",
                "adults": 1,
                "children": 0,
                "guests": [{"name": "No Identity Guest", "is_primary": True}],
            },
            core_business_id=self.hotel.core_business_id,
            check_in_immediately=False,
        )
        headers = {
            "HTTP_X_BOOKING_ADMIN_KEY": "test-admin-key",
            "HTTP_X_BOOKING_BUSINESS_ID": str(self.hotel.core_business_id),
        }

        response = self.client.post(
            f"/api/v1/admin/bookings/{booking.id}/check-in/",
            {"verification_confirmed": True},
            format="json",
            **headers,
        )

        self.assertEqual(response.status_code, 400, response.data)
        self.assertIn(".identity_number", response.data["error"][1])

    def test_walk_in_v2_accepts_guest_identity_photos_in_initial_multipart_request(self):
        room = PhysicalRoom.objects.create(hotel=self.hotel, room_type=self.room_type, room_number="307")
        response = self.client.post(
            "/api/v1/admin/walk-in-booking-v2/",
            {
                "physical_room_id": room.id,
                "rate_plan_id": self.rate_plan.id,
                "check_in": str(self.check_in),
                "check_out": str(self.check_out),
                "contact_name": "Multipart Guest",
                "contact_phone": "093333333",
                "guest_market": "local",
                "adults": 1,
                "children": 0,
                "guests": '[{"name":"Multipart Guest","identity_type":"nrc","identity_number":"12/ABC(N)123456","is_primary":true}]',
                "payment": '{"payment_type":"full_payment","provider":"cash","status":"paid"}',
                "guest_identity_photo_0": SimpleUploadedFile(
                    "identity-photo.jpg", b"test-image", content_type="image/jpeg"
                ),
            },
            format="multipart",
            HTTP_X_BOOKING_ADMIN_KEY="test-admin-key",
            HTTP_X_BOOKING_BUSINESS_ID=str(self.hotel.core_business_id),
        )

        self.assertEqual(response.status_code, 201, response.data)
        booking = Booking.objects.get(id=response.data["data"]["booking"]["id"])
        guest = booking.guests.get(is_primary=True)
        document = guest.identity_documents.get(document_type="identity_photo")
        self.assertEqual(document.document_number, "12/ABC(N)123456")
        self.assertTrue(response.data["data"]["verification"]["identity_documents_complete"])

    def test_walk_in_v2_accepts_bracket_notation_guest_form_data(self):
        room = PhysicalRoom.objects.create(hotel=self.hotel, room_type=self.room_type, room_number="308")
        response = self.client.post(
            "/api/v1/admin/walk-in-booking-v2/",
            {
                "physical_room_id": room.id,
                "rate_plan_id": self.rate_plan.id,
                "check_in": str(self.check_in),
                "check_out": str(self.check_out),
                "contact_name": "Mg Mg",
                "contact_phone": "094444444",
                "guest_market": "local",
                "adults": 2,
                "children": 0,
                "guest[0][name]": "Mg Mg",
                "guest[0][nrc_number]": "12/ABC(N)123456",
                "guest[0][identity_type]": "nrc",
                "guest[0][identity_number]": "12/ABC(N)123456",
                "guest[0][is_primary]": "true",
                "guest[0][photo]": SimpleUploadedFile(
                    "mg-mg.jpg", b"first-image", content_type="image/jpeg"
                ),
                "guest[1][name]": "Su Su",
                "guest[1][nrc_number]": "12/ABC(N)654321",
                "guest[1][identity_type]": "nrc",
                "guest[1][identity_number]": "12/ABC(N)654321",
                "guest[1][is_primary]": "false",
                "guest[1][photo]": SimpleUploadedFile(
                    "su-su.jpg", b"second-image", content_type="image/jpeg"
                ),
                "payment[payment_type]": "deposit",
                "payment[provider]": "cash",
                "payment[status]": "paid",
                "payment[amount]": "100.00",
            },
            format="multipart",
            HTTP_X_BOOKING_ADMIN_KEY="test-admin-key",
            HTTP_X_BOOKING_BUSINESS_ID=str(self.hotel.core_business_id),
        )

        self.assertEqual(response.status_code, 201, response.data)
        booking = Booking.objects.get(id=response.data["data"]["booking"]["id"])
        self.assertEqual(booking.guests.count(), 2)
        self.assertEqual(booking.guests.filter(identity_documents__document_type="identity_photo").count(), 2)
        self.assertTrue(booking.guests.get(name="Mg Mg").is_primary)
        payment = booking.payments.get()
        self.assertEqual(payment.payment_type, Payment.Type.DEPOSIT)
        self.assertEqual(payment.amount, Decimal("100.00"))

    def test_public_booking_estimate_includes_paid_meal_plan_supplement(self):
        meal_plan = MealPlan.objects.create(
            hotel=self.hotel,
            core_meal_plan_id=403,
            name="Breakfast",
            included_meals=["breakfast"],
            local_base_price=Decimal("20000"),
            foreign_base_price=Decimal("30000"),
        )
        link = RoomTypeMealPlan.objects.create(
            room_type=self.room_type,
            meal_plan=meal_plan,
            is_included=False,
            is_guest_selectable=True,
        )
        payload = {
            "core_business_id": self.hotel.core_business_id,
            "check_in": str(self.check_in),
            "check_out": str(self.check_out),
            "guest_market": "local",
            "rooms": [{
                "core_room_type_id": self.room_type.core_room_type_id,
                "rate_plan_id": self.rate_plan.id,
                "meal_plan_link_id": link.id,
                "quantity": 2,
                "adults": 4,
                "children": 0,
            }],
        }

        response = self.client.post("/api/v1/public/bookings/estimate/", payload, format="json")

        self.assertEqual(response.status_code, 200, response.data)
        data = response.data["data"]
        self.assertEqual(data["grand_total"], Decimal("400000"))
        self.assertEqual(data["rooms"][0]["meal_plan_total"], Decimal("80000"))
        self.assertEqual(data["rooms"][0]["meal_plan"]["name"], "Breakfast")
        self.assertEqual(data["summary_items"][1]["type"], "meal_plan")

    def test_global_availability_returns_rooms_from_multiple_hotels(self):
        second_hotel = Hotel.objects.create(
            core_business_id=88,
            name="Yangon Riverside Hotel",
            address="Yangon",
        )
        second_room_type = RoomType.objects.create(
            hotel=second_hotel,
            core_room_type_id=401,
            name="River View Room",
            max_adults=2,
            max_children=1,
            max_occupancy=3,
            default_inventory=2,
        )
        RatePlan.objects.create(
            room_type=second_room_type,
            code="local-standard",
            name="Local Standard",
            guest_market=RatePlan.GuestMarket.LOCAL,
            currency="MMK",
            default_price=Decimal("90000"),
        )
        response = self.client.get(
            "/api/v1/public/search/availability/",
            {
                "check_in": self.check_in,
                "check_out": self.check_out,
                "adults": 2,
                "children": 0,
                "guest_market": "local",
            },
        )
        self.assertEqual(response.status_code, 200, response.data)
        results = response.data["data"]["results"]
        self.assertEqual(response.data["data"]["pagination"]["total"], 2)
        self.assertEqual(
            {item["hotel"]["core_business_id"] for item in results},
            {77, 88},
        )
        self.assertNotIn("access_snapshot", results[0]["hotel"])
        second_result = next(item for item in results if item["hotel"]["core_business_id"] == 88)
        self.assertEqual(second_result["room_types"][0]["rate_plans"][0]["total"], Decimal("180000"))

    def test_global_availability_can_search_business_name_or_address(self):
        response = self.client.get(
            "/api/v1/public/search/availability/",
            {
                "check_in": self.check_in,
                "check_out": self.check_out,
                "adults": 2,
                "guest_market": "local",
                "q": "Boston",
            },
        )
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["data"]["pagination"]["total"], 1)
        self.assertEqual(response.data["data"]["results"][0]["hotel"]["core_business_id"], 77)

    def test_global_availability_validates_date_range(self):
        response = self.client.get(
            "/api/v1/public/search/availability/",
            {"check_in": self.check_out, "check_out": self.check_in},
        )
        self.assertEqual(response.status_code, 400)

    def test_public_add_ons_only_returns_active_hotel_items(self):
        AddOn.objects.create(hotel=self.hotel, code="airport-pickup", name="Airport Pickup", price=Decimal("30000"))
        response = self.client.get(f"/api/v1/public/hotels/{self.hotel.core_business_id}/add-ons/")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["data"][0]["code"], "airport-pickup")

    def test_admin_add_on_templates_and_default_schema(self):
        headers = {
            "HTTP_X_BOOKING_ADMIN_KEY": "test-admin-key",
            "HTTP_X_BOOKING_BUSINESS_ID": str(self.hotel.core_business_id),
        }
        templates = self.client.get("/api/v1/admin/add-on-templates/", **headers)
        self.assertEqual(templates.status_code, 200, templates.data)
        airport = next(item for item in templates.data["data"] if item["type"] == "airport_pickup")
        self.assertEqual(airport["configuration_schema"]["fields"][0]["key"], "airport_name")

        created = self.client.post(
            "/api/v1/admin/add-ons/",
            {
                "hotel": self.hotel.id,
                "service_type": "airport_pickup",
                "code": "airport-transfer",
                "name": "Airport Transfer",
                "pricing_unit": "per_booking",
                "price": "30000.00",
                "currency": "MMK",
            },
            format="json",
            **headers,
        )
        self.assertEqual(created.status_code, 201, created.data)
        self.assertEqual(created.data["data"]["configuration_schema"], airport["configuration_schema"])

    def test_booking_validates_required_add_on_configuration(self):
        add_on = AddOn.objects.create(
            hotel=self.hotel,
            service_type="airport_pickup",
            code="airport-transfer",
            name="Airport Transfer",
            pricing_unit=AddOn.PricingUnit.PER_BOOKING,
            price=Decimal("30000"),
            configuration_schema=AddOnTemplate.objects.get(code="airport_pickup", version=1).configuration_schema,
        )
        payload = self.payload()
        payload["check_in"] = str(payload["check_in"])
        payload["check_out"] = str(payload["check_out"])
        payload["add_ons"] = [{"add_on_id": add_on.id, "quantity": 1, "configuration": {}}]
        invalid = self.client.post("/api/v1/public/bookings/", payload, format="json")
        self.assertEqual(invalid.status_code, 400, invalid.data)
        self.assertTrue(any("airport_name" in error for error in invalid.data["error"]))

        payload["add_ons"][0]["configuration"] = {
            "airport_name": "Yangon International Airport",
            "flight_number": "8M-301",
            "arrival_date": str(self.check_in),
            "arrival_time": "10:30",
        }
        valid = self.client.post("/api/v1/public/bookings/", payload, format="json")
        self.assertEqual(valid.status_code, 201, valid.data)
        self.assertEqual(valid.data["data"]["add_on_total"], "30000.00")

    def test_hotel_requests_template_and_superadmin_approves_it(self):
        hotel_headers = {
            "HTTP_X_BOOKING_ADMIN_KEY": "test-admin-key",
            "HTTP_X_BOOKING_BUSINESS_ID": str(self.hotel.core_business_id),
            "HTTP_X_CORE_USER_ID": "501",
        }
        requested = self.client.post(
            "/api/v1/admin/add-on-template-requests/",
            {
                "requested_name": "Spa Appointment",
                "description": "Let guests select a spa time.",
                "suggested_pricing_units": ["per_unit"],
                "suggested_schema": {
                    "version": 1,
                    "fields": [{"key": "appointment_time", "label": "Appointment Time", "type": "time", "required": True}],
                },
            },
            format="json",
            **hotel_headers,
        )
        self.assertEqual(requested.status_code, 201, requested.data)
        request_id = requested.data["data"]["id"]
        self.assertEqual(requested.data["data"]["core_business_id"], self.hotel.core_business_id)
        self.assertEqual(requested.data["data"]["requested_by_core_user_id"], 501)

        approved = self.client.post(
            f"/api/v1/superadmin/add-on-template-requests/{request_id}/approve/",
            {"code": "spa-appointment", "admin_note": "Approved for all hotels."},
            format="json",
            HTTP_AUTHORIZATION=f"Bearer {self.core_access_token()}",
        )
        self.assertEqual(approved.status_code, 200, approved.data)
        self.assertEqual(approved.data["data"]["status"], AddOnTemplateRequest.Status.APPROVED)
        template = AddOnTemplate.objects.get(code="spa-appointment", status=AddOnTemplate.Status.PUBLISHED)
        self.assertEqual(template.version, 1)
        self.assertEqual(template.created_by_core_user_id, 9001)

    def test_superadmin_publishes_new_template_version(self):
        original = AddOnTemplate.objects.get(code="airport_pickup", version=1)
        created = self.client.post(
            "/api/v1/superadmin/add-on-templates/",
            {
                "code": "airport_pickup",
                "name": "Airport Pickup v2",
                "allowed_pricing_units": ["per_booking"],
                "configuration_schema": original.configuration_schema,
            },
            format="json",
            HTTP_AUTHORIZATION=f"Bearer {self.core_access_token()}",
        )
        self.assertEqual(created.status_code, 201, created.data)
        self.assertEqual(created.data["data"]["version"], 2)
        self.assertEqual(created.data["data"]["status"], AddOnTemplate.Status.DRAFT)
        published = self.client.post(
            f"/api/v1/superadmin/add-on-templates/{created.data['data']['id']}/publish/",
            {},
            format="json",
            HTTP_AUTHORIZATION=f"Bearer {self.core_access_token()}",
        )
        self.assertEqual(published.status_code, 200, published.data)
        original.refresh_from_db()
        self.assertEqual(original.status, AddOnTemplate.Status.ARCHIVED)

    def test_booking_superadmin_rejects_non_superadmin_core_token(self):
        response = self.client.get(
            "/api/v1/superadmin/add-on-template-requests/",
            HTTP_AUTHORIZATION=f"Bearer {self.core_access_token(is_superuser=False, user_id=501)}",
        )
        self.assertEqual(response.status_code, 403, response.data)

    def test_public_booking_flow_and_private_detail_token(self):
        payload = self.payload()
        payload["check_in"] = str(payload["check_in"])
        payload["check_out"] = str(payload["check_out"])
        response = self.client.post("/api/v1/public/bookings/", payload, format="json", HTTP_IDEMPOTENCY_KEY="api-key")
        self.assertEqual(response.status_code, 201, response.data)
        token = response.data["data"]["public_token"]
        detail = self.client.get(f"/api/v1/public/bookings/{token}/")
        self.assertEqual(detail.status_code, 200)
        self.assertEqual(detail.data["data"]["contact_name"], "Myo Myo")

    @override_settings(DEMO_PAYMENT_ENABLED=True)
    def test_demo_payment_confirms_booking_and_reserves_inventory(self):
        payload = self.payload()
        payload["check_in"] = str(payload["check_in"])
        payload["check_out"] = str(payload["check_out"])
        created = self.client.post("/api/v1/public/bookings/", payload, format="json")
        token = created.data["data"]["public_token"]

        paid = self.client.post(f"/api/v1/public/bookings/{token}/demo-payment/", {}, format="json")
        self.assertEqual(paid.status_code, 201, paid.data)
        self.assertEqual(paid.data["data"]["booking"]["status"], Booking.Status.CONFIRMED)
        self.assertEqual(paid.data["data"]["payment"]["status"], Payment.Status.PAID)
        self.assertEqual(list(DailyInventory.objects.values_list("held_rooms", flat=True)), [0, 0])
        self.assertEqual(list(DailyInventory.objects.values_list("reserved_rooms", flat=True)), [2, 2])

        duplicate = self.client.post(f"/api/v1/public/bookings/{token}/demo-payment/", {}, format="json")
        self.assertEqual(duplicate.status_code, 200)
        self.assertTrue(duplicate.data["duplicate"])

    @override_settings(DEMO_PAYMENT_ENABLED=False)
    def test_demo_payment_is_hidden_when_disabled(self):
        response = self.client.post(f"/api/v1/public/bookings/{uuid.uuid4()}/demo-payment/", {}, format="json")
        self.assertEqual(response.status_code, 404)

    @override_settings(CORE_SERVICE_KEY="core-service-key")
    @patch("booking.views.CoreClient.post")
    def test_public_aya_payment_starts_core_checkout(self, core_post):
        core_post.return_value = {
            "checkout_type": "form_post",
            "checkout_url": "https://core.test/subscriptions/billing/aya/checkout/AYA-BKG-1/",
        }
        payload = self.payload()
        payload["check_in"] = str(payload["check_in"])
        payload["check_out"] = str(payload["check_out"])
        created = self.client.post("/api/v1/public/bookings/", payload, format="json")
        token = created.data["data"]["public_token"]

        response = self.client.post(f"/api/v1/public/bookings/{token}/aya-payment/", {"channel": "MMQR", "method": "QR"}, format="json")
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["data"]["booking"]["status"], Booking.Status.PENDING_PAYMENT)
        self.assertEqual(response.data["data"]["checkout"]["checkout_type"], "form_post")
        core_post.assert_called_once()
        self.assertEqual(core_post.call_args.args[0], "direct-booking/hotel-bookings/aya-checkout/")
        self.assertEqual(core_post.call_args.args[1]["amount"], 320000)

    def test_core_payment_success_confirms_booking_and_is_idempotent(self):
        payload = self.payload()
        payload["check_in"] = str(payload["check_in"])
        payload["check_out"] = str(payload["check_out"])
        created = self.client.post("/api/v1/public/bookings/", payload, format="json")
        booking_id = created.data["data"]["id"]
        public_token = created.data["data"]["public_token"]

        core_payload = {
            "booking_id": booking_id,
            "booking_public_token": public_token,
            "business_id": self.hotel.core_business_id,
            "payment": {
                "provider": "aya",
                "status": "success",
                "amount": "320000",
                "currency": "MMK",
                "payment_reference": "AYA-BKG-test-001",
                "transaction_id": "AYA-TXN-001",
            },
            "aya": {"status_code": "00", "transaction_id": "AYA-TXN-001"},
        }
        response = self.client.post(
            "/api/v1/admin/payments/core-success/",
            core_payload,
            format="json",
            HTTP_X_BOOKING_ADMIN_KEY="test-admin-key",
        )
        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(response.data["data"]["booking"]["status"], Booking.Status.CONFIRMED)
        self.assertEqual(response.data["data"]["payment"]["provider"], Payment.Provider.AYA)

        duplicate = self.client.post(
            "/api/v1/admin/payments/core-success/",
            core_payload,
            format="json",
            HTTP_X_BOOKING_ADMIN_KEY="test-admin-key",
        )
        self.assertEqual(duplicate.status_code, 200, duplicate.data)
        self.assertTrue(duplicate.data["duplicate"])
        self.assertEqual(Payment.objects.filter(provider_reference="AYA-BKG-test-001").count(), 1)

    def test_admin_api_requires_key(self):
        denied = self.client.get("/api/v1/admin/bookings/")
        allowed = self.client.get("/api/v1/admin/bookings/", HTTP_X_BOOKING_ADMIN_KEY="test-admin-key")
        self.assertEqual(denied.status_code, 403)
        self.assertEqual(allowed.status_code, 200)

    @override_settings(BOOKING_REQUIRE_BUSINESS_SCOPE=True)
    def test_production_admin_api_requires_business_scope(self):
        denied = self.client.get(
            "/api/v1/admin/rate-plans/",
            HTTP_X_BOOKING_ADMIN_KEY="test-admin-key",
        )
        allowed = self.client.get(
            "/api/v1/admin/rate-plans/",
            HTTP_X_BOOKING_ADMIN_KEY="test-admin-key",
            HTTP_X_BOOKING_BUSINESS_ID=str(self.hotel.core_business_id),
        )
        self.assertEqual(denied.status_code, 403)
        self.assertEqual(allowed.status_code, 200)

    def test_business_scope_filters_and_rejects_cross_hotel_write(self):
        other_hotel = Hotel.objects.create(core_business_id=88, name="Other Hotel")
        other_room_type = RoomType.objects.create(
            hotel=other_hotel,
            core_room_type_id=401,
            name="Other Room",
            max_adults=2,
            max_occupancy=2,
        )
        RatePlan.objects.create(
            room_type=other_room_type,
            code="other-local",
            name="Other Local",
            guest_market="local",
            default_price=Decimal("50000"),
        )
        headers = {
            "HTTP_X_BOOKING_ADMIN_KEY": "test-admin-key",
            "HTTP_X_BOOKING_BUSINESS_ID": str(self.hotel.core_business_id),
        }
        listed = self.client.get("/api/v1/admin/rate-plans/", **headers)
        self.assertEqual(listed.status_code, 200)
        self.assertEqual({row["room_type"] for row in listed.data["data"]}, {self.room_type.id})

        cross_write = self.client.post(
            "/api/v1/admin/rate-plans/",
            {
                "room_type": other_room_type.id,
                "code": "forbidden-plan",
                "name": "Forbidden",
                "guest_market": "local",
                "currency": "MMK",
                "default_price": "10000.00",
            },
            format="json",
            **headers,
        )
        self.assertEqual(cross_write.status_code, 400)

    def test_bulk_inventory_upsert_and_committed_room_guard(self):
        headers = {
            "HTTP_X_BOOKING_ADMIN_KEY": "test-admin-key",
            "HTTP_X_BOOKING_BUSINESS_ID": str(self.hotel.core_business_id),
        }
        payload = {
            "room_type_id": self.room_type.id,
            "start_date": str(self.check_in),
            "end_date": str(self.check_in + timedelta(days=2)),
            "total_rooms": 10,
            "stop_sell": False,
        }
        created = self.client.post(
            "/api/v1/admin/inventory/bulk-upsert/",
            payload,
            format="json",
            **headers,
        )
        self.assertEqual(created.status_code, 200, created.data)
        self.assertEqual(created.data["data"]["created"], 3)
        self.assertEqual(DailyInventory.objects.filter(room_type=self.room_type, total_rooms=10).count(), 3)

        first = DailyInventory.objects.get(room_type=self.room_type, stay_date=self.check_in)
        first.reserved_rooms = 2
        first.save(update_fields=["reserved_rooms"])
        payload["total_rooms"] = 1
        rejected = self.client.post(
            "/api/v1/admin/inventory/bulk-upsert/",
            payload,
            format="json",
            **headers,
        )
        self.assertEqual(rejected.status_code, 400)
        first.refresh_from_db()
        self.assertEqual(first.total_rooms, 10)

    def test_room_board_aggregates_reserved_oos_and_unassigned_rooms(self):
        room_801 = PhysicalRoom.objects.create(
            hotel=self.hotel,
            room_type=self.room_type,
            core_physical_room_id=801,
            core_building_id=7001,
            core_floor_id=8008,
            room_number="801",
            building="Main Building",
            floor="8",
        )
        PhysicalRoom.objects.create(
            hotel=self.hotel,
            room_type=self.room_type,
            core_physical_room_id=802,
            core_building_id=7001,
            core_floor_id=8008,
            room_number="802",
            building="Main Building",
            floor="8",
            status=PhysicalRoom.Status.OUT_OF_SERVICE,
        )
        room_803 = PhysicalRoom.objects.create(
            hotel=self.hotel,
            room_type=self.room_type,
            core_physical_room_id=803,
            core_building_id=7001,
            core_floor_id=8008,
            room_number="803",
            building="Main Building",
            floor="8",
        )
        payload = self.payload()
        payload["rooms"][0]["quantity"] = 1
        payload["rooms"][0]["adults"] = 2
        booking, _ = create_booking(payload)
        record_payment(booking, {"provider": "cash", "amount": booking.grand_total, "status": Payment.Status.PAID})
        booking.refresh_from_db()
        future_payload = self.payload()
        future_payload["check_in"] = self.check_in + timedelta(days=2)
        future_payload["check_out"] = self.check_in + timedelta(days=3)
        future_payload["rooms"][0]["quantity"] = 1
        future_payload["rooms"][0]["adults"] = 2
        future_booking, _ = create_booking(future_payload)
        record_payment(future_booking, {"provider": "cash", "amount": future_booking.grand_total, "status": Payment.Status.PAID})
        future_booking.refresh_from_db()
        RoomAssignment.objects.filter(booking_room__booking=future_booking).delete()
        RoomAssignment.objects.create(booking_room=future_booking.rooms.get(), physical_room=room_803)
        headers = {
            "HTTP_X_BOOKING_ADMIN_KEY": "test-admin-key",
            "HTTP_X_BOOKING_BUSINESS_ID": str(self.hotel.core_business_id),
        }
        board = self.client.get(
            "/api/v1/admin/room-board/",
            {"date": str(self.check_in), "building_id": 7001},
            **headers,
        )
        self.assertEqual(board.status_code, 200, board.data)
        summary = board.data["data"]["summary"]
        self.assertEqual(summary["reserved"], 1)
        self.assertEqual(summary["out_of_service"], 1)
        self.assertEqual(summary["unassigned_bookings"], 0)
        self.assertEqual(board.data["data"]["floors"][0]["building_id"], 7001)
        self.assertEqual(board.data["data"]["floors"][0]["floor_id"], 8008)
        reserved = next(room for room in board.data["data"]["rooms"] if room["display_status"] == "reserved")
        self.assertEqual(reserved["building_id"], 7001)
        self.assertEqual(reserved["floor_id"], 8008)
        self.assertEqual(reserved["room_type"]["name"], self.room_type.name)
        self.assertEqual(reserved["room_type"]["price"]["base_price"], Decimal("80000"))
        self.assertEqual(reserved["room_type"]["price"]["currency"], "MMK")
        self.assertEqual(reserved["assignment"]["booking_reference"], booking.reference)
        self.assertEqual(reserved["assignment"]["payment_status"], "paid")
        self.assertEqual(reserved["timeline"]["text"], "Reserved: 2 Nights")
        available_with_next = next(room for room in board.data["data"]["rooms"] if room["room_number"] == "803")
        self.assertEqual(available_with_next["display_status"], "available")
        self.assertEqual(available_with_next["timeline"]["text"], "Vacant: 2 Days | Reserved: 1 Night")
        self.assertEqual(available_with_next["timeline"]["next_reserved"]["booking_reference"], future_booking.reference)

    def test_physical_room_detail_includes_current_booking(self):
        room = PhysicalRoom.objects.create(
            hotel=self.hotel,
            room_type=self.room_type,
            core_physical_room_id=901,
            room_number="303",
            building="Main Building",
            floor="3",
            core_snapshot={"room_view": {"name": "City View"}, "room_area": 301, "area_unit": "sqft"},
        )
        payload = self.payload()
        payload["rooms"][0]["quantity"] = 1
        payload["rooms"][0]["adults"] = 2
        payload["rooms"][0]["children"] = 1
        booking, _ = create_booking(payload)
        record_payment(booking, {"provider": "cash", "amount": booking.grand_total, "status": Payment.Status.PAID})
        headers = {
            "HTTP_X_BOOKING_ADMIN_KEY": "test-admin-key",
            "HTTP_X_BOOKING_BUSINESS_ID": str(self.hotel.core_business_id),
        }

        response = self.client.get(
            f"/api/v1/admin/physical-rooms/{room.id}/",
            {"date": str(self.check_in)},
            **headers,
        )

        self.assertEqual(response.status_code, 200, response.data)
        data = response.data["data"]
        self.assertEqual(data["room_number"], "303")
        self.assertEqual(data["display_status"], "reserved")
        self.assertEqual(data["room_type"]["name"], self.room_type.name)
        self.assertEqual(data["current_booking"]["reference"], booking.reference)
        self.assertEqual(data["current_booking"]["payment_status"], "paid")
        self.assertEqual(data["current_booking"]["guest_count"], {"adults": 2, "children": 1, "total": 3})
        self.assertEqual(data["current_booking"]["amount"]["grand_total"], booking.grand_total)
        guest_data = data["current_booking"]["primary_guest"]
        self.assertIn("nrc_number", guest_data)
        self.assertIn("passport_number", guest_data)
        self.assertIn("identity_type", guest_data)
        self.assertIn("identity_number", guest_data)
        self.assertIn("is_primary", guest_data)
        self.assertIn("documents", guest_data)

    def test_assignment_requires_confirmed_booking_and_vacant_room(self):
        room = PhysicalRoom.objects.create(
            hotel=self.hotel,
            room_type=self.room_type,
            room_number="801",
            status=PhysicalRoom.Status.OUT_OF_SERVICE,
        )
        booking, _ = create_booking(self.payload())
        headers = {
            "HTTP_X_BOOKING_ADMIN_KEY": "test-admin-key",
            "HTTP_X_BOOKING_BUSINESS_ID": str(self.hotel.core_business_id),
        }
        pending = self.client.post(
            f"/api/v1/admin/bookings/{booking.id}/assign-room/",
            {"booking_room_id": booking.rooms.get().id, "physical_room_id": room.id},
            format="json",
            **headers,
        )
        self.assertEqual(pending.status_code, 400)
        record_payment(booking, {"provider": "cash", "amount": booking.grand_total, "status": Payment.Status.PAID})
        not_vacant = self.client.post(
            f"/api/v1/admin/bookings/{booking.id}/assign-room/",
            {"booking_room_id": booking.rooms.get().id, "physical_room_id": room.id},
            format="json",
            **headers,
        )
        self.assertEqual(not_vacant.status_code, 400)

    def test_cancel_confirmed_booking_releases_room_assignment(self):
        room = PhysicalRoom.objects.create(
            hotel=self.hotel,
            room_type=self.room_type,
            room_number="801",
        )
        booking, _ = create_booking(self.payload())
        record_payment(booking, {"provider": "cash", "amount": booking.grand_total, "status": Payment.Status.PAID})
        booking.refresh_from_db()
        assignment = RoomAssignment.objects.create(booking_room=booking.rooms.get(), physical_room=room)
        canceled = cancel_booking(booking)
        assignment.refresh_from_db()
        self.assertEqual(canceled.status, Booking.Status.CANCELED)
        self.assertIsNotNone(assignment.released_at)

    def test_integration_status_reports_synced_catalog_counts(self):
        PhysicalRoom.objects.create(hotel=self.hotel, room_type=self.room_type, room_number="801")
        response = self.client.get(
            "/api/v1/admin/integration-status/",
            HTTP_X_BOOKING_ADMIN_KEY="test-admin-key",
            HTTP_X_BOOKING_BUSINESS_ID=str(self.hotel.core_business_id),
        )
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["data"]["status"], "active")
        self.assertEqual(response.data["data"]["counts"]["room_types"], 1)
        self.assertEqual(response.data["data"]["counts"]["physical_rooms"], 1)

    def test_admin_can_create_update_and_deactivate_custom_rate_plan(self):
        headers = {"HTTP_X_BOOKING_ADMIN_KEY": "test-admin-key"}
        created = self.client.post(
            "/api/v1/admin/rate-plans/",
            {
                "room_type": self.room_type.id,
                "code": "local-no-refund",
                "name": "Local No Refund",
                "guest_market": "local",
                "currency": "MMK",
                "default_price": "70000.00",
                "extra_bed_price": "20000.00",
                "breakfast_included": False,
                "refundable": False,
                "cancellation_policy": {"type": "non_refundable"},
                "is_active": True,
            },
            format="json",
            **headers,
        )
        self.assertEqual(created.status_code, 201, created.data)
        created_data = created.data["data"]
        self.assertEqual(created_data["source"], RatePlan.Source.BOOKING)
        self.assertFalse(created_data["is_default"])

        updated = self.client.patch(
            f"/api/v1/admin/rate-plans/{created_data['id']}/",
            {"default_price": "75000.00"},
            format="json",
            **headers,
        )
        self.assertEqual(updated.status_code, 200, updated.data)
        self.assertEqual(updated.data["data"]["default_price"], "75000.00")

        deleted = self.client.delete(
            f"/api/v1/admin/rate-plans/{created_data['id']}/",
            **headers,
        )
        self.assertEqual(deleted.status_code, 204)
        custom = RatePlan.objects.get(id=created_data["id"])
        self.assertFalse(custom.is_active)

    def test_core_generated_rate_plan_is_read_only(self):
        core_plan = RatePlan.objects.create(
            room_type=self.room_type,
            core_rate_plan_id="room-301-core-default",
            source=RatePlan.Source.CORE,
            is_default=True,
            code="core-default",
            name="Core Default",
            guest_market="local",
            currency="MMK",
            default_price=Decimal("80000"),
        )
        headers = {"HTTP_X_BOOKING_ADMIN_KEY": "test-admin-key"}
        updated = self.client.patch(
            f"/api/v1/admin/rate-plans/{core_plan.id}/",
            {"default_price": "90000.00"},
            format="json",
            **headers,
        )
        deleted = self.client.delete(
            f"/api/v1/admin/rate-plans/{core_plan.id}/",
            **headers,
        )
        self.assertEqual(updated.status_code, 400)
        self.assertEqual(deleted.status_code, 400)

    def test_bulk_upsert_daily_rates_for_inclusive_date_range(self):
        start_date = self.check_in
        end_date = self.check_in + timedelta(days=2)
        payload = {
            "rate_plan_id": self.rate_plan.id,
            "start_date": str(start_date),
            "end_date": str(end_date),
            "price": "120000.00",
            "min_stay": 2,
            "closed_to_arrival": False,
            "closed_to_departure": False,
        }
        created = self.client.post(
            "/api/v1/admin/rates/bulk-upsert/",
            payload,
            format="json",
            HTTP_X_BOOKING_ADMIN_KEY="test-admin-key",
        )
        self.assertEqual(created.status_code, 200, created.data)
        self.assertEqual(created.data["data"]["created"], 3)
        self.assertEqual(created.data["data"]["updated"], 0)

        payload["price"] = "150000.00"
        updated = self.client.post(
            "/api/v1/admin/rates/bulk-upsert/",
            payload,
            format="json",
            HTTP_X_BOOKING_ADMIN_KEY="test-admin-key",
        )
        self.assertEqual(updated.data["data"]["created"], 0)
        self.assertEqual(updated.data["data"]["updated"], 3)
        self.assertEqual(
            DailyRate.objects.filter(rate_plan=self.rate_plan, price=Decimal("150000.00")).count(),
            3,
        )

    def test_rate_period_api_rejects_overlapping_active_periods(self):
        headers = {"HTTP_X_BOOKING_ADMIN_KEY": "test-admin-key"}
        first = self.client.post(
            "/api/v1/admin/rate-periods/",
            {
                "rate_plan": self.rate_plan.id,
                "name": "High Season",
                "start_date": str(self.check_in),
                "end_date": str(self.check_out + timedelta(days=10)),
                "price": "120000.00",
            },
            format="json",
            **headers,
        )
        self.assertEqual(first.status_code, 201, first.data)
        overlap = self.client.post(
            "/api/v1/admin/rate-periods/",
            {
                "rate_plan": self.rate_plan.id,
                "name": "Overlapping Season",
                "start_date": str(self.check_in + timedelta(days=1)),
                "end_date": str(self.check_out + timedelta(days=20)),
                "price": "140000.00",
            },
            format="json",
            **headers,
        )
        self.assertEqual(overlap.status_code, 400)

    def test_daily_rate_requires_price(self):
        response = self.client.post(
            "/api/v1/admin/rates/",
            {
                "rate_plan": self.rate_plan.id,
                "stay_date": str(self.check_in),
            },
            format="json",
            HTTP_X_BOOKING_ADMIN_KEY="test-admin-key",
        )
        self.assertEqual(response.status_code, 400)
        self.assertTrue(any(item.startswith("Price -") for item in response.data["error"]))

    @patch("booking.views.sync_business_from_core")
    def test_core_activation_event_is_idempotent(self, sync_business):
        sync_business.return_value = {"hotel_id": self.hotel.id}
        event_id = uuid.uuid4()
        payload = {"event_id": str(event_id), "event_type": "direct_booking.activated", "business_id": 77}
        first = self.client.post("/api/v1/admin/core-events/", payload, format="json", HTTP_X_BOOKING_ADMIN_KEY="test-admin-key")
        second = self.client.post("/api/v1/admin/core-events/", payload, format="json", HTTP_X_BOOKING_ADMIN_KEY="test-admin-key")
        self.assertEqual(first.status_code, 200)
        self.assertTrue(second.data["duplicate"])
        sync_business.assert_called_once_with(77)
        self.assertTrue(CoreIntegrationEvent.objects.filter(event_id=event_id).exists())

    def test_core_revoke_event_deprovisions_hotel(self):
        payload = {"event_id": str(uuid.uuid4()), "event_type": "direct_booking.revoked", "business_id": 77}
        response = self.client.post("/api/v1/admin/core-events/", payload, format="json", HTTP_X_BOOKING_ADMIN_KEY="test-admin-key")
        self.assertEqual(response.status_code, 200)
        self.hotel.refresh_from_db()
        self.room_type.refresh_from_db()
        self.assertFalse(self.hotel.is_active)
        self.assertFalse(self.room_type.booking_enabled)


class DemoSeedCommandTests(TestCase):
    def test_seed_demo_data_is_idempotent_and_builds_complete_flow(self):
        output = StringIO()
        call_command("seed_demo_data", stdout=output)
        call_command("seed_demo_data", stdout=output)

        hotel = Hotel.objects.get(core_business_id=990001)
        room_type = RoomType.objects.get(hotel=hotel, core_room_type_id=990001 * 100 + 1)
        self.assertEqual(Hotel.objects.filter(core_business_id__in=[990001, 990002, 990003]).count(), 3)
        self.assertEqual(PhysicalRoom.objects.filter(hotel=hotel).count(), 10)
        self.assertEqual(room_type.rate_plans.count(), 3)
        self.assertEqual(hotel.meal_plans.count(), 5)
        self.assertEqual(room_type.meal_plan_links.count(), 5)
        self.assertTrue(room_type.meal_plan_links.filter(meal_plan__included_meals=["breakfast"], is_included=True, is_default=True).exists())
        self.assertEqual(room_type.rate_plans.filter(source=RatePlan.Source.CORE, is_default=True).count(), 2)
        self.assertEqual(room_type.rate_plans.filter(source=RatePlan.Source.BOOKING, is_default=False).count(), 1)
        self.assertEqual(Booking.objects.filter(hotel=hotel, reference__startswith="DEMO-").count(), 4)
        self.assertEqual(Booking.objects.get(reference="DEMO-UNASSIGNED-001").rooms.get().meal_plan_snapshot["name"], "Half Board")
        self.assertTrue(RatePeriod.objects.filter(rate_plan__room_type=room_type, name="Demo High Season").exists())
        self.assertTrue(DailyRate.objects.filter(rate_plan__room_type=room_type).exists())
        self.assertEqual(PhysicalRoom.objects.get(hotel=hotel, room_number="807").status, PhysicalRoom.Status.OCCUPIED)
        self.assertEqual(PhysicalRoom.objects.get(hotel=hotel, room_number="806").status, PhysicalRoom.Status.CLEANING)
        self.assertEqual(PhysicalRoom.objects.get(hotel=hotel, room_number="810").status, PhysicalRoom.Status.OUT_OF_SERVICE)
        self.assertIn("Visit77 Direct Booking demo data is ready", output.getvalue())
