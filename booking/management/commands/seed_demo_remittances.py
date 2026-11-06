from datetime import date, datetime, time, timedelta
from uuid import uuid4

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from booking.booking_services.receipt import finalize_receipt_snapshot
from booking.models import (
    Booking, BookingRoom, BookingRoomNight, Guest, Hotel, Invoice,
    OTARemittance, OTARemittanceAuditLog, OTARevenueLedger, Payment, RatePlan,
)
from booking.remittance_services import (
    active_payout_period, generate_weekly_remittance, hotel_timezone, sync_payment_ledger,
)
from booking.services import _sync_invoice_status, ensure_initial_invoice


class Command(BaseCommand):
    help = "Preview or seed paid demo OTA bookings and pending remittances for a test hotel."

    def add_arguments(self, parser):
        parser.add_argument("--business-id", type=int, required=True, help="Core business ID")
        parser.add_argument("--remittances", type=int, default=5)
        parser.add_argument("--bookings", type=int, default=10)
        parser.add_argument("--as-of", type=date.fromisoformat, help="Use completed periods before this hotel-local date.")
        parser.add_argument("--apply", action="store_true", help="Write demo data to the current database.")

    @transaction.atomic
    def handle(self, *args, **options):
        count, booking_count = options["remittances"], options["bookings"]
        if not 1 <= count <= booking_count <= 100:
            raise CommandError("Require 1 <= remittances <= bookings <= 100.")
        hotels = Hotel.objects.filter(core_business_id=options["business_id"], is_active=True,
                                      package__in=[Hotel.Package.OTA, Hotel.Package.OTA_PMS])
        if options["apply"]:
            hotels = hotels.select_for_update()
        hotel = hotels.first()
        if not hotel:
            raise CommandError("No active OTA or OTA+PMS hotel found for this Core business ID.")

        zone = hotel_timezone(hotel)
        today = timezone.now().astimezone(zone).date()
        as_of = options["as_of"] or today
        if as_of > today or as_of < date(2000, 1, 1):
            raise CommandError("--as-of must be between 2000-01-01 and today.")
        batch = f"demo-remittance:{as_of}:"
        if hotel.bookings.filter(idempotency_key__startswith=batch).exists():
            self.stdout.write(f"Demo batch {as_of} already exists for {hotel.name}.")
            return

        plans_by_room = {}
        for plan in RatePlan.objects.filter(
            room_type__hotel=hotel, room_type__core_active=True,
            room_type__booking_enabled=True, room_type__room_price_setup=True,
            is_active=True, currency=hotel.base_currency, base_price__gt=0,
        ).select_related("room_type").order_by("room_type_id", "-is_default", "id"):
            plans_by_room.setdefault(plan.room_type_id, plan)
        plans = list(plans_by_room.values())
        if not plans:
            raise CommandError("Set up an active room type with a positive base rate first.")

        periods = self.available_periods(hotel, as_of, count, zone)
        base_count, remainder = divmod(booking_count, count)
        self.stdout.write(f"{hotel.name} (Core business {hotel.core_business_id}): "
                          f"{booking_count} demo bookings in {count} completed payout periods.")
        for index, (start, end) in enumerate(periods):
            quantity = base_count + (index < remainder)
            self.stdout.write(f"  {start} to {end} | payout {end + timedelta(days=1)} | {quantity} bookings")
        if not options["apply"]:
            self.stdout.write("Preview only. Pass --apply to create this batch.")
            return

        number = 0
        for index, (start, end) in enumerate(periods):
            for _ in range(base_count + (index < remainder)):
                plan = plans[number % len(plans)]
                self.create_demo_booking(hotel, plan, start, zone, batch, number)
                number += 1
            record = generate_weekly_remittance(hotel, start, end)
            if (
                not record or record.net_remittance_amount <= 0
                or record.items.count() != base_count + (index < remainder)
                or record.items.exclude(booking__idempotency_key__startswith=batch).exists()
            ):
                raise CommandError(f"Could not create a positive, demo-only remittance for {start} to {end}. Batch rolled back.")
            OTARemittanceAuditLog.objects.create(
                remittance=record, action="demo_seeded", actor_name="System", metadata={"batch": batch},
            )
            self.stdout.write(f"  Created {record.reference} | {record.status} | {record.currency} {record.net_remittance_amount}")

    def available_periods(self, hotel, as_of, count, zone):
        periods = []
        current_start, _ = active_payout_period(hotel, as_of)
        cursor = current_start - timedelta(days=1)
        while len(periods) < count and cursor >= as_of - timedelta(weeks=104):
            start, end = active_payout_period(hotel, cursor)
            cursor = start - timedelta(days=1)
            lower = datetime.combine(start, time.min, tzinfo=zone)
            upper = datetime.combine(end + timedelta(days=1), time.min, tzinfo=zone)
            if hotel.ota_remittances.filter(period_from__lte=end, period_to__gte=start).exists():
                continue
            if OTARevenueLedger.objects.filter(hotel=hotel, effective_at__gte=lower, effective_at__lt=upper).exists():
                continue
            if Payment.objects.filter(booking__hotel=hotel, paid_at__isnull=False).filter(
                Q(paid_at__gte=lower, paid_at__lt=upper) | Q(booking__check_in__range=(start, end))
            ).exists():
                continue
            periods.append((start, end))
        if len(periods) != count:
            raise CommandError("Not enough empty completed periods in the past two years.")
        return periods

    def create_demo_booking(self, hotel, plan, check_in, zone, batch, number):
        paid_at = datetime.combine(check_in, time(12), tzinfo=zone)
        room_type = plan.room_type
        amount = plan.base_price
        market = RatePlan.GuestMarket.LOCAL if plan.guest_market == RatePlan.GuestMarket.ALL else plan.guest_market
        booking = Booking.objects.create(
            hotel=hotel, reference=f"DEMO-{uuid4().hex[:19]}", idempotency_key=f"{batch}{number}",
            source=Booking.Source.OTA, source_name="Remittance demo",
            status=Booking.Status.CHECKED_OUT, inventory_mode=hotel.inventory_mode,
            check_in=check_in, check_out=check_in + timedelta(days=1), checked_in_at=paid_at,
            contact_name=f"Demo Guest {number + 1}", contact_phone="0900000000",
            guest_market=market, currency=hotel.base_currency,
            room_total=amount, grand_total=amount, amount_paid=amount,
        )
        Booking.objects.filter(pk=booking.pk).update(created_at=paid_at)
        room = BookingRoom.objects.create(
            booking=booking, room_type=room_type, rate_plan=plan, quantity=1, adults=1,
            room_type_snapshot={"core_room_type_id": room_type.core_room_type_id, "name": room_type.name},
            rate_plan_snapshot={"code": plan.code, "name": plan.name, "base_price": str(amount),
                                "base_currency": hotel.base_currency, "currency": hotel.base_currency},
            total=amount,
        )
        BookingRoomNight.objects.create(
            booking_room=room, stay_date=check_in, unit_price=amount, quantity=1, total=amount,
        )
        Guest.objects.create(booking=booking, name=booking.contact_name, phone=booking.contact_phone, is_primary=True)
        invoice = ensure_initial_invoice(booking)
        Invoice.objects.filter(pk=invoice.pk).update(issued_at=paid_at)
        payment = Payment.objects.create(
            booking=booking, invoice=invoice, provider=Payment.Provider.DEMO, status=Payment.Status.PAID,
            amount=invoice.total, currency=hotel.base_currency, invoice_number=invoice.invoice_number,
            provider_reference=booking.reference, metadata={"demo": True, "batch": batch}, paid_at=paid_at,
        )
        Payment.objects.filter(pk=payment.pk).update(created_at=paid_at)
        invoice.issued_at = paid_at
        booking.created_at = paid_at
        _sync_invoice_status(invoice)
        finalize_receipt_snapshot(payment)
        sync_payment_ledger(payment)
