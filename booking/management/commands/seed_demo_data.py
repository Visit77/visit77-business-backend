from datetime import timedelta
from decimal import Decimal

from django.core.management.base import BaseCommand
from django.db import transaction
from django.utils import timezone

from booking.models import (
    AddOn,
    AddOnTemplate,
    Booking,
    BookingRoom,
    BookingRoomNight,
    DailyInventory,
    DailyRate,
    Guest,
    Hotel,
    MealPlan,
    Payment,
    PhysicalRoom,
    RatePeriod,
    RatePlan,
    RoomTypeMealPlan,
    RoomAssignment,
    RoomType,
)
from booking.services import stay_dates


class Command(BaseCommand):
    help = "Create idempotent Direct Booking demo hotels, rates, inventory, bookings, and room-board states."

    def add_arguments(self, parser):
        parser.add_argument(
            "--business-id",
            type=int,
            default=990001,
            help="Core business id for the primary demo hotel (default: 990001). Use an unused id.",
        )

    @transaction.atomic
    def handle(self, *args, **options):
        primary_business_id = options["business_id"]
        if primary_business_id <= 0:
            self.stderr.write(self.style.ERROR("--business-id must be positive."))
            return

        today = timezone.localdate()
        inventory_start = today - timedelta(days=7)
        inventory_end = today + timedelta(days=120)
        high_season_start = today + timedelta(days=30)
        high_season_end = today + timedelta(days=90)
        festival_day = today + timedelta(days=45)

        primary = self._seed_primary_hotel(
            primary_business_id,
            today,
            inventory_start,
            inventory_end,
            high_season_start,
            high_season_end,
            festival_day,
        )
        self._seed_search_hotel(
            primary_business_id + 1,
            "Bagan River View Hotel",
            "Old Bagan, Mandalay Region",
            "River View Twin",
            (primary_business_id + 1) * 100 + 1,
            Decimal("95000"),
            Decimal("45"),
            inventory_start,
            inventory_end,
        )
        self._seed_search_hotel(
            primary_business_id + 2,
            "Inle Lake Garden Resort",
            "Nyaung Shwe, Shan State",
            "Lake View Villa",
            (primary_business_id + 2) * 100 + 1,
            Decimal("130000"),
            Decimal("65"),
            inventory_start,
            inventory_end,
        )

        self.stdout.write(self.style.SUCCESS("Visit77 Direct Booking demo data is ready."))
        self.stdout.write(f"Primary Core business id: {primary_business_id}")
        self.stdout.write(f"Primary Booking hotel id: {primary['hotel'].id}")
        self.stdout.write(f"Room type id: {primary['room_type'].id}")
        self.stdout.write(f"Board date: {today}")
        self.stdout.write(f"Availability example: {today + timedelta(days=10)} to {today + timedelta(days=13)}")
        self.stdout.write(f"High season: {high_season_start} to {high_season_end}")
        self.stdout.write(f"Festival DailyRate: {festival_day}")
        self.stdout.write("Demo booking references: DEMO-RSV-001, DEMO-OCC-001, DEMO-UNASSIGNED-001, DEMO-HOLD-001")

    def _seed_primary_hotel(
        self,
        core_business_id,
        today,
        inventory_start,
        inventory_end,
        high_season_start,
        high_season_end,
        festival_day,
    ):
        hotel, _ = Hotel.objects.update_or_create(
            core_business_id=core_business_id,
            defaults={
                "name": "Visit77 Demo Grand Hotel",
                "slug": "visit77-demo-grand-hotel",
                "address": "Downtown Yangon, Myanmar",
                "phone": "+95 9 777 000 667",
                "check_in_time": "14:00:00",
                "check_out_time": "12:00:00",
                "is_active": True,
                "core_snapshot": {"demo": True},
                "access_snapshot": {"status": "active", "demo": True},
                "synced_at": timezone.now(),
            },
        )
        core_room_type_id = core_business_id * 100 + 1
        room_type, _ = RoomType.objects.update_or_create(
            hotel=hotel,
            core_room_type_id=core_room_type_id,
            defaults={
                "name": "Deluxe Double Room",
                "description": "Demo king room with city view, private bathroom, Wi-Fi and breakfast options.",
                "max_adults": 2,
                "max_children": 1,
                "max_occupancy": 3,
                "default_inventory": 10,
                "booking_enabled": True,
                "core_active": True,
                "core_snapshot": {
                    "demo": True,
                    "size_sqft": 301,
                    "amenities": ["Wi-Fi", "Air conditioning", "Private bathroom", "TV"],
                },
                "synced_at": timezone.now(),
            },
        )
        local_plan = self._upsert_plan(
            room_type,
            f"room-{core_room_type_id}-local",
            "local-standard",
            "Local Standard - Breakfast & Refundable",
            RatePlan.GuestMarket.LOCAL,
            "MMK",
            Decimal("80000"),
            Decimal("30000"),
            True,
            True,
            {"type": "free_until", "hours_before_check_in": 24},
        )
        self._upsert_plan(
            room_type,
            f"room-{core_room_type_id}-foreign",
            "foreign-standard",
            "Foreign Standard - Breakfast & Refundable",
            RatePlan.GuestMarket.FOREIGN,
            "USD",
            Decimal("40"),
            Decimal("15"),
            True,
            True,
            {"type": "free_until", "hours_before_check_in": 24},
        )
        RatePlan.objects.update_or_create(
            room_type=room_type,
            code="local-saver-no-refund",
            defaults={
                "core_rate_plan_id": "",
                "source": RatePlan.Source.BOOKING,
                "is_default": False,
                "name": "Local Saver - Room Only, No Refund",
                "guest_market": RatePlan.GuestMarket.LOCAL,
                "currency": "MMK",
                "default_price": Decimal("70000"),
                "extra_bed_price": Decimal("25000"),
                "breakfast_included": False,
                "refundable": False,
                "cancellation_policy": {"type": "non_refundable"},
                "is_active": True,
            },
        )
        RatePeriod.objects.update_or_create(
            rate_plan=local_plan,
            name="Demo High Season",
            defaults={
                "start_date": high_season_start,
                "end_date": high_season_end,
                "price": Decimal("120000"),
                "min_stay": 1,
                "is_active": True,
            },
        )
        DailyRate.objects.update_or_create(
            rate_plan=local_plan,
            stay_date=festival_day,
            defaults={"price": Decimal("150000"), "min_stay": 1},
        )
        self._seed_inventory(room_type, inventory_start, inventory_end, 10)

        physical_rooms = {}
        for room_number in range(801, 811):
            status = PhysicalRoom.Status.VACANT
            if room_number == 806:
                status = PhysicalRoom.Status.CLEANING
            elif room_number == 807:
                status = PhysicalRoom.Status.OCCUPIED
            elif room_number == 810:
                status = PhysicalRoom.Status.OUT_OF_SERVICE
            room, _ = PhysicalRoom.objects.update_or_create(
                hotel=hotel,
                room_number=str(room_number),
                defaults={
                    "room_type": room_type,
                    "core_physical_room_id": core_business_id * 1000 + room_number,
                    "floor": "8",
                    "building": "Main Building",
                    "status": status,
                    "note": "Demo room" if room_number != 810 else "Demo maintenance",
                    "is_active": True,
                },
            )
            physical_rooms[room_number] = room

        self._seed_add_ons(hotel)
        meal_plans = self._seed_meal_plans(hotel, room_type)
        self._seed_demo_booking(
            "DEMO-RSV-001",
            hotel,
            room_type,
            local_plan,
            Booking.Status.CONFIRMED,
            today,
            today + timedelta(days=2),
            1,
            "Myo Myo",
            physical_rooms[804],
            paid=True,
            meal_plan_link=meal_plans["Breakfast"],
            special_request="High floor and quiet room, please.",
        )
        self._seed_demo_booking(
            "DEMO-OCC-001",
            hotel,
            room_type,
            local_plan,
            Booking.Status.CHECKED_IN,
            today - timedelta(days=1),
            today + timedelta(days=2),
            1,
            "Aye Aye",
            physical_rooms[807],
            paid=True,
            meal_plan_link=meal_plans["Breakfast"],
            special_request="Late checkout requested.",
        )
        self._seed_demo_booking(
            "DEMO-UNASSIGNED-001",
            hotel,
            room_type,
            local_plan,
            Booking.Status.CONFIRMED,
            today,
            today + timedelta(days=3),
            2,
            "John Demo",
            physical_rooms[805],
            paid=True,
            meal_plan_link=meal_plans["Half Board"],
            special_request="Two nearby rooms.",
        )
        self._seed_demo_booking(
            "DEMO-HOLD-001",
            hotel,
            room_type,
            local_plan,
            Booking.Status.PENDING_PAYMENT,
            today + timedelta(days=10),
            today + timedelta(days=13),
            1,
            "Pending Guest",
            None,
            paid=False,
            meal_plan_link=meal_plans["Breakfast"],
        )
        self._rebuild_demo_inventory(room_type, inventory_start, inventory_end)
        return {"hotel": hotel, "room_type": room_type, "local_plan": local_plan}

    def _seed_search_hotel(
        self,
        core_business_id,
        name,
        address,
        room_name,
        core_room_type_id,
        local_price,
        foreign_price,
        inventory_start,
        inventory_end,
    ):
        hotel, _ = Hotel.objects.update_or_create(
            core_business_id=core_business_id,
            defaults={
                "name": name,
                "slug": name.lower().replace(" ", "-"),
                "address": address,
                "is_active": True,
                "core_snapshot": {"demo": True},
                "access_snapshot": {"status": "active", "demo": True},
                "synced_at": timezone.now(),
            },
        )
        room_type, _ = RoomType.objects.update_or_create(
            hotel=hotel,
            core_room_type_id=core_room_type_id,
            defaults={
                "name": room_name,
                "description": f"Demo room at {name}.",
                "max_adults": 2,
                "max_children": 1,
                "max_occupancy": 3,
                "default_inventory": 5,
                "booking_enabled": True,
                "core_active": True,
                "core_snapshot": {"demo": True},
                "synced_at": timezone.now(),
            },
        )
        self._upsert_plan(
            room_type,
            f"room-{core_room_type_id}-local",
            "local-standard",
            "Local Standard",
            RatePlan.GuestMarket.LOCAL,
            "MMK",
            local_price,
            Decimal("0"),
            True,
            True,
            {},
        )
        self._upsert_plan(
            room_type,
            f"room-{core_room_type_id}-foreign",
            "foreign-standard",
            "Foreign Standard",
            RatePlan.GuestMarket.FOREIGN,
            "USD",
            foreign_price,
            Decimal("0"),
            True,
            True,
            {},
        )
        self._seed_inventory(room_type, inventory_start, inventory_end, 5)
        self._seed_meal_plans(hotel, room_type)
        for index in range(1, 6):
            PhysicalRoom.objects.update_or_create(
                hotel=hotel,
                room_number=f"D{index:02d}",
                defaults={
                    "room_type": room_type,
                    "core_physical_room_id": core_business_id * 100 + index,
                    "floor": "1",
                    "building": "Main Building",
                    "status": PhysicalRoom.Status.VACANT,
                    "is_active": True,
                },
            )

    def _upsert_plan(
        self,
        room_type,
        core_rate_plan_id,
        code,
        name,
        market,
        currency,
        default_price,
        extra_bed_price,
        breakfast,
        refundable,
        policy,
    ):
        plan, _ = RatePlan.objects.update_or_create(
            core_rate_plan_id=core_rate_plan_id,
            defaults={
                "room_type": room_type,
                "source": RatePlan.Source.CORE,
                "is_default": True,
                "code": code,
                "name": name,
                "guest_market": market,
                "currency": currency,
                "default_price": default_price,
                "extra_bed_price": extra_bed_price,
                "breakfast_included": breakfast,
                "refundable": refundable,
                "cancellation_policy": policy,
                "is_active": True,
            },
        )
        return plan

    def _seed_inventory(self, room_type, start_date, end_date, total_rooms):
        dates = []
        current = start_date
        while current <= end_date:
            dates.append(current)
            current += timedelta(days=1)
        existing = {
            row.stay_date: row
            for row in DailyInventory.objects.filter(room_type=room_type, stay_date__in=dates)
        }
        to_create = []
        to_update = []
        for stay_date in dates:
            row = existing.get(stay_date)
            if row is None:
                to_create.append(DailyInventory(
                    room_type=room_type,
                    stay_date=stay_date,
                    total_rooms=total_rooms,
                    stop_sell=False,
                ))
            else:
                row.total_rooms = total_rooms
                row.stop_sell = False
                to_update.append(row)
        if to_create:
            DailyInventory.objects.bulk_create(to_create)
        if to_update:
            DailyInventory.objects.bulk_update(to_update, ["total_rooms", "stop_sell"])

    def _seed_add_ons(self, hotel):
        airport_template = AddOnTemplate.objects.filter(
            code="airport_pickup", status=AddOnTemplate.Status.PUBLISHED
        ).order_by("-version").first()
        early_template = AddOnTemplate.objects.filter(
            code="early_check_in", status=AddOnTemplate.Status.PUBLISHED
        ).order_by("-version").first()
        AddOn.objects.update_or_create(
            hotel=hotel,
            code="airport-pickup",
            defaults={
                "service_type": "airport_pickup",
                "template": airport_template,
                "name": "Airport Pickup",
                "description": "Demo airport pickup service.",
                "pricing_unit": AddOn.PricingUnit.PER_BOOKING,
                "price": Decimal("30000"),
                "currency": "MMK",
                "configuration_schema": airport_template.configuration_schema,
                "is_active": True,
            },
        )
        AddOn.objects.update_or_create(
            hotel=hotel,
            code="early-check-in",
            defaults={
                "service_type": "early_check_in",
                "template": early_template,
                "name": "Early Check-in",
                "pricing_unit": AddOn.PricingUnit.PER_BOOKING,
                "price": Decimal("20000"),
                "currency": "MMK",
                "configuration_schema": early_template.configuration_schema,
                "is_active": True,
            },
        )

    def _seed_meal_plans(self, hotel, room_type):
        specs = [
            {
                "core_meal_plan_id": hotel.core_business_id * 1000 + 1,
                "name": "Room Only",
                "description": "No meals included. Useful for price-sensitive guests.",
                "included_meals": ["no_meal"],
                "meal_windows": {},
                "availability": MealPlan.Availability.PUBLIC,
                "local_base_price": Decimal("0"),
                "local_usd_display_price": Decimal("0"),
                "foreign_base_price": Decimal("0"),
                "foreign_usd_display_price": Decimal("0"),
            },
            {
                "core_meal_plan_id": hotel.core_business_id * 1000 + 2,
                "name": "Breakfast",
                "description": "Breakfast buffet for hotel guests.",
                "included_meals": ["breakfast"],
                "meal_windows": {"breakfast": {"start": "06:30", "end": "10:00"}},
                "availability": MealPlan.Availability.GUEST_ONLY,
                "local_base_price": Decimal("20000"),
                "local_usd_display_price": Decimal("10"),
                "foreign_base_price": Decimal("30000"),
                "foreign_usd_display_price": Decimal("15"),
            },
            {
                "core_meal_plan_id": hotel.core_business_id * 1000 + 3,
                "name": "Half Board",
                "description": "Breakfast and dinner included.",
                "included_meals": ["breakfast", "dinner"],
                "meal_windows": {
                    "breakfast": {"start": "06:30", "end": "10:00"},
                    "dinner": {"start": "18:00", "end": "21:00"},
                },
                "availability": MealPlan.Availability.GUEST_ONLY,
                "local_base_price": Decimal("100000"),
                "local_usd_display_price": Decimal("50"),
                "foreign_base_price": Decimal("120000"),
                "foreign_usd_display_price": Decimal("60"),
            },
            {
                "core_meal_plan_id": hotel.core_business_id * 1000 + 4,
                "name": "Full Board",
                "description": "Breakfast, lunch, and dinner included.",
                "included_meals": ["breakfast", "lunch", "dinner"],
                "meal_windows": {
                    "breakfast": {"start": "06:30", "end": "10:00"},
                    "lunch": {"start": "12:00", "end": "14:00"},
                    "dinner": {"start": "18:00", "end": "21:00"},
                },
                "availability": MealPlan.Availability.GUEST_ONLY,
                "local_base_price": Decimal("140000"),
                "local_usd_display_price": Decimal("70"),
                "foreign_base_price": Decimal("170000"),
                "foreign_usd_display_price": Decimal("85"),
            },
            {
                "core_meal_plan_id": hotel.core_business_id * 1000 + 5,
                "name": "All Inclusive",
                "description": "All meals and selected drinks. Public restaurant package demo.",
                "included_meals": ["breakfast", "lunch", "dinner", "drinks"],
                "meal_windows": {
                    "breakfast": {"start": "06:30", "end": "10:00"},
                    "lunch": {"start": "12:00", "end": "14:00"},
                    "dinner": {"start": "18:00", "end": "21:00"},
                    "drinks": {"start": "10:00", "end": "22:00"},
                },
                "availability": MealPlan.Availability.PUBLIC,
                "local_base_price": Decimal("200000"),
                "local_usd_display_price": Decimal("100"),
                "foreign_base_price": Decimal("240000"),
                "foreign_usd_display_price": Decimal("120"),
            },
        ]
        meal_plans = {}
        for spec in specs:
            meal_plan, _ = MealPlan.objects.update_or_create(
                hotel=hotel,
                core_meal_plan_id=spec["core_meal_plan_id"],
                defaults={
                    "name": spec["name"],
                    "description": spec["description"],
                    "included_meals": spec["included_meals"],
                    "meal_windows": spec["meal_windows"],
                    "availability": spec["availability"],
                    "local_base_price": spec["local_base_price"],
                    "local_usd_display_price": spec["local_usd_display_price"],
                    "foreign_base_price": spec["foreign_base_price"],
                    "foreign_usd_display_price": spec["foreign_usd_display_price"],
                    "core_active": True,
                    "core_snapshot": {"demo": True},
                    "synced_at": timezone.now(),
                },
            )
            meal_plans[meal_plan.name] = meal_plan

        link_specs = [
            ("Breakfast", True, True, True, True, Decimal("0"), Decimal("0"), Decimal("0"), Decimal("0")),
            ("Room Only", False, False, True, True, Decimal("0"), Decimal("0"), Decimal("0"), Decimal("0")),
            ("Half Board", False, False, True, True, Decimal("0"), Decimal("0"), Decimal("0"), Decimal("0")),
            ("Full Board", False, False, True, True, Decimal("0"), Decimal("0"), Decimal("0"), Decimal("0")),
            ("All Inclusive", False, False, True, False, Decimal("180000"), Decimal("90"), Decimal("220000"), Decimal("110")),
        ]
        links = {}
        for rank, (name, is_included, is_default, is_guest_selectable, use_default_price, local_price, local_usd, foreign_price, foreign_usd) in enumerate(link_specs, start=1):
            link, _ = RoomTypeMealPlan.objects.update_or_create(
                room_type=room_type,
                meal_plan=meal_plans[name],
                defaults={
                    "is_included": is_included,
                    "is_default": is_default,
                    "is_guest_selectable": is_guest_selectable,
                    "use_hotel_default_price": use_default_price,
                    "local_base_price": local_price,
                    "local_usd_display_price": local_usd,
                    "foreign_base_price": foreign_price,
                    "foreign_usd_display_price": foreign_usd,
                    "core_snapshot": {"demo": True, "rank": rank},
                    "synced_at": timezone.now(),
                },
            )
            links[name] = link
        return links

    def _seed_demo_booking(
        self,
        reference,
        hotel,
        room_type,
        rate_plan,
        status,
        check_in,
        check_out,
        quantity,
        guest_name,
        physical_room,
        paid,
        meal_plan_link=None,
        special_request="",
    ):
        nightly_prices = []
        for day in stay_dates(check_in, check_out):
            daily = DailyRate.objects.filter(rate_plan=rate_plan, stay_date=day).first()
            period = RatePeriod.objects.filter(
                rate_plan=rate_plan,
                is_active=True,
                start_date__lte=day,
            ).filter(end_date__isnull=True).first() or RatePeriod.objects.filter(
                rate_plan=rate_plan,
                is_active=True,
                start_date__lte=day,
                end_date__gte=day,
            ).first()
            nightly_prices.append((day, daily.price if daily else period.price if period else rate_plan.default_price))
        meal_plan_nightly_total = Decimal("0")
        meal_plan_snapshot = {}
        if meal_plan_link:
            if not meal_plan_link.is_included:
                meal_plan_price = (
                    meal_plan_link.effective_foreign_base_price
                    if rate_plan.guest_market == RatePlan.GuestMarket.FOREIGN
                    else meal_plan_link.effective_local_base_price
                )
                meal_plan_nightly_total = meal_plan_price * quantity
            meal_plan_snapshot = {
                "id": meal_plan_link.id,
                "meal_plan_id": meal_plan_link.meal_plan_id,
                "core_meal_plan_id": meal_plan_link.meal_plan.core_meal_plan_id,
                "name": meal_plan_link.meal_plan.name,
                "description": meal_plan_link.meal_plan.description,
                "included_meals": meal_plan_link.meal_plan.included_meals,
                "meal_windows": meal_plan_link.meal_plan.meal_windows,
                "availability": meal_plan_link.meal_plan.availability,
                "is_included": meal_plan_link.is_included,
                "is_default": meal_plan_link.is_default,
                "is_guest_selectable": meal_plan_link.is_guest_selectable,
                "base_currency": rate_plan.room_type.hotel.base_currency,
            }
        room_total = sum((price * quantity + meal_plan_nightly_total for _, price in nightly_prices), Decimal("0"))
        meal_plan_total = meal_plan_nightly_total * len(nightly_prices)
        booking, _ = Booking.objects.update_or_create(
            reference=reference,
            defaults={
                "hotel": hotel,
                "status": status,
                "check_in": check_in,
                "check_out": check_out,
                "contact_name": guest_name,
                "contact_phone": "09110000000",
                "contact_email": "demo@example.com",
                "guest_market": RatePlan.GuestMarket.LOCAL,
                "currency": rate_plan.currency,
                "room_total": room_total,
                "grand_total": room_total,
                "amount_paid": room_total if paid else Decimal("0"),
                "special_request": special_request,
                "hold_expires_at": timezone.now() + timedelta(minutes=30) if status == Booking.Status.PENDING_PAYMENT else None,
                "cancellation_policy_snapshot": {str(rate_plan.id): rate_plan.cancellation_policy},
            },
        )
        booking_room, _ = BookingRoom.objects.update_or_create(
            booking=booking,
            defaults={
                "room_type": room_type,
                "rate_plan": rate_plan,
                "meal_plan_link": meal_plan_link,
                "quantity": quantity,
                "adults": quantity * 2,
                "children": 0,
                "extra_beds": 0,
                "room_type_snapshot": {"core_room_type_id": room_type.core_room_type_id, "name": room_type.name},
                "rate_plan_snapshot": {"code": rate_plan.code, "name": rate_plan.name, "currency": rate_plan.currency},
                "meal_plan_snapshot": meal_plan_snapshot,
                "meal_plan_total": meal_plan_total,
                "total": room_total,
            },
        )
        booking_room.nights.all().delete()
        BookingRoomNight.objects.bulk_create([
            BookingRoomNight(
                booking_room=booking_room,
                stay_date=day,
                unit_price=price,
                quantity=quantity,
                meal_plan_total=meal_plan_nightly_total,
                total=price * quantity + meal_plan_nightly_total,
            )
            for day, price in nightly_prices
        ])
        Guest.objects.update_or_create(
            booking=booking,
            is_primary=True,
            defaults={"name": guest_name, "phone": booking.contact_phone, "email": booking.contact_email},
        )
        RoomAssignment.objects.filter(booking_room=booking_room).delete()
        if physical_room:
            RoomAssignment.objects.create(booking_room=booking_room, physical_room=physical_room)
        if paid:
            Payment.objects.update_or_create(
                invoice_number=f"IV-{reference}",
                defaults={
                    "booking": booking,
                    "provider": Payment.Provider.DEMO,
                    "provider_reference": f"PAY-{reference}",
                    "status": Payment.Status.PAID,
                    "amount": room_total,
                    "refunded_amount": Decimal("0"),
                    "currency": rate_plan.currency,
                    "receipt_number": f"RC-{reference}",
                    "metadata": {"demo": True},
                    "paid_at": timezone.now(),
                },
            )
        else:
            Payment.objects.filter(booking=booking).delete()
        return booking

    def _rebuild_demo_inventory(self, room_type, start_date, end_date):
        DailyInventory.objects.filter(
            room_type=room_type,
            stay_date__range=(start_date, end_date),
        ).update(held_rooms=0, reserved_rooms=0)
        bookings = Booking.objects.filter(
            hotel=room_type.hotel,
            reference__startswith="DEMO-",
            status__in=[Booking.Status.PENDING_PAYMENT, Booking.Status.CONFIRMED, Booking.Status.CHECKED_IN],
        ).prefetch_related("rooms")
        for booking in bookings:
            for booking_room in booking.rooms.filter(room_type=room_type):
                field = "held_rooms" if booking.status == Booking.Status.PENDING_PAYMENT else "reserved_rooms"
                for day in stay_dates(booking.check_in, booking.check_out):
                    row = DailyInventory.objects.filter(room_type=room_type, stay_date=day).first()
                    if row:
                        setattr(row, field, getattr(row, field) + booking_room.quantity)
                        row.save(update_fields=[field])
