from uuid import UUID

from django.core.management.base import BaseCommand
from django.db.models import Q
from django.utils import timezone
from django.utils.dateparse import parse_date

from booking.services import (
    paid_room_type_count_no_show_candidates,
    restore_paid_room_type_count_no_show_booking,
)


class Command(BaseCommand):
    help = (
        "Restore paid, canceled, past OTA room-type-count no-show bookings "
        "to confirmed. Runs as a dry-run unless --apply is supplied."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--apply",
            action="store_true",
            help="Persist the restore. Without this flag, only candidates are listed.",
        )
        parser.add_argument(
            "--booking",
            action="append",
            default=[],
            help=(
                "Optional booking UUID, reference, or booking code. Repeat the "
                "option to select multiple bookings."
            ),
        )
        parser.add_argument(
            "--as-of",
            help="Candidate cutoff date in YYYY-MM-DD format (default: today).",
        )

    def handle(self, *args, **options):
        as_of = timezone.localdate()
        if options["as_of"]:
            as_of = parse_date(options["as_of"])
            if not as_of:
                self.stderr.write(self.style.ERROR("--as-of must use YYYY-MM-DD."))
                return

        queryset = paid_room_type_count_no_show_candidates(as_of=as_of).order_by(
            "check_in", "id"
        )
        identifiers = [value.strip() for value in options["booking"] if value.strip()]
        if identifiers:
            selector = Q()
            for value in identifiers:
                selector |= Q(reference=value) | Q(booking_code=value)
                try:
                    selector |= Q(pk=UUID(value))
                except ValueError:
                    pass
            queryset = queryset.filter(selector)

        candidates = list(queryset)
        for booking in candidates:
            self.stdout.write(
                f"{booking.id} | {booking.booking_code} | {booking.reference} | "
                f"check-in={booking.check_in} | status={booking.status}"
            )

        if not options["apply"]:
            self.stdout.write(
                self.style.WARNING(
                    f"Dry-run: {len(candidates)} candidate(s). Re-run with --apply to restore."
                )
            )
            return

        restored = 0
        for booking in candidates:
            restore_paid_room_type_count_no_show_booking(booking, as_of=as_of)
            restored += 1
        self.stdout.write(self.style.SUCCESS(f"Restored {restored} booking(s) to confirmed."))
