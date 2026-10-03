from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.db.models import Exists, OuterRef
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from booking.models import Booking, Hotel, RoomAssignment


class Command(BaseCommand):
    help = (
        "Backfill legacy booking inventory_mode values: bookings with any "
        "physical-room assignment become physical_room; bookings without an "
        "assignment become room_type_count. Dry-run unless --apply is supplied."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--business-id",
            type=int,
            help="Limit the backfill to one Hotel.core_business_id.",
        )
        parser.add_argument(
            "--source",
            choices=[Booking.Source.OTA, Booking.Source.PMS, "all"],
            default=Booking.Source.OTA,
            help="Booking source to process (default: ota).",
        )
        parser.add_argument(
            "--created-before",
            help=(
                "Only process bookings created before this ISO-8601 datetime. "
                "Useful for limiting the command to legacy records."
            ),
        )
        parser.add_argument(
            "--apply",
            action="store_true",
            help="Write changes. Without this flag the command is a dry-run.",
        )

    def handle(self, *args, **options):
        queryset = Booking.objects.all()
        business_id = options.get("business_id")
        if business_id is not None:
            if not Hotel.objects.filter(core_business_id=business_id).exists():
                raise CommandError(f"Hotel business {business_id} was not found.")
            queryset = queryset.filter(hotel__core_business_id=business_id)

        source = options["source"]
        if source != "all":
            queryset = queryset.filter(source=source)

        created_before = options.get("created_before")
        if created_before:
            cutoff = parse_datetime(created_before)
            if cutoff is None:
                raise CommandError(
                    "--created-before must be a valid ISO-8601 datetime."
                )
            if timezone.is_naive(cutoff):
                cutoff = timezone.make_aware(
                    cutoff,
                    timezone.get_current_timezone(),
                )
            queryset = queryset.filter(created_at__lt=cutoff)

        assignment_exists = RoomAssignment.objects.filter(
            booking_room__booking_id=OuterRef("pk"),
        )
        classified = queryset.annotate(
            has_physical_assignment=Exists(assignment_exists),
        )
        physical_ids = classified.filter(
            has_physical_assignment=True,
        ).values("pk")
        count_ids = classified.filter(
            has_physical_assignment=False,
        ).values("pk")

        physical_count = physical_ids.count()
        room_type_count = count_ids.count()
        total = physical_count + room_type_count
        mode = "APPLY" if options["apply"] else "DRY-RUN"
        self.stdout.write(f"Mode: {mode}")
        self.stdout.write(f"Source: {source}")
        if business_id is not None:
            self.stdout.write(f"Business ID: {business_id}")
        if created_before:
            self.stdout.write(f"Created before: {created_before}")
        self.stdout.write(f"Bookings found: {total}")
        self.stdout.write(f"  physical_room (has assignment): {physical_count}")
        self.stdout.write(f"  room_type_count (no assignment): {room_type_count}")

        if not options["apply"]:
            self.stdout.write(self.style.WARNING(
                "No records changed. Re-run with --apply after reviewing counts."
            ))
            return

        with transaction.atomic():
            physical_updated = Booking.objects.filter(
                pk__in=physical_ids,
            ).exclude(
                inventory_mode=Hotel.InventoryMode.PHYSICAL_ROOM,
            ).update(inventory_mode=Hotel.InventoryMode.PHYSICAL_ROOM)
            count_updated = Booking.objects.filter(
                pk__in=count_ids,
            ).exclude(
                inventory_mode=Hotel.InventoryMode.ROOM_TYPE_COUNT,
            ).update(inventory_mode=Hotel.InventoryMode.ROOM_TYPE_COUNT)

        self.stdout.write(self.style.SUCCESS(
            f"Updated {physical_updated + count_updated} booking(s): "
            f"{physical_updated} physical_room, "
            f"{count_updated} room_type_count."
        ))
