from datetime import timedelta

from django.core.management.base import BaseCommand
from django.utils import timezone

from booking.models import Hotel, Payment
from booking.remittance_services import generate_weekly_remittance, hotel_timezone, sync_all_payment_ledgers


class Command(BaseCommand):
    help = "Rebuild weekly OTA revenue/remittance history and mark completed weeks paid/received."

    def handle(self, *args, **options):
        sync_all_payment_ledgers()
        today = timezone.localdate()
        current_monday = today - timedelta(days=today.weekday())
        created = 0
        for hotel in Hotel.objects.all():
            first_paid = Payment.objects.filter(
                booking__hotel=hotel,
                booking__source="ota",
                paid_at__isnull=False,
            ).order_by("paid_at").values_list("paid_at", flat=True).first()
            if not first_paid:
                continue
            first_local_date = first_paid.astimezone(hotel_timezone(hotel)).date()
            cursor = first_local_date - timedelta(days=first_local_date.weekday())
            while cursor <= current_monday:
                period_to = cursor + timedelta(days=6)
                result = generate_weekly_remittance(
                    hotel,
                    cursor,
                    period_to,
                    historical=True,
                    completed=period_to < current_monday,
                )
                created += int(result is not None)
                cursor += timedelta(days=7)
        self.stdout.write(self.style.SUCCESS(f"Backfilled {created} weekly remittance period(s)."))
