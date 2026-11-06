from datetime import timedelta

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from booking.models import Hotel, OTARemittance, OTARemittanceAuditLog, OTARemittanceItem
from booking.remittance_services import (
    current_week_period, generate_weekly_remittance, hotel_timezone, payout_periods_for_week,
)


class Command(BaseCommand):
    help = "Preview or switch OTA and OTA+PMS hotels to three remittance payouts per week."

    def add_arguments(self, parser):
        parser.add_argument("--apply", action="store_true", help="Write changes to the database.")
        parser.add_argument("--business-id", type=int, help="Limit to one Core business ID.")

    def handle(self, *args, **options):
        hotels = Hotel.objects.filter(package__in=[Hotel.Package.OTA, Hotel.Package.OTA_PMS])
        if options["business_id"] is not None:
            hotels = hotels.filter(core_business_id=options["business_id"])
        counts = {"already set": 0, "next week": 0, "would update": 0, "updated": 0}
        for hotel_id in hotels.order_by("id").values_list("id", flat=True).iterator():
            result = self.update_hotel(hotels, hotel_id, apply=options["apply"])
            counts[result] += 1

        self.stdout.write(", ".join(f"{label}: {count}" for label, count in counts.items()))
        if not options["apply"]:
            self.stdout.write("Preview only. Pass --apply to save the new payout option.")

    @transaction.atomic
    def update_hotel(self, hotels, hotel_id, *, apply):
        hotel = (hotels.select_for_update() if apply else hotels).get(id=hotel_id)
        option = Hotel.RemittancePayoutOption.THREE_TIMES_WEEKLY
        if hotel.remittance_payout_option == option:
            return "already set"

        today = timezone.now().astimezone(hotel_timezone(hotel)).date()
        week_from, week_to = current_week_period(today)
        overlapping = OTARemittance.objects.filter(
            hotel=hotel, period_from__lte=week_to, period_to__gte=week_from,
        )
        rebuildable = Q(
            status__in=[OTARemittance.Status.ACCRUING, OTARemittance.Status.PENDING],
            adjustment_amount=0, adjustment_note="",
            payment_reference="", payment_receipt="",
            period_from__gte=week_from, period_to__lte=week_to,
            checked_at__isnull=True, paid_at__isnull=True, received_at__isnull=True,
            checked_by__isnull=True, paid_by__isnull=True, received_by__isnull=True,
        )
        if apply:
            # Lock existing periods before checking whether they can be rebuilt.
            list(overlapping.select_for_update().values_list("id", flat=True))
        protected = overlapping.exclude(rebuildable).exists() or overlapping.filter(
            audit_logs__action__in=["paid", "received"],
        ).exists()
        missing_items = overlapping.filter(items__isnull=True).exclude(
            gross_booking_amount=0, released_held_funds_amount=0, net_remittance_amount=0,
            tax_amount=0, service_charge_amount=0, refund_amount=0, commission_amount=0,
        ).exists()
        defer = protected or missing_items
        effective_date = week_to + timedelta(days=1) if defer else week_from
        if not apply:
            self.stdout.write(f"WOULD UPDATE {hotel.core_business_id} ({hotel.name}) from {effective_date}")
            return "would update"

        hotel.previous_remittance_payout_option = hotel.remittance_payout_option
        hotel.remittance_payout_option = option
        hotel.remittance_payout_effective_date = effective_date
        hotel.save(update_fields=[
            "previous_remittance_payout_option", "remittance_payout_option", "remittance_payout_effective_date",
        ])
        if defer:
            self.stdout.write(f"UPDATED {hotel.core_business_id} ({hotel.name}) from {effective_date}")
            return "next week"

        previous_ids = list(overlapping.values_list("id", flat=True))
        ledger_ids = set(OTARemittanceItem.objects.filter(
            remittance_id__in=previous_ids,
        ).values_list("ledger_entry_id", flat=True))
        overlapping.filter(rebuildable).delete()
        if hotel.is_active or previous_ids:
            for start, end in payout_periods_for_week(hotel, week_from):
                if start <= today:
                    generate_weekly_remittance(hotel, start, end, accruing=today <= end)
        reassigned = OTARemittanceItem.objects.filter(
            ledger_entry_id__in=ledger_ids, remittance__hotel=hotel,
            remittance__period_from__gte=week_from, remittance__period_to__lte=week_to,
        )
        if reassigned.count() != len(ledger_ids):
            raise CommandError(f"Hotel {hotel.core_business_id}: not all ledger items could be reassigned. Hotel change rolled back.")
        for record in overlapping:
            OTARemittanceAuditLog.objects.create(
                remittance=record, action="payout_schedule_rebuilt", actor_name="System",
                metadata={"previous_remittance_ids": previous_ids},
            )
        self.stdout.write(f"UPDATED {hotel.core_business_id} ({hotel.name})")
        return "updated"
