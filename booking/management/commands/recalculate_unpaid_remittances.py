from django.core.management.base import BaseCommand, CommandError

from booking.models import OTARemittance
from booking.remittance_services import recalculate_unpaid_remittance, unpaid_remittance_filter


class Command(BaseCommand):
    help = (
        "Recalculate unpaid OTA remittances using tax-only commission exclusion. "
        "Preserves stored rate, refunds, adjustments and ledger items. "
        "Changed checked statements return to pending for recheck."
    )
    requires_migrations_checks = True

    def add_arguments(self, parser):
        parser.add_argument("--dry-run", action="store_true", help="Preview changes without writing amounts, status or audit logs.")
        parser.add_argument("--business-id", type=int, help="Limit to a Core business ID.")
        parser.add_argument("--remittance-id", type=int, action="append", help="Limit to a remittance ID; repeat to select multiple IDs.")

    def handle(self, *args, **options):
        if options["business_id"] is not None and options["business_id"] <= 0:
            raise CommandError("--business-id must be positive.")
        if any(value <= 0 for value in options["remittance_id"] or []):
            raise CommandError("--remittance-id must be positive.")
        scoped = OTARemittance.objects.all()
        if options["business_id"] is not None:
            scoped = scoped.filter(hotel__core_business_id=options["business_id"])
        if options["remittance_id"]:
            scoped = scoped.filter(pk__in=options["remittance_id"])
        eligible = scoped.filter(unpaid_remittance_filter())
        skipped = scoped.exclude(unpaid_remittance_filter()).count()
        # Lock and recheck each row inside the service so a concurrent payment is protected.
        ids = list(eligible.order_by("pk").values_list("pk", flat=True))
        changed = unchanged = 0
        preview = options["dry_run"]
        for record_id in ids:
            result = recalculate_unpaid_remittance(record_id, dry_run=preview)
            if result["outcome"] == "skipped_paid":
                skipped += 1
                continue
            if result["outcome"] == "unchanged":
                unchanged += 1
                continue
            changed += 1
            prefix = "WOULD UPDATE" if preview else "UPDATED"
            self.stdout.write(
                f"{prefix} {result['reference']} (id={record_id}, {result['currency']}): "
                f"commission {result['previous_commission']:.2f} -> {result['commission']:.2f}; "
                f"net payout {result['previous_net_payout']:.2f} -> {result['net_payout']:.2f}; "
                f"status {result['previous_status']} -> {result['status']}"
            )
        self.stdout.write(self.style.SUCCESS(
            f"{'Dry run' if preview else 'Complete'}: {'would update' if preview else 'updated'}={changed}, "
            f"unchanged={unchanged}, skipped paid/received={skipped}."
        ))
