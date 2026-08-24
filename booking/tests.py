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
from rest_framework.exceptions import ValidationError
from rest_framework_simplejwt.backends import TokenBackend

from booking.models import AddOn, AddOnTemplate, AddOnTemplateRequest, Booking, BookingRoom, CoreIntegrationEvent, DailyInventory, DailyRate, Hotel, Invoice, MealPlan, Payment, PhysicalRoom, PhysicalRoomActionHistory, PhysicalRoomBlock, RatePlan, RatePeriod, RoomAssignment, RoomType, RoomTypeMealPlan
from booking.integrations.core import sync_business_from_core
from booking.services import auto_assign_physical_rooms_for_booking, auto_cancel_no_show_reservations, availability_for_hotel, cancel_booking, create_admin_reservation, create_booking, create_invoice, create_walk_in_booking, ensure_daily_inventory_for_room_type, record_payment, refund_payment, refund_quote


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

    def test_ota_auto_assignment_uses_lowest_available_room_number_first_fit(self):
        rooms = [
            PhysicalRoom.objects.create(
                hotel=self.hotel,
                room_type=self.room_type,
                room_number=str(number),
                ota_enabled=True,
                ota_sale_open=True,
            )
            for number in [1, 2, 3, 4]
        ]
        start = date.today() + timedelta(days=1)

        def create_confirmed_ota(reference, check_in, check_out):
            booking = Booking.objects.create(
                reference=reference,
                hotel=self.hotel,
                status=Booking.Status.CONFIRMED,
                source=Booking.Source.OTA,
                check_in=check_in,
                check_out=check_out,
                contact_name=reference,
                contact_phone="091111111",
            )
            BookingRoom.objects.create(
                booking=booking,
                room_type=self.room_type,
                rate_plan=self.rate_plan,
            )
            assignments = auto_assign_physical_rooms_for_booking(booking)
            self.assertEqual(len(assignments), 1)
            return assignments[0].physical_room

        for offset in range(4):
            assigned_room = create_confirmed_ota(
                f"OTA-SHORT-{offset}",
                start + timedelta(days=offset),
                start + timedelta(days=offset + 1),
            )
            self.assertEqual(assigned_room.id, rooms[0].id)

        long_stay_room = create_confirmed_ota(
            "OTA-LONG-STAY",
            start,
            start + timedelta(days=5),
        )
        self.assertEqual(long_stay_room.id, rooms[1].id)

    def test_ota_auto_assignment_excludes_closed_and_non_ota_rooms(self):
        PhysicalRoom.objects.create(
            hotel=self.hotel,
            room_type=self.room_type,
            room_number="1",
            ota_enabled=True,
            ota_sale_open=False,
        )
        PhysicalRoom.objects.create(
            hotel=self.hotel,
            room_type=self.room_type,
            room_number="2",
            ota_enabled=False,
            ota_sale_open=True,
        )
        expected = PhysicalRoom.objects.create(
            hotel=self.hotel,
            room_type=self.room_type,
            room_number="3",
            ota_enabled=True,
            ota_sale_open=True,
        )
        booking = Booking.objects.create(
            reference="OTA-ELIGIBLE-ROOM",
            hotel=self.hotel,
            status=Booking.Status.CONFIRMED,
            source=Booking.Source.OTA,
            check_in=date.today() + timedelta(days=1),
            check_out=date.today() + timedelta(days=2),
            contact_name="OTA Guest",
            contact_phone="091111111",
        )
        BookingRoom.objects.create(
            booking=booking,
            room_type=self.room_type,
            rate_plan=self.rate_plan,
        )

        assignments = auto_assign_physical_rooms_for_booking(booking)

        self.assertEqual(len(assignments), 1)
        self.assertEqual(assignments[0].physical_room_id, expected.id)

    def test_pms_walk_in_can_use_ota_disabled_room_in_ota_pms_hotel(self):
        self.hotel.package = Hotel.Package.OTA_PMS
        self.hotel.save(update_fields=["package"])
        room = PhysicalRoom.objects.create(
            hotel=self.hotel,
            room_type=self.room_type,
            room_number="PMS-WALK-IN",
            ota_enabled=False,
        )

        booking = create_walk_in_booking(
            {
                "physical_room_id": room.id,
                "rate_plan_id": self.rate_plan.id,
                "check_in": self.check_in,
                "check_out": self.check_out,
                "contact_name": "PMS Walk-in Guest",
                "contact_phone": "091111111",
                "guest_market": "local",
                "adults": 1,
                "children": 0,
                "guests": [{"name": "PMS Walk-in Guest", "is_primary": True}],
            },
            core_business_id=self.hotel.core_business_id,
            check_in_immediately=False,
        )

        self.assertEqual(booking.status, Booking.Status.CONFIRMED)
        self.assertTrue(RoomAssignment.objects.filter(
            booking_room__booking=booking,
            physical_room=room,
        ).exists())
        self.assertTrue(all(
            total == 1
            for total in DailyInventory.objects.filter(
                room_type=self.room_type,
                stay_date__gte=self.check_in,
                stay_date__lt=self.check_out,
            ).values_list("total_rooms", flat=True)
        ))

    def test_pms_on_call_reservation_can_use_ota_disabled_room(self):
        self.hotel.package = Hotel.Package.OTA_PMS
        self.hotel.save(update_fields=["package"])
        room = PhysicalRoom.objects.create(
            hotel=self.hotel,
            room_type=self.room_type,
            room_number="PMS-ON-CALL",
            ota_enabled=False,
        )
        booking = create_admin_reservation(
            {
                "core_business_id": self.hotel.core_business_id,
                "source": Booking.Source.PHONE,
                "source_name": "Hotel hotline",
                "check_in": self.check_in,
                "check_out": self.check_out,
                "contact_name": "On-call Guest",
                "contact_phone": "092222222",
                "guest_market": "local",
                "rooms": [{
                    "core_room_type_id": self.room_type.core_room_type_id,
                    "rate_plan_id": self.rate_plan.id,
                    "quantity": 1,
                    "adults": 1,
                    "children": 0,
                    "extra_beds": 0,
                    "physical_room_ids": [room.id],
                }],
                "guests": [{"name": "On-call Guest", "is_primary": True}],
                "add_ons": [],
            },
            core_business_id=self.hotel.core_business_id,
        )

        self.assertEqual(booking.status, Booking.Status.CONFIRMED)
        self.assertTrue(RoomAssignment.objects.filter(
            booking_room__booking=booking,
            physical_room=room,
        ).exists())

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
        reservation_events = PhysicalRoomActionHistory.objects.filter(
            booking=booking,
            action=PhysicalRoomActionHistory.Action.RESERVED,
        )
        self.assertEqual(reservation_events.count(), 2)
        self.assertTrue(all(event.actor_type == "guest" for event in reservation_events))
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
        room_802 = PhysicalRoom.objects.create(
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

    def test_auto_cancel_no_show_releases_assignment_without_room_history(self):
        room = PhysicalRoom.objects.create(
            hotel=self.hotel,
            room_type=self.room_type,
            room_number="NO-SHOW-1",
        )
        booking, _ = create_booking(self.payload())
        record_payment(booking, {
            "provider": "cash",
            "amount": booking.grand_total,
            "status": Payment.Status.PAID,
        })
        booking.refresh_from_db()
        assignment = RoomAssignment.objects.create(
            booking_room=booking.rooms.first(),
            physical_room=room,
        )
        history_count_before_cancel = PhysicalRoomActionHistory.objects.filter(
            physical_room=room,
        ).count()

        canceled_count = auto_cancel_no_show_reservations(as_of=booking.check_in + timedelta(days=1))

        booking.refresh_from_db()
        assignment.refresh_from_db()
        self.assertEqual(canceled_count, 1)
        self.assertEqual(booking.status, Booking.Status.CANCELED)
        self.assertIsNotNone(assignment.released_at)
        self.assertEqual(
            PhysicalRoomActionHistory.objects.filter(physical_room=room).count(),
            history_count_before_cancel,
        )

    def test_auto_cancel_no_show_does_not_cancel_today_arrival(self):
        booking, _ = create_booking(self.payload())
        record_payment(booking, {
            "provider": "cash",
            "amount": booking.grand_total,
            "status": Payment.Status.PAID,
        })

        canceled_count = auto_cancel_no_show_reservations(as_of=booking.check_in)

        booking.refresh_from_db()
        self.assertEqual(canceled_count, 0)
        self.assertEqual(booking.status, Booking.Status.CONFIRMED)

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

    def test_one_invoice_accepts_deposit_and_balance_as_separate_receipts(self):
        booking, _ = create_booking(self.payload())
        invoice = booking.invoices.get(invoice_type=Invoice.Type.ROOM_BOOKING)

        deposit = record_payment(booking, {
            "invoice_id": invoice.id,
            "payment_type": Payment.Type.DEPOSIT,
            "provider": Payment.Provider.CASH,
            "amount": Decimal("50000"),
            "status": Payment.Status.PAID,
        })
        invoice.refresh_from_db()
        self.assertEqual(invoice.status, Invoice.Status.PARTIALLY_PAID)
        self.assertEqual(invoice.balance, Decimal("270000"))

        balance = record_payment(booking, {
            "invoice_id": invoice.id,
            "payment_type": Payment.Type.BALANCE,
            "provider": Payment.Provider.MMQR,
            "amount": invoice.balance,
            "status": Payment.Status.PAID,
        })
        invoice.refresh_from_db()
        self.assertEqual(invoice.status, Invoice.Status.PAID)
        self.assertEqual(invoice.balance, Decimal("0"))
        self.assertEqual(invoice.receipts.count(), 2)
        self.assertEqual(deposit.invoice_number, invoice.invoice_number)
        self.assertEqual(balance.invoice_number, invoice.invoice_number)
        self.assertNotEqual(deposit.receipt_number, balance.receipt_number)

    def test_new_charge_creates_separate_invoice_with_own_tax_and_discount(self):
        booking, _ = create_booking(self.payload())
        initial_invoice = booking.invoices.get(invoice_type=Invoice.Type.ROOM_BOOKING)

        extra_invoice = create_invoice(
            booking,
            Invoice.Type.EXTRA_SERVICE,
            [{"description": "Airport transfer", "quantity": 1, "unit_price": Decimal("30000")}],
            tax_total=Decimal("1500"),
            discount_total=Decimal("500"),
            add_to_booking_total=True,
        )

        booking.refresh_from_db()
        self.assertNotEqual(initial_invoice.invoice_number, extra_invoice.invoice_number)
        self.assertEqual(extra_invoice.subtotal, Decimal("30000"))
        self.assertEqual(extra_invoice.total, Decimal("31000"))
        self.assertEqual(booking.invoices.count(), 2)
        self.assertEqual(booking.grand_total, Decimal("351000"))

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
            room_number = "801"

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
                        },
                        {
                            "id": 502,
                            "name": "Half Board Package",
                            "description": "Breakfast and dinner.",
                            "plan_type": "package",
                            "package_pricing_mode": "sum_default_prices",
                            "components": [{"id": 501, "name": "Breakfast"}, {"id": 503, "name": "Dinner"}],
                            "effective_included_meals": ["breakfast", "dinner"],
                            "effective_meal_windows": {
                                "breakfast": {"start": "06:30", "end": "10:00"},
                                "dinner": {"start": "18:00", "end": "21:00"},
                            },
                            "effective_local_base_price": 100000,
                            "effective_local_usd_display_price": 25,
                            "effective_foreign_base_price": 130000,
                            "effective_foreign_usd_display_price": 33,
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
                            "room_no": self.room_number,
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
        package = hotel.meal_plans.get(core_meal_plan_id=502)
        self.assertEqual(package.plan_type, "package")
        self.assertEqual(package.local_base_price, Decimal("100000"))
        self.assertEqual(package.included_meals, ["breakfast", "dinner"])
        self.assertEqual(len(package.components), 2)
        room_type = RoomType.objects.get(hotel__core_business_id=99, core_room_type_id=901)
        # PMS inventory includes all active rooms; OTA availability applies the
        # separate ota_enabled selection when serving public inventory.
        self.assertEqual(room_type.default_inventory, 2)
        self.assertEqual(DailyInventory.objects.filter(room_type=room_type, total_rooms=2).count(), 3)
        room = PhysicalRoom.objects.get(core_physical_room_id=9901)
        self.assertFalse(room.ota_enabled)
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
        client.room_number = "801-A"
        sync_business_from_core(99, client=client)

        renamed_room = PhysicalRoom.objects.get(
            hotel=hotel,
            core_physical_room_id=9901,
        )
        self.assertEqual(renamed_room.room_number, "801-A")
        self.assertEqual(
            PhysicalRoom.objects.filter(hotel=hotel, core_physical_room_id=9901).count(),
            1,
        )

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
    def test_physical_room_history_records_status_block_and_checkout_actions(self):
        room = PhysicalRoom.objects.create(
            hotel=self.hotel,
            room_type=self.room_type,
            room_number="HIST-01",
            core_physical_room_id=9129,
        )
        headers = {
            "HTTP_X_BOOKING_ADMIN_KEY": "test-admin-key",
            "HTTP_X_BOOKING_BUSINESS_ID": str(self.hotel.core_business_id),
            "HTTP_X_CORE_USER_ID": "501",
            "HTTP_X_CORE_USER_NAME": "Staff One",
        }

        oos = self.client.patch(
            f"/api/v1/admin/physical-rooms/{room.id}/",
            {"status": "out_of_service", "note": "Air conditioner repair"},
            format="json",
            **headers,
        )
        self.assertEqual(oos.status_code, 200, oos.data)
        room.refresh_from_db()
        oos_event = room.action_history.get(action="out_of_service_started")
        self.assertEqual(oos_event.actor_type, "hotel_admin")
        self.assertEqual(oos_event.actor_core_user_id, 501)
        self.assertEqual(oos_event.note, "Air conditioner repair")

        restored = self.client.patch(
            f"/api/v1/admin/physical-rooms/{room.id}/",
            {"status": "vacant", "note": "Repair completed"},
            format="json",
            **headers,
        )
        self.assertEqual(restored.status_code, 200, restored.data)
        block_response = self.client.post(
            "/api/v1/admin/room-blocks/",
            {
                "physical_room": room.id,
                "start_date": str(self.check_out + timedelta(days=1)),
                "end_date": str(self.check_out + timedelta(days=2)),
                "note": "Held for VIP",
            },
            format="json",
            **headers,
        )
        self.assertEqual(block_response.status_code, 201, block_response.data)
        unblock_response = self.client.post(
            f"/api/v1/admin/room-blocks/{block_response.data['data']['id']}/unblock/",
            {}, format="json", **headers,
        )
        self.assertEqual(unblock_response.status_code, 200, unblock_response.data)

        booking = create_walk_in_booking(
            {
                "physical_room_id": room.id,
                "rate_plan_id": self.rate_plan.id,
                "check_in": self.check_in,
                "check_out": self.check_out,
                "contact_name": "History Guest",
                "contact_phone": "091111111",
                "guest_market": "local",
                "adults": 1,
                "children": 0,
            },
            core_business_id=self.hotel.core_business_id,
            check_in_immediately=True,
        )
        checkout = self.client.post(
            f"/api/v1/admin/bookings/{booking.id}/check-out/", {}, format="json", **headers,
        )
        self.assertEqual(checkout.status_code, 200, checkout.data)

        history = self.client.get(
            f"/api/v1/admin/physical-rooms/{room.core_physical_room_id}/history/", **headers,
        )
        self.assertEqual(history.status_code, 200, history.data)
        actions = [item["action"] for item in history.data["data"]]
        self.assertIn("out_of_service", actions)
        self.assertIn("oos_repaired", actions)
        self.assertIn("block_created", actions)
        self.assertIn("unblocked", actions)
        self.assertIn("checked_out", actions)
        self.assertNotIn("cleaning_started", actions)
        checked_out = next(item for item in history.data["data"] if item["action"] == "checked_out")
        self.assertEqual(checked_out["booking_reference"], booking.reference)
        self.assertEqual(checked_out["guest_name"], "History Guest")
        self.assertEqual(checked_out["actor"]["core_user_id"], 501)
        self.assertEqual(checked_out["actor"]["name"], "Staff One")
        self.assertEqual(checked_out["guest"]["name"], "History Guest")
        self.assertTrue(checked_out["created_at"].endswith("Z"), checked_out["created_at"])

        board = self.client.get(
            "/api/v1/admin/room-board/", {"date": str(self.check_out)}, **headers,
        )
        board_room = next(item for item in board.data["data"]["rooms"] if item["id"] == room.id)
        self.assertEqual(board_room["display_status"], "cleaning")
        self.assertIn("Cleaning | Checked out:", board_room["timeline_text"])

        vacant = self.client.patch(
            f"/api/v1/admin/physical-rooms/{room.id}/",
            {"status": "vacant", "note": "Available"},
            format="json",
            **headers,
        )
        self.assertEqual(vacant.status_code, 200, vacant.data)
        latest_event = room.action_history.order_by("-created_at", "-id").first()
        self.assertEqual(latest_event.action, PhysicalRoomActionHistory.Action.CLEANING_COMPLETED)
        self.assertEqual(latest_event.old_status, PhysicalRoom.Status.CLEANING)
        self.assertEqual(latest_event.new_status, PhysicalRoom.Status.VACANT)

        history = self.client.get(
            f"/api/v1/admin/physical-rooms/{room.core_physical_room_id}/history/", **headers,
        )
        cleaned = next(item for item in history.data["data"] if item["action"] == "cleaned")
        self.assertEqual(cleaned["raw_action"], "cleaning_completed")
        self.assertEqual(cleaned["action_label"], "Cleaned")

        raw_history = self.client.get(
            f"/api/v1/admin/physical-rooms/{room.core_physical_room_id}/history/",
            {"include_system_events": "true"},
            **headers,
        )
        self.assertIn("cleaning_started", [
            item["raw_action"] for item in raw_history.data["data"]
        ])

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
        self.assertEqual(
            board_room["timeline_text"],
            f"Blocked: {self.check_in.isoformat()} to {self.check_out.isoformat()}",
        )
        self.assertEqual(board_room["timeline"]["block"]["start_date"], str(self.check_in))
        self.assertEqual(board_room["timeline"]["block"]["end_date"], str(self.check_out))
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

    def test_checkout_day_shows_occupied_until_checkout_then_blocked(self):
        room = PhysicalRoom.objects.create(
            hotel=self.hotel,
            room_type=self.room_type,
            room_number="BLOCK-AFTER-STAY",
        )
        booking = create_walk_in_booking(
            {
                "physical_room_id": room.id,
                "rate_plan_id": self.rate_plan.id,
                "check_in": self.check_in,
                "check_out": self.check_out,
                "contact_name": "Checkout Guest",
                "contact_phone": "091111111",
                "guest_market": "local",
                "adults": 1,
                "children": 0,
                "guests": [{"name": "Checkout Guest", "is_primary": True}],
                "payment": {"provider": "cash", "status": "paid", "payment_type": "full_payment"},
            },
            core_business_id=self.hotel.core_business_id,
            check_in_immediately=True,
        )
        headers = {
            "HTTP_X_BOOKING_ADMIN_KEY": "test-admin-key",
            "HTTP_X_BOOKING_BUSINESS_ID": str(self.hotel.core_business_id),
        }
        block = self.client.post(
            "/api/v1/admin/room-blocks/",
            {
                "physical_room": room.id,
                "start_date": str(self.check_out),
                "end_date": str(self.check_out + timedelta(days=2)),
                "note": "VIP hold after checkout",
            },
            format="json",
            **headers,
        )
        self.assertEqual(block.status_code, 201, block.data)

        before_checkout = self.client.get(
            "/api/v1/admin/room-board/", {"date": str(self.check_out)}, **headers,
        )
        before_room = next(item for item in before_checkout.data["data"]["rooms"] if item["id"] == room.id)
        self.assertEqual(before_room["display_status"], "occupied")
        self.assertEqual(str(before_room["current_booking"]["id"]), str(booking.id))
        self.assertEqual(before_room["block_status"], "currently_blocked")

        checkout = self.client.post(
            f"/api/v1/admin/bookings/{booking.id}/check-out/", {}, format="json", **headers,
        )
        self.assertEqual(checkout.status_code, 200, checkout.data)

        after_checkout = self.client.get(
            "/api/v1/admin/room-board/", {"date": str(self.check_out)}, **headers,
        )
        after_room = next(item for item in after_checkout.data["data"]["rooms"] if item["id"] == room.id)
        self.assertEqual(after_room["operational_status"], PhysicalRoom.Status.CLEANING)
        self.assertEqual(after_room["display_status"], "blocked")
        self.assertIsNone(after_room["current_booking"])
        self.assertEqual(after_room["timeline"]["type"], "blocked")

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
        later_block = PhysicalRoomBlock.objects.create(
            physical_room=room,
            start_date=future_end + timedelta(days=2),
            end_date=future_end + timedelta(days=4),
            note="Later maintenance hold",
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
        self.assertEqual(
            [item["id"] for item in today_room["upcoming_blocks"]],
            [block.id, later_block.id],
        )
        self.assertEqual(today_room["block_timeline"]["days_until_block"], 2)
        self.assertEqual(today_room["block_timeline"]["blocked_days"], 6)

        future_board = self.client.get("/api/v1/admin/room-board/", {"date": str(future_start)}, **headers)
        future_room = next(item for item in future_board.data["data"]["rooms"] if item["id"] == room.id)
        self.assertEqual(future_room["display_status"], "blocked")
        self.assertEqual(future_room["block_status"], "currently_blocked")
        self.assertEqual(future_room["current_block"]["id"], block.id)
        self.assertEqual(
            [item["id"] for item in future_room["upcoming_blocks"]],
            [later_block.id],
        )

        detail = self.client.get(
            f"/api/v1/admin/physical-rooms/{room.id}/",
            {"date": str(self.check_in)},
            **headers,
        )
        self.assertEqual(detail.status_code, 200, detail.data)
        self.assertEqual(
            [item["id"] for item in detail.data["data"]["upcoming_blocks"]],
            [block.id, later_block.id],
        )

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
        self.rate_plan.extra_bed_base_price = Decimal("10000")
        self.rate_plan.save(update_fields=["extra_bed_base_price"])
        add_on = AddOn.objects.create(
            hotel=self.hotel,
            code="check-in-service",
            name="Check-in Service",
            pricing_unit=AddOn.PricingUnit.PER_BOOKING,
            price=Decimal("30000"),
            currency="MMK",
        )
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
                "add_ons": [{"add_on_id": add_on.id, "quantity": 1, "configuration": {}}],
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
        self.assertEqual(response.data["data"]["payment_summary"]["room_total"], Decimal("270000"))
        self.assertEqual(response.data["data"]["payment_summary"]["add_on_total"], Decimal("30000"))
        self.assertEqual(response.data["data"]["payment_summary"]["grand_total"], Decimal("300000"))
        self.assertEqual(response.data["data"]["payment_summary"]["amount_due"], Decimal("300000"))
        self.assertEqual(booking_room.assignments.get().physical_room, room)
        self.assertEqual(booking.guests.count(), 2)
        self.assertTrue(booking.guests.filter(name="Updated Primary", identity_number="NRC-1").exists())
        self.assertTrue(booking.guests.filter(name="Second Guest", identity_number="PP-2").exists())
        invoice = booking.invoices.get(invoice_type=Invoice.Type.ROOM_BOOKING)
        self.assertEqual(invoice.total, Decimal("300000"))
        self.assertEqual(invoice.lines.count(), 2)

    def test_stay_bill_lists_separate_invoices_and_receipts(self):
        booking, _ = create_booking(self.payload())
        headers = {
            "HTTP_X_BOOKING_ADMIN_KEY": "test-admin-key",
            "HTTP_X_BOOKING_BUSINESS_ID": str(self.hotel.core_business_id),
        }
        created = self.client.post(
            f"/api/v1/admin/bookings/{booking.id}/invoices/",
            {
                "invoice_type": "extra_service",
                "lines": [{"description": "Laundry", "quantity": "2", "unit_price": "5000"}],
                "tax_total": "500",
                "discount_total": "0",
                "note": "Laundry service",
            },
            format="json",
            **headers,
        )
        self.assertEqual(created.status_code, 201, created.data)
        extra_invoice_id = created.data["data"]["id"]

        paid = self.client.post(
            f"/api/v1/admin/bookings/{booking.id}/payment/",
            {
                "invoice_id": extra_invoice_id,
                "payment_type": "full_payment",
                "provider": "cash",
                "status": "paid",
            },
            format="json",
            **headers,
        )
        self.assertEqual(paid.status_code, 201, paid.data)
        self.assertEqual(paid.data["data"]["amount"], "10500.00")

        admin_bill = self.client.get(f"/api/v1/admin/bookings/{booking.id}/stay-bill/", **headers)
        self.assertEqual(admin_bill.status_code, 200, admin_bill.data)
        self.assertEqual(len(admin_bill.data["data"]["invoices"]), 2)
        extra = next(item for item in admin_bill.data["data"]["invoices"] if item["id"] == extra_invoice_id)
        self.assertEqual(extra["status"], Invoice.Status.PAID)
        self.assertEqual(len(extra["receipts"]), 1)

        public_bill = self.client.get(f"/api/v1/public/bookings/{booking.public_token}/stay-bill/")
        self.assertEqual(public_bill.status_code, 200, public_bill.data)
        self.assertEqual(len(public_bill.data["data"]["invoices"]), 2)

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
        room.refresh_from_db()
        self.assertEqual(booking.rooms.get().assignments.get().physical_room, room)
        self.assertEqual(room.status, PhysicalRoom.Status.VACANT)
        self.assertEqual(booking.guests.count(), 1)
        guest = booking.guests.get()
        self.assertEqual(guest.name, "Updated Existing Guest")
        self.assertEqual(guest.nrc_number, "8/MAMANA(N)123465")

        checked_in = self.client.post(
            f"/api/v1/admin/bookings/{booking.id}/check-in/",
            {"verification_confirmed": True},
            format="json",
            **headers,
        )
        self.assertEqual(checked_in.status_code, 200, checked_in.data)
        booking.refresh_from_db()
        room.refresh_from_db()
        self.assertEqual(booking.status, Booking.Status.CHECKED_IN)
        self.assertEqual(room.status, PhysicalRoom.Status.OCCUPIED)

    def test_check_in_form_reports_actual_checked_in_booking_conflict(self):
        room = PhysicalRoom.objects.create(hotel=self.hotel, room_type=self.room_type, room_number="303-C")
        PhysicalRoom.objects.create(hotel=self.hotel, room_type=self.room_type, room_number="303-C2")
        PhysicalRoom.objects.create(hotel=self.hotel, room_type=self.room_type, room_number="303-C3")
        occupied_booking = create_walk_in_booking(
            {
                "physical_room_id": room.id,
                "rate_plan_id": self.rate_plan.id,
                "check_in": self.check_in,
                "check_out": self.check_out,
                "contact_name": "Existing Stay",
                "contact_phone": "091111111",
                "guest_market": "local",
                "adults": 1,
                "children": 0,
                "guests": [{"name": "Existing Stay", "identity_number": "NRC-OLD", "is_primary": True}],
            },
            core_business_id=self.hotel.core_business_id,
            check_in_immediately=True,
        )
        reserved_booking, _ = create_booking(self.payload())
        record_payment(reserved_booking, {
            "payment_type": Payment.Type.FULL_PAYMENT,
            "provider": Payment.Provider.CASH,
            "amount": reserved_booking.grand_total,
            "status": Payment.Status.PAID,
        }, auto_assign=False)
        reserved_booking.refresh_from_db()
        RoomAssignment.objects.create(
            booking_room=reserved_booking.rooms.get(), physical_room=room,
        )
        headers = {
            "HTTP_X_BOOKING_ADMIN_KEY": "test-admin-key",
            "HTTP_X_BOOKING_BUSINESS_ID": str(self.hotel.core_business_id),
        }

        response = self.client.patch(
            f"/api/v1/admin/bookings/{reserved_booking.id}/check-in-form/",
            {"physical_room_id": room.id},
            format="json",
            **headers,
        )

        self.assertEqual(response.status_code, 400, response.data)
        conflict = response.data["data"]["conflict_bookings"][0]
        self.assertEqual(conflict["booking_id"], str(occupied_booking.id))
        self.assertEqual(conflict["reference"], occupied_booking.reference)
        self.assertEqual(conflict["status"], "occupied")

        board = self.client.get(
            "/api/v1/admin/room-board/", {"date": str(self.check_in)}, **headers,
        )
        board_room = next(item for item in board.data["data"]["rooms"] if item["id"] == room.id)
        self.assertEqual(board_room["display_status"], "occupied")
        self.assertEqual(str(board_room["current_booking"]["id"]), str(occupied_booking.id))
        self.assertEqual(
            [item["booking_reference"] for item in board_room["next_reservations"]],
            [reserved_booking.reference],
        )

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

    def test_checkout_releases_daily_inventory_for_room_to_be_resold(self):
        room = PhysicalRoom.objects.create(hotel=self.hotel, room_type=self.room_type, room_number="304-R")
        booking = create_walk_in_booking(
            {
                "physical_room_id": room.id,
                "rate_plan_id": self.rate_plan.id,
                "check_in": self.check_in,
                "check_out": self.check_out,
                "contact_name": "First Guest",
                "contact_phone": "091111111",
                "guest_market": "local",
                "adults": 1,
                "children": 0,
                "guests": [{"name": "First Guest", "is_primary": True}],
            },
            core_business_id=self.hotel.core_business_id,
            check_in_immediately=True,
        )
        self.assertEqual(
            list(DailyInventory.objects.order_by("stay_date").values_list("reserved_rooms", flat=True)),
            [1, 1],
        )
        headers = {
            "HTTP_X_BOOKING_ADMIN_KEY": "test-admin-key",
            "HTTP_X_BOOKING_BUSINESS_ID": str(self.hotel.core_business_id),
        }

        checked_out = self.client.post(
            f"/api/v1/admin/bookings/{booking.id}/check-out/", {}, format="json", **headers,
        )

        self.assertEqual(checked_out.status_code, 200, checked_out.data)
        room.refresh_from_db()
        self.assertEqual(room.status, PhysicalRoom.Status.CLEANING)
        self.assertEqual(
            list(DailyInventory.objects.order_by("stay_date").values_list("reserved_rooms", flat=True)),
            [0, 0],
        )
        room.status = PhysicalRoom.Status.VACANT
        room.save(update_fields=["status"])
        # Simulate counters left by an older deployment. Walk-in creation must
        # reconcile them from active booking statuses before checking capacity.
        DailyInventory.objects.update(reserved_rooms=1)

        second_booking = self.client.post(
            "/api/v1/admin/walk-in-booking-v2/",
            {
                "workflow": "direct_check_in",
                "physical_room_id": room.id,
                "rate_plan_id": self.rate_plan.id,
                "check_in": str(self.check_in),
                "check_out": str(self.check_out),
                "contact_name": "Second Guest",
                "contact_phone": "092222222",
                "guest_market": "local",
                "adults": 1,
                "children": 0,
                "guests": [{"name": "Second Guest", "is_primary": True}],
            },
            format="json",
            **headers,
        )
        self.assertEqual(second_booking.status_code, 201, second_booking.data)
        self.assertEqual(
            list(DailyInventory.objects.order_by("stay_date").values_list("reserved_rooms", flat=True)),
            [1, 1],
        )

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
        self.room_type.core_snapshot = {
            "amenities": [{"id": 1, "name": "Wi-Fi"}],
            "facilities": [{"id": 2, "name": "Air Conditioning"}],
            "photos": [{"id": 3, "image": "https://media.example.com/double-room.jpg"}],
            "room_standard": {"id": 4, "name": "Deluxe"},
            "bed_type": {"id": 5, "name": "King Bed"},
            "room_area": 320,
            "area_unit": "sqft",
            "local_base_price": "80000.00",
            "local_base_currency": "MMK",
            "local_usd_display_price": "20.00",
            "foreign_base_price": "100000.00",
            "foreign_base_currency": "MMK",
            "foreign_usd_display_price": "25.00",
        }
        self.room_type.save(update_fields=["core_snapshot"])
        self.rate_plan.is_default = True
        self.rate_plan.usd_display_price = Decimal("20")
        self.rate_plan.extra_bed_base_price = Decimal("30000")
        self.rate_plan.extra_bed_usd_display_price = Decimal("8")
        self.rate_plan.save(update_fields=[
            "is_default", "usd_display_price", "extra_bed_base_price", "extra_bed_usd_display_price",
        ])
        response = self.client.get(
            f"/api/v1/public/hotels/{self.hotel.core_business_id}/availability/",
            {"check_in": self.check_in, "check_out": self.check_out, "adults": 2, "guest_market": "local"},
        )
        self.assertEqual(response.status_code, 200)
        room_type = response.data["data"]["room_types"][0]
        self.assertEqual(room_type["available_rooms"], 3)
        self.assertEqual(room_type["amenities"], [{"id": 1, "name": "Wi-Fi"}])
        self.assertEqual(room_type["photos"][0]["id"], 3)
        self.assertEqual(room_type["room_standard"], {"id": 4, "name": "Deluxe"})
        self.assertEqual(room_type["default_price"]["base_price"], Decimal("80000"))
        self.assertEqual(room_type["default_price"]["base_currency"], "MMK")
        self.assertEqual(room_type["default_rate_plan"]["id"], self.rate_plan.id)
        self.assertTrue(room_type["rate_plans"][0]["is_default"])
        self.assertEqual(room_type["extra_bed_price"]["base_price"], Decimal("30000"))
        self.assertEqual(room_type["extra_bed_price"]["base_currency"], "MMK")
        self.assertEqual(room_type["extra_bed_price"]["usd_display_price"], Decimal("8"))
        self.assertEqual(room_type["extra_bed_price"]["pricing_unit"], "per_bed_per_night")
        self.assertEqual(room_type["extra_bed_base_price"], Decimal("30000"))
        self.assertEqual(room_type["rate_plans"][0]["extra_bed_base_price"], Decimal("30000"))

    def test_public_ota_room_type_catalog_only_returns_ota_selected_room_types_without_availability(self):
        self.room_type.core_snapshot = {
            "photos": [{"id": 3, "image": "https://media.example.com/double-room.jpg"}],
            "amenities": [{"id": 1, "name": "Wi-Fi"}],
            "room_view": [{"id": 4, "name": "City View"}],
            "beds": [{"id": 5, "name": "King Bed", "quantity": 1}],
        }
        self.room_type.save(update_fields=["core_snapshot"])
        self.rate_plan.is_default = True
        self.rate_plan.save(update_fields=["is_default"])
        PhysicalRoom.objects.create(
            hotel=self.hotel,
            room_type=self.room_type,
            room_number="OTA-CATALOG-1",
            ota_enabled=True,
        )
        PhysicalRoom.objects.create(
            hotel=self.hotel,
            room_type=self.room_type,
            room_number="OTA-CATALOG-2",
            ota_enabled=False,
        )
        hidden_room_type = RoomType.objects.create(
            hotel=self.hotel,
            core_room_type_id=9991,
            name="PMS-only Room Type",
        )
        RatePlan.objects.create(
            room_type=hidden_room_type,
            code="hidden-local",
            name="Hidden Local",
            guest_market=RatePlan.GuestMarket.LOCAL,
            default_price=Decimal("50000"),
        )
        PhysicalRoom.objects.create(
            hotel=self.hotel,
            room_type=hidden_room_type,
            room_number="PMS-ONLY-1",
            ota_enabled=False,
        )

        response = self.client.get(
            f"/api/v1/public/hotels/{self.hotel.core_business_id}/ota-room-types/",
            {"guest_market": "local", "display_currency": "MMK"},
        )

        self.assertEqual(response.status_code, 200, response.data)
        payload = response.data["data"]
        self.assertFalse(payload["availability_calculated"])
        self.assertEqual(len(payload["room_types"]), 1)
        room_type = payload["room_types"][0]
        self.assertEqual(room_type["core_room_type_id"], self.room_type.core_room_type_id)
        self.assertEqual(room_type["ota_enabled_room_count"], 1)
        self.assertFalse(room_type["availability_calculated"])
        self.assertNotIn("available_rooms", room_type)
        self.assertNotIn("room_list", room_type)
        self.assertEqual(room_type["amenities"][0]["name"], "Wi-Fi")
        self.assertEqual(room_type["default_price"]["rate_plan_id"], self.rate_plan.id)

    def test_public_ota_room_type_catalog_rejects_pms_only_hotel(self):
        self.hotel.package = Hotel.Package.PMS
        self.hotel.save(update_fields=["package"])

        response = self.client.get(
            f"/api/v1/public/hotels/{self.hotel.core_business_id}/ota-room-types/",
        )

        self.assertEqual(response.status_code, 404, response.data)

    def test_ota_room_selection_controls_public_availability(self):
        rooms = [
            PhysicalRoom.objects.create(
                hotel=self.hotel,
                room_type=self.room_type,
                core_physical_room_id=700 + index,
                room_number=f"OTA-{index}",
            )
            for index in range(1, 4)
        ]
        headers = {
            "HTTP_X_BOOKING_ADMIN_KEY": "test-admin-key",
            "HTTP_X_BOOKING_BUSINESS_ID": str(self.hotel.core_business_id),
        }

        updated = self.client.put(
            "/api/v1/admin/ota-rooms/selection/",
            {
                "selected_room_ids": [rooms[0].id],
                "deselected_room_ids": [rooms[1].id, rooms[2].id],
            },
            format="json",
            **headers,
        )
        self.assertEqual(updated.status_code, 200, updated.data)
        self.assertEqual(updated.data["data"]["total_rooms"], 3)
        self.assertEqual(updated.data["data"]["total_ota_rooms"], 1)
        self.assertEqual(updated.data["data"]["selected_room_ids"], [rooms[0].id])
        room_rows = updated.data["data"]["room_types"][0]["rooms"]
        self.assertTrue(next(item for item in room_rows if item["physical_room_id"] == rooms[0].id)["is_ota_selected"])
        self.assertEqual(
            next(item for item in room_rows if item["physical_room_id"] == rooms[1].id)["ota_sale_status"],
            "not_selected",
        )

        availability = self.client.get(
            f"/api/v1/public/hotels/{self.hotel.core_business_id}/availability/",
            {"check_in": self.check_in, "check_out": self.check_out, "adults": 2, "guest_market": "local"},
        )
        self.assertEqual(availability.status_code, 200, availability.data)
        self.assertEqual(availability.data["data"]["room_types"][0]["available_rooms"], 1)

        history = self.client.get(
            f"/api/v1/admin/physical-rooms/{rooms[1].core_physical_room_id}/history/",
            **headers,
        )
        self.assertEqual(history.status_code, 200, history.data)
        self.assertTrue(history.data["data"][0]["metadata"]["ota_selection_changed"])
        self.assertFalse(history.data["data"][0]["metadata"]["ota_enabled"])

    def test_ota_room_records_are_sorted_and_close_open_controls_sale(self):
        room = PhysicalRoom.objects.create(
            hotel=self.hotel,
            room_type=self.room_type,
            core_physical_room_id=799,
            room_number="OTA-TIMELINE",
            ota_enabled=True,
            ota_sale_open=True,
        )
        today = timezone.localdate()

        def assigned_booking(reference, check_in, check_out, status=Booking.Status.CONFIRMED):
            booking = Booking.objects.create(
                reference=reference,
                hotel=self.hotel,
                status=status,
                source=Booking.Source.OTA,
                check_in=check_in,
                check_out=check_out,
                contact_name=reference,
                contact_phone="091111111",
                grand_total=Decimal("80000"),
            )
            booking_room = BookingRoom.objects.create(
                booking=booking,
                room_type=self.room_type,
                rate_plan=self.rate_plan,
                adults=2,
                children=1,
                total=Decimal("80000"),
            )
            assignment = RoomAssignment.objects.create(booking_room=booking_room, physical_room=room)
            return booking, assignment

        current, current_assignment = assigned_booking(
            "OTA-CURRENT", today, today + timedelta(days=1), Booking.Status.CHECKED_IN,
        )
        upcoming_near, upcoming_near_assignment = assigned_booking(
            "OTA-UPCOMING-NEAR", today + timedelta(days=2), today + timedelta(days=3),
        )
        upcoming_far, upcoming_far_assignment = assigned_booking(
            "OTA-UPCOMING-FAR", today + timedelta(days=5), today + timedelta(days=7),
        )
        past_near, past_near_assignment = assigned_booking(
            "OTA-PAST-NEAR", today - timedelta(days=2), today - timedelta(days=1), Booking.Status.CHECKED_OUT,
        )
        past_far, past_far_assignment = assigned_booking(
            "OTA-PAST-FAR", today - timedelta(days=8), today - timedelta(days=6), Booking.Status.CHECKED_OUT,
        )
        RoomAssignment.objects.filter(id__in=[past_near_assignment.id, past_far_assignment.id]).update(
            released_at=timezone.now(),
        )
        headers = {
            "HTTP_X_BOOKING_ADMIN_KEY": "test-admin-key",
            "HTTP_X_BOOKING_BUSINESS_ID": str(self.hotel.core_business_id),
        }

        response = self.client.get("/api/v1/admin/ota-rooms/selection/", **headers)

        self.assertEqual(response.status_code, 200, response.data)
        room_payload = response.data["data"]["room_types"][0]["rooms"][0]
        self.assertEqual(
            [item["booking_reference"] for item in room_payload["ota_records"]],
            ["OTA-CURRENT", "OTA-UPCOMING-NEAR", "OTA-UPCOMING-FAR", "OTA-PAST-NEAR", "OTA-PAST-FAR"],
        )
        self.assertEqual(
            [item["color"] for item in room_payload["ota_records"]],
            ["blue", "orange", "orange", "grey", "grey"],
        )
        self.assertEqual(room_payload["ota_record_summary"], {
            "all": 5,
            "active_today": 1,
            "upcoming": 2,
            "past": 2,
        })

        for timeline_status, expected_references in [
            ("active_today", ["OTA-CURRENT"]),
            ("upcoming", ["OTA-UPCOMING-NEAR", "OTA-UPCOMING-FAR"]),
            ("past", ["OTA-PAST-NEAR", "OTA-PAST-FAR"]),
        ]:
            filtered = self.client.get(
                "/api/v1/admin/ota-rooms/selection/",
                {"timeline_status": timeline_status},
                **headers,
            )
            self.assertEqual(filtered.status_code, 200, filtered.data)
            filtered_room = filtered.data["data"]["room_types"][0]["rooms"][0]
            self.assertEqual(filtered.data["data"]["applied_timeline_status"], timeline_status)
            self.assertEqual(filtered_room["applied_timeline_status"], timeline_status)
            self.assertEqual(
                [item["booking_reference"] for item in filtered_room["ota_records"]],
                expected_references,
            )
            self.assertEqual(filtered_room["ota_record_count"], len(expected_references))
            self.assertEqual(filtered_room["ota_record_total"], 5)

        close_conflict = self.client.post(
            f"/api/v1/admin/ota-rooms/{room.id}/sale-status/",
            {"action": "close", "note": "Manual stop sale"},
            format="json",
            **headers,
        )
        self.assertEqual(close_conflict.status_code, 400, close_conflict.data)
        self.assertEqual(len(close_conflict.data["data"]["conflict_bookings"]), 3)

        Booking.objects.filter(id__in=[current.id, upcoming_near.id, upcoming_far.id]).update(
            status=Booking.Status.CANCELED,
        )
        RoomAssignment.objects.filter(id__in=[
            current_assignment.id, upcoming_near_assignment.id, upcoming_far_assignment.id,
        ]).update(released_at=timezone.now())
        closed = self.client.post(
            f"/api/v1/admin/ota-rooms/{room.id}/sale-status/",
            {"action": "close", "note": "Manual stop sale"},
            format="json",
            **headers,
        )
        self.assertEqual(closed.status_code, 200, closed.data)
        room.refresh_from_db()
        self.assertFalse(room.ota_sale_open)

        opened = self.client.post(
            f"/api/v1/admin/ota-rooms/{room.id}/sale-status/",
            {"action": "open"},
            format="json",
            **headers,
        )
        self.assertEqual(opened.status_code, 200, opened.data)
        room.refresh_from_db()
        self.assertTrue(room.ota_sale_open)

    def test_admin_room_type_api_includes_physical_room_ota_selection_state(self):
        selected = PhysicalRoom.objects.create(
            hotel=self.hotel,
            room_type=self.room_type,
            core_physical_room_id=781,
            room_number="OTA-SELECTED",
            ota_enabled=True,
        )
        deselected = PhysicalRoom.objects.create(
            hotel=self.hotel,
            room_type=self.room_type,
            core_physical_room_id=782,
            room_number="OTA-DESELECTED",
            ota_enabled=False,
        )
        response = self.client.get(
            "/api/v1/admin/room-types/",
            HTTP_X_BOOKING_ADMIN_KEY="test-admin-key",
            HTTP_X_BOOKING_BUSINESS_ID=str(self.hotel.core_business_id),
        )
        self.assertEqual(response.status_code, 200, response.data)
        room_type = next(item for item in response.data["data"] if item["id"] == self.room_type.id)
        self.assertEqual(room_type["total_room_count"], 2)
        self.assertEqual(room_type["ota_enabled_room_count"], 1)
        self.assertEqual(len(room_type["room_list"]), 2)
        selected_data = next(
            item for item in room_type["room_list"] if item["physical_room_id"] == selected.id
        )
        deselected_data = next(
            item for item in room_type["room_list"] if item["physical_room_id"] == deselected.id
        )
        self.assertTrue(selected_data["ota_enabled"])
        self.assertTrue(selected_data["is_ota_selected"])
        self.assertEqual(selected_data["ota_sale_status"], "open")
        self.assertFalse(deselected_data["ota_enabled"])
        self.assertEqual(deselected_data["ota_sale_status"], "not_selected")
        self.assertEqual(
            [item["physical_room_id"] for item in room_type["room_list"]],
            [selected.id, deselected.id],
        )

        ota_only = self.client.get(
            "/api/v1/admin/room-types/",
            {"ota_enabled": "true"},
            HTTP_X_BOOKING_ADMIN_KEY="test-admin-key",
            HTTP_X_BOOKING_BUSINESS_ID=str(self.hotel.core_business_id),
        )
        self.assertEqual(ota_only.status_code, 200, ota_only.data)
        ota_room_type = next(item for item in ota_only.data["data"] if item["id"] == self.room_type.id)
        self.assertEqual(
            [item["physical_room_id"] for item in ota_room_type["room_list"]],
            [selected.id],
        )

        ota_only_alias = self.client.get(
            "/api/v1/admin/room-types/",
            {"ota_only": "true"},
            HTTP_X_BOOKING_ADMIN_KEY="test-admin-key",
            HTTP_X_BOOKING_BUSINESS_ID=str(self.hotel.core_business_id),
        )
        alias_room_type = next(
            item for item in ota_only_alias.data["data"] if item["id"] == self.room_type.id
        )
        self.assertEqual(
            [item["physical_room_id"] for item in alias_room_type["room_list"]],
            [selected.id],
        )

        disabled_only = self.client.get(
            "/api/v1/admin/room-types/",
            {"ota_enabled": "false"},
            HTTP_X_BOOKING_ADMIN_KEY="test-admin-key",
            HTTP_X_BOOKING_BUSINESS_ID=str(self.hotel.core_business_id),
        )
        self.assertEqual(disabled_only.status_code, 200, disabled_only.data)
        disabled_room_type = next(item for item in disabled_only.data["data"] if item["id"] == self.room_type.id)
        self.assertEqual(
            [item["physical_room_id"] for item in disabled_room_type["room_list"]],
            [deselected.id],
        )

        disabled_first = self.client.get(
            "/api/v1/admin/room-types/",
            {"ota_sort": "disabled_first"},
            HTTP_X_BOOKING_ADMIN_KEY="test-admin-key",
            HTTP_X_BOOKING_BUSINESS_ID=str(self.hotel.core_business_id),
        )
        self.assertEqual(disabled_first.status_code, 200, disabled_first.data)
        sorted_room_type = next(
            item for item in disabled_first.data["data"] if item["id"] == self.room_type.id
        )
        self.assertEqual(
            [item["physical_room_id"] for item in sorted_room_type["room_list"]],
            [deselected.id, selected.id],
        )

    def test_ota_room_deselect_rejects_capacity_below_existing_commitments(self):
        room = PhysicalRoom.objects.create(
            hotel=self.hotel,
            room_type=self.room_type,
            core_physical_room_id=799,
            room_number="OTA-CONFLICT",
        )
        ensure_daily_inventory_for_room_type(self.room_type, start_date=self.check_in, days=2)
        payload = self.payload()
        payload["rooms"][0]["quantity"] = 1
        payload["rooms"][0]["adults"] = 2
        create_booking(payload)
        headers = {
            "HTTP_X_BOOKING_ADMIN_KEY": "test-admin-key",
            "HTTP_X_BOOKING_BUSINESS_ID": str(self.hotel.core_business_id),
        }

        response = self.client.put(
            "/api/v1/admin/ota-rooms/selection/",
            {"selected_room_ids": [], "deselected_room_ids": [room.id]},
            format="json",
            **headers,
        )
        self.assertEqual(response.status_code, 400, response.data)
        room.refresh_from_db()
        self.assertTrue(room.ota_enabled)
        self.assertIn("conflict_dates", response.data["data"])

    def test_pms_only_package_cannot_manage_ota_room_selection(self):
        self.hotel.package = Hotel.Package.PMS
        self.hotel.save(update_fields=["package"])
        response = self.client.get(
            "/api/v1/admin/ota-rooms/selection/",
            HTTP_X_BOOKING_ADMIN_KEY="test-admin-key",
            HTTP_X_BOOKING_BUSINESS_ID=str(self.hotel.core_business_id),
        )
        self.assertEqual(response.status_code, 403, response.data)

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
        self.assertTrue(response.data["data"]["verification"]["can_check_in"])

    def test_future_reservation_accepts_occupied_room_after_current_checkout(self):
        room = PhysicalRoom.objects.create(
            hotel=self.hotel,
            room_type=self.room_type,
            room_number="305-F",
        )
        current_booking = create_walk_in_booking(
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
            },
            core_business_id=self.hotel.core_business_id,
            check_in_immediately=True,
        )
        room.refresh_from_db()
        self.assertEqual(current_booking.status, Booking.Status.CHECKED_IN)
        self.assertEqual(room.status, PhysicalRoom.Status.OCCUPIED)

        overlapping_payload = self.payload()
        overlapping_payload.update({
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
        with self.assertRaises(ValidationError):
            create_admin_reservation(overlapping_payload)

        future_payload = self.payload()
        future_payload.update({
            "source": Booking.Source.PHONE,
            "check_in": self.check_out,
            "check_out": self.check_out + timedelta(days=2),
            "rooms": [{
                "core_room_type_id": self.room_type.core_room_type_id,
                "rate_plan_id": self.rate_plan.id,
                "quantity": 1,
                "adults": 1,
                "children": 0,
                "physical_room_ids": [room.id],
            }],
        })

        future_booking = create_admin_reservation(future_payload)

        self.assertEqual(future_booking.status, Booking.Status.CONFIRMED)
        self.assertEqual(
            future_booking.rooms.get().assignments.get().physical_room_id,
            room.id,
        )
        room.refresh_from_db()
        self.assertEqual(room.status, PhysicalRoom.Status.OCCUPIED)

    def test_walk_in_check_in_accepts_identity_number_without_identity_photo(self):
        room = PhysicalRoom.objects.create(hotel=self.hotel, room_type=self.room_type, room_number="306")
        headers = {
            "HTTP_X_BOOKING_ADMIN_KEY": "test-admin-key",
            "HTTP_X_BOOKING_BUSINESS_ID": str(self.hotel.core_business_id),
            "HTTP_X_CORE_USER_ID": "501",
        }
        created = self.client.post(
            "/api/v1/admin/walk-in-booking-v2/",
            {
                "workflow": "direct_check_in",
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
        self.assertFalse(PhysicalRoomActionHistory.objects.filter(
            booking=booking,
            action=PhysicalRoomActionHistory.Action.RESERVED,
        ).exists())

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
        self.assertEqual(
            list(PhysicalRoomActionHistory.objects.filter(booking=booking).values_list("action", flat=True)),
            [PhysicalRoomActionHistory.Action.CHECKED_IN],
        )

    def test_walk_in_v2_defaults_to_reservation_and_records_reserved_action(self):
        room = PhysicalRoom.objects.create(
            hotel=self.hotel, room_type=self.room_type, room_number="306-RSV",
        )
        response = self.client.post(
            "/api/v1/admin/walk-in-booking-v2/",
            {
                "physical_room_id": room.id,
                "rate_plan_id": self.rate_plan.id,
                "check_in": str(self.check_in),
                "check_out": str(self.check_out),
                "contact_name": "Reserved Walk In Guest",
                "contact_phone": "092222223",
                "guest_market": "local",
                "adults": 1,
                "children": 0,
                "guests": [{"name": "Reserved Walk In Guest", "is_primary": True}],
            },
            format="json",
            HTTP_X_BOOKING_ADMIN_KEY="test-admin-key",
            HTTP_X_BOOKING_BUSINESS_ID=str(self.hotel.core_business_id),
            HTTP_X_CORE_USER_ID="501",
        )

        self.assertEqual(response.status_code, 201, response.data)
        booking = Booking.objects.get(id=response.data["data"]["booking"]["id"])
        event = PhysicalRoomActionHistory.objects.get(
            booking=booking,
            action=PhysicalRoomActionHistory.Action.RESERVED,
        )
        self.assertEqual(event.actor_type, PhysicalRoomActionHistory.ActorType.HOTEL_ADMIN)
        self.assertEqual(event.actor_core_user_id, 501)

    def test_walk_in_v2_reserves_occupied_room_after_current_checkout(self):
        room = PhysicalRoom.objects.create(
            hotel=self.hotel,
            room_type=self.room_type,
            room_number="306-F",
        )
        create_walk_in_booking(
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
            },
            core_business_id=self.hotel.core_business_id,
            check_in_immediately=True,
        )

        future_booking = create_walk_in_booking(
            {
                "physical_room_id": room.id,
                "rate_plan_id": self.rate_plan.id,
                "check_in": self.check_out,
                "check_out": self.check_out + timedelta(days=1),
                "contact_name": "Future Guest",
                "contact_phone": "092222222",
                "guest_market": "local",
                "adults": 1,
                "children": 0,
            },
            core_business_id=self.hotel.core_business_id,
            check_in_immediately=False,
        )

        future_booking.refresh_from_db()
        room.refresh_from_db()
        self.assertEqual(future_booking.status, Booking.Status.CONFIRMED)
        self.assertEqual(room.status, PhysicalRoom.Status.OCCUPIED)
        self.assertEqual(
            future_booking.rooms.get().assignments.get().physical_room_id,
            room.id,
        )

    def test_check_in_accepts_guest_without_identity_number_or_photo(self):
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

        self.assertEqual(response.status_code, 200, response.data)
        booking.refresh_from_db()
        room.refresh_from_db()
        self.assertEqual(booking.status, Booking.Status.CHECKED_IN)
        self.assertEqual(room.status, PhysicalRoom.Status.OCCUPIED)

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

    def test_walk_in_v2_prices_extra_beds_add_ons_and_full_payment(self):
        room = PhysicalRoom.objects.create(hotel=self.hotel, room_type=self.room_type, room_number="307-S")
        self.rate_plan.extra_bed_base_price = Decimal("10000")
        self.rate_plan.save(update_fields=["extra_bed_base_price"])
        add_on = AddOn.objects.create(
            hotel=self.hotel,
            code="late-service",
            name="Late Service",
            pricing_unit=AddOn.PricingUnit.PER_NIGHT,
            price=Decimal("5000"),
            currency="MMK",
        )
        response = self.client.post(
            "/api/v1/admin/walk-in-booking-v2/",
            {
                "physical_room_id": room.id,
                "rate_plan_id": self.rate_plan.id,
                "check_in": str(self.check_in),
                "check_out": str(self.check_out),
                "contact_name": "Service Guest",
                "contact_phone": "093333333",
                "guest_market": "local",
                "adults": 1,
                "children": 0,
                "extra_beds": 1,
                "guests": [{
                    "name": "Service Guest", "identity_number": "NRC-S", "is_primary": True,
                }],
                "add_ons": [{"add_on_id": add_on.id, "quantity": 1, "configuration": {}}],
                "payment": {
                    "payment_type": "full_payment", "provider": "cash", "status": "paid",
                },
            },
            format="json",
            HTTP_X_BOOKING_ADMIN_KEY="test-admin-key",
            HTTP_X_BOOKING_BUSINESS_ID=str(self.hotel.core_business_id),
        )

        self.assertEqual(response.status_code, 201, response.data)
        summary = response.data["data"]["payment_summary"]
        self.assertEqual(summary["room_total"], Decimal("180000"))
        self.assertEqual(summary["add_on_total"], Decimal("10000"))
        self.assertEqual(summary["grand_total"], Decimal("190000"))
        self.assertEqual(summary["amount_paid"], Decimal("190000"))
        self.assertEqual(summary["amount_due"], Decimal("0"))
        booking = Booking.objects.get(id=response.data["data"]["booking"]["id"])
        self.assertEqual(booking.payments.get().amount, Decimal("190000"))
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
        self.room_type.core_snapshot = {
            "photos": [
                {"id": 11, "image": "https://media.example/room.jpg", "is_cover": True},
            ],
            "beds": [
                {"bed_type": {"id": 7, "name": "King Bed"}, "quantity": 1},
            ],
            "room_area": 301,
            "room_area_from": 300,
            "room_area_to": 320,
            "area_unit": "sqft",
            "size_sqft": 301,
        }
        self.room_type.save(update_fields=["core_snapshot"])
        room_801 = PhysicalRoom.objects.create(
            hotel=self.hotel,
            room_type=self.room_type,
            core_physical_room_id=801,
            core_building_id=7001,
            core_floor_id=8008,
            room_number="801",
            building="Main Building",
            floor="8",
            core_snapshot={
                "room_type": {
                    "room_standard": {"id": 4, "name": "Deluxe"},
                },
                "room_view": {"id": 3, "name": "City View"},
                "beds": [{"bed_type": {"id": 9, "name": "Physical King Bed"}, "quantity": 1}],
                "room_area": 315,
                "area_unit": "sqft",
            },
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
            note="Air conditioner repair",
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
        ensure_daily_inventory_for_room_type(
            self.room_type, start_date=self.check_in + timedelta(days=3), days=1,
        )
        later_payload = self.payload()
        later_payload["check_in"] = self.check_in + timedelta(days=3)
        later_payload["check_out"] = self.check_in + timedelta(days=4)
        later_payload["rooms"][0]["quantity"] = 1
        later_payload["rooms"][0]["adults"] = 2
        later_booking, _ = create_booking(later_payload)
        record_payment(later_booking, {
            "provider": "cash", "amount": later_booking.grand_total, "status": Payment.Status.PAID,
        })
        RoomAssignment.objects.filter(booking_room__booking=later_booking).delete()
        RoomAssignment.objects.create(booking_room=later_booking.rooms.get(), physical_room=room_803)
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
        self.assertEqual(reserved["room_view"], {"id": 3, "name": "City View"})
        self.assertEqual(reserved["room_standard"], {"id": 4, "name": "Deluxe"})
        self.assertEqual(reserved["room_standard_id"], 4)
        self.assertEqual(reserved["view"], reserved["room_view"])
        self.assertEqual(reserved["bed_type"], {"id": 9, "name": "Physical King Bed"})
        self.assertEqual(reserved["beds"][0]["quantity"], 1)
        self.assertEqual(reserved["room_area"], 315)
        self.assertEqual(reserved["area_unit"], "sqft")
        self.assertEqual(reserved["size_sqft"], 315)
        self.assertEqual(reserved["room_type"]["name"], self.room_type.name)
        self.assertEqual(reserved["room_type"]["room_type_name"], self.room_type.name)
        self.assertEqual(reserved["room_type"]["photos"][0]["id"], 11)
        self.assertEqual(reserved["room_type"]["bed_type"], {"id": 7, "name": "King Bed"})
        self.assertEqual(reserved["room_type"]["bed_types"], [{"id": 7, "name": "King Bed"}])
        self.assertEqual(reserved["room_type"]["room_area"], 301)
        self.assertEqual(reserved["room_type"]["area_unit"], "sqft")
        self.assertEqual(reserved["room_type"]["price"]["base_price"], Decimal("80000"))
        self.assertEqual(reserved["room_type"]["price"]["currency"], "MMK")
        self.assertEqual(reserved["assignment"]["booking_reference"], booking.reference)
        self.assertEqual(reserved["assignment"]["payment_status"], "paid")
        self.assertEqual(reserved["timeline"]["text"], "Reserved: 2 Nights")
        out_of_service = next(
            room for room in board.data["data"]["rooms"] if room["display_status"] == "out_of_service"
        )
        self.assertEqual(out_of_service["status_note"], "Air conditioner repair")
        self.assertEqual(out_of_service["oos_note"], "Air conditioner repair")
        self.assertIsNone(reserved["oos_note"])
        available_with_next = next(room for room in board.data["data"]["rooms"] if room["room_number"] == "803")
        self.assertEqual(available_with_next["display_status"], "available")
        self.assertEqual(available_with_next["timeline"]["text"], "Vacant: 2 Days | Reserved: 1 Night")
        self.assertEqual(available_with_next["timeline"]["next_reserved"]["booking_reference"], future_booking.reference)
        self.assertEqual(
            [item["booking_reference"] for item in available_with_next["next_reservations"]],
            [future_booking.reference, later_booking.reference],
        )
        self.assertEqual(
            available_with_next["timeline"]["next_reservations"],
            available_with_next["next_reservations"],
        )
        next_reservation = available_with_next["next_reservations"][0]
        self.assertEqual(next_reservation["check_in"], future_booking.check_in)
        self.assertEqual(next_reservation["check_out"], future_booking.check_out)
        self.assertEqual(next_reservation["nights"], 1)
        self.assertEqual(next_reservation["adults"], 2)
        self.assertEqual(next_reservation["children"], 0)
        self.assertEqual(next_reservation["guest_name"], future_booking.contact_name)
        self.assertEqual(next_reservation["guest_phone"], future_booking.contact_phone)
        self.assertEqual(next_reservation["currency"], "MMK")
        self.assertEqual(next_reservation["nightly_price"], Decimal("80000"))
        self.assertEqual(next_reservation["grand_total"], Decimal("80000"))
        self.assertEqual(next_reservation["formatted_grand_total"], "MMK 80,000")
        self.assertEqual(next_reservation["payment_status"], "paid")
        self.assertEqual(next_reservation["invoice_count"], 1)
        self.assertEqual(next_reservation["receipt_count"], 1)
        self.assertEqual(next_reservation["total_rooms"], 1)

    def test_physical_room_detail_includes_current_booking(self):
        room = PhysicalRoom.objects.create(
            hotel=self.hotel,
            room_type=self.room_type,
            core_physical_room_id=901,
            room_number="303",
            building="Main Building",
            floor="3",
            core_snapshot={
                "room_type": {
                    "room_standard": {"id": 5, "name": "Superior"},
                },
                "room_view": {"name": "City View"},
                "room_views": [
                    {"id": 1, "name": "City View"},
                    {"id": 2, "name": "Garden View"},
                ],
                "bath_type": {"id": 3, "name": "Shower"},
                "bath_types": [
                    {"id": 3, "name": "Shower"},
                    {"id": 4, "name": "Bathtub"},
                ],
                "beds": [{"bed_type": {"id": 1, "name": "King Bed"}, "quantity": 1}],
                "room_area": 301,
                "area_unit": "sqft",
            },
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
        self.assertEqual(data["room_view"], {"name": "City View"})
        self.assertEqual(len(data["room_views"]), 2)
        self.assertEqual(data["bath_type"], {"id": 3, "name": "Shower"})
        self.assertEqual(len(data["bath_types"]), 2)
        self.assertEqual(data["room_standard"], {"id": 5, "name": "Superior"})
        self.assertEqual(data["room_standard_id"], 5)
        self.assertEqual(data["bed_type"], {"id": 1, "name": "King Bed"})
        self.assertEqual(data["room_area"], 301)
        self.assertEqual(data["size_sqft"], 301)
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
