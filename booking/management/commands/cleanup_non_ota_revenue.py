from django.core.management.base import BaseCommand

from booking.remittance_services import cleanup_non_ota_revenue_ledgers


class Command(BaseCommand):
    help = (
        "Remove PMS-scoped/additional payments from OTA revenue ledgers and "
        "recalculate affected remittances."
    )

    def handle(self, *args, **options):
        result = cleanup_non_ota_revenue_ledgers()
        self.stdout.write(self.style.SUCCESS(
            "Removed {removed_ledgers} invalid ledger row(s) and "
            "{removed_items} remittance item(s); updated "
            "{updated_remittances} remittance(s), deleted "
            "{deleted_remittances} empty remittance(s).".format(**result)
        ))
