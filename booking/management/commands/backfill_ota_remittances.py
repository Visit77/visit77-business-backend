from datetime import timedelta

from django.core.management.base import BaseCommand
from django.db import transaction
from django.utils import timezone

from booking.models import Booking, Hotel, OTARemittance, OTARemittanceAuditLog, Payment
from booking.remittance_services import (
    generate_weekly_remittance,
    hotel_timezone,
    refresh_current_week_remittance,
    sync_all_payment_ledgers,
)


class Command(BaseCommand):
    help = (
        "Rebuild scheduled OTA remittances. Older historical periods are received, "
        "the latest completed week is pending, and the current partial week is rebuilt as accruing."
    )

    def handle(self, *args, **options):
        sync_all_payment_ledgers()
        created = 0
        for hotel in Hotel.objects.all():
            hotel_today = timezone.now().astimezone(hotel_timezone(hotel)).date()
            current_monday = hotel_today - timedelta(days=hotel_today.weekday())
            first_paid = Payment.objects.filter(
                booking__hotel=hotel,
                booking__source="ota",
                paid_at__isnull=False,
            ).order_by("paid_at").values_list("paid_at", flat=True).first()
            if not first_paid:
                continue
            # An earlier version incorrectly created a statement for the current
            # partial Monday-Sunday period. Remove only those system backfill
            # statements; their ledger entries become eligible next Monday.
            with transaction.atomic():
                incorrect = OTARemittance.objects.filter(
                    hotel=hotel,
                    historical_backfill=True,
                    system_generated=True,
                    period_from__gte=current_monday,
                )
                affected_booking_ids = list(
                    Booking.objects.filter(remittance_items__remittance__in=incorrect)
                    .values_list("id", flat=True)
                    .distinct()
                )
                incorrect.delete()
                if affected_booking_ids:
                    Booking.objects.filter(id__in=affected_booking_ids).update(
                        settlement_status=Booking.SettlementStatus.AVAILABLE,
                        settled_at=None,
                    )

            first_local_date = first_paid.astimezone(hotel_timezone(hotel)).date()
            cursor = first_local_date - timedelta(days=first_local_date.weekday())
            while cursor < current_monday:
                periods = (
                    [
                        (cursor, cursor + timedelta(days=2)),
                        (cursor + timedelta(days=3), cursor + timedelta(days=6)),
                    ]
                    if hotel.remittance_payout_option == Hotel.RemittancePayoutOption.TWICE_WEEKLY
                    else [(cursor, cursor + timedelta(days=6))]
                )
                for period_from, period_to in periods:
                    is_latest_completed_period = period_to == current_monday - timedelta(days=1)
                    result = generate_weekly_remittance(
                        hotel,
                        period_from,
                        period_to,
                        historical=True,
                        completed=not is_latest_completed_period,
                    )
                    if result and is_latest_completed_period:
                        result.status = OTARemittance.Status.PENDING
                        result.checked_at = None
                        result.checked_by = None
                        result.paid_at = None
                        result.paid_by = None
                        result.received_at = None
                        result.received_by = None
                        result.save(update_fields=[
                            "status", "checked_at", "checked_by", "paid_at", "paid_by",
                            "received_at", "received_by", "updated_at",
                        ])
                        Booking.objects.filter(remittance_items__remittance=result).update(
                            settlement_status=Booking.SettlementStatus.PENDING_REMITTANCE,
                            settled_at=None,
                        )
                        OTARemittanceAuditLog.objects.get_or_create(
                            remittance=result,
                            action="schedule_corrected",
                            actor_name="System",
                        )
                    created += int(result is not None)
                cursor += timedelta(days=7)
            created += int(
                refresh_current_week_remittance(
                    hotel, today=hotel_today, sync_ledger=False,
                ) is not None
            )
        self.stdout.write(self.style.SUCCESS(f"Backfilled {created} remittance period(s)."))
