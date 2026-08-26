from django.core.management.base import BaseCommand, CommandError

from booking.integrations.core import sync_business_from_core
from booking.models import Hotel


class Command(BaseCommand):
    help = (
        "Synchronize Core catalog data for every Hotel already known to the Booking Engine "
        "without changing package, features, or access/subscription snapshot."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--fail-fast",
            action="store_true",
            help="Stop at the first failed business instead of continuing.",
        )

    def handle(self, *args, **options):
        business_ids = list(
            Hotel.objects.order_by("core_business_id").values_list("core_business_id", flat=True)
        )
        if not business_ids:
            self.stdout.write(self.style.WARNING("No Booking Engine hotels found to synchronize."))
            return

        succeeded = 0
        failures = []
        total = len(business_ids)
        for position, business_id in enumerate(business_ids, start=1):
            try:
                result = sync_business_from_core(business_id, preserve_access=True)
            except Exception as exc:
                message = f"[{position}/{total}] Business {business_id}: {exc}"
                failures.append(message)
                self.stderr.write(self.style.ERROR(message))
                if options["fail_fast"]:
                    raise CommandError(message) from exc
                continue

            succeeded += 1
            self.stdout.write(
                self.style.SUCCESS(
                    f"[{position}/{total}] Business {business_id} synchronized "
                    f"(room types: {result['room_types']}, access preserved: yes)."
                )
            )

        summary = f"Core catalog sync complete. Success: {succeeded}, Failed: {len(failures)}, Total: {total}."
        if failures:
            raise CommandError(summary)
        self.stdout.write(self.style.SUCCESS(summary))
