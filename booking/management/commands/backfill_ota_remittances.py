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
        "Rebuild weekly OTA remittances. Older historical weeks are received, "
        "the latest completed week is pending, and the current partial week is rebuilt as accruing."
    )

    def handle(self, *args, **options):
        sync_all_payment_ledgers()
        today = timezone.localdate()
        current_monday = today - timedelta(days=today.weekday())
        latest_completed_monday = current_monday - timedelta(days=7)
        created = 0
        for hotel in Hotel.objects.all():
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
                period_to = cursor + timedelta(days=6)
                is_latest_completed_week = cursor == latest_completed_monday
                result = generate_weekly_remittance(
                    hotel,
                    cursor,
                    period_to,
                    historical=True,
                    completed=not is_latest_completed_week,
                )
                if result and is_latest_completed_week:
                    # The immediately previous seven-day period is payable this
                    # week, not already paid/received by the historical import.
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
                refresh_current_week_remittance(hotel, today=today, sync_ledger=False) is not None
            )
        self.stdout.write(self.style.SUCCESS(f"Backfilled {created} weekly remittance period(s)."))
