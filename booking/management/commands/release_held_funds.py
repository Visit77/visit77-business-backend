from django.core.management.base import BaseCommand
from django.db import transaction

from booking.models import Booking, Invoice, OTARevenueLedger, Payment
from booking.remittance_services import sync_payment_ledger, unreleased_held_entries


class Command(BaseCommand):
    help = "Release existing OTA held funds into Available using today's payout period."

    def add_arguments(self, parser):
        parser.add_argument("--apply", action="store_true", help="Release funds. Default is preview.")
        parser.add_argument("--business-id", type=int, help="Limit to a Core business ID.")

    def handle(self, *args, **options):
        eligible = unreleased_held_entries().filter(
            booking__source=Booking.Source.OTA,
            payment__paid_at__isnull=False,
            payment__status__in=[Payment.Status.PAID, Payment.Status.PARTIALLY_REFUNDED, Payment.Status.REFUNDED],
            payment__invoice__charge_scope=Invoice.ChargeScope.OTA,
            payment__invoice__invoice_type=Invoice.Type.ROOM_BOOKING,
        )
        if options["business_id"] is not None:
            eligible = eligible.filter(hotel__core_business_id=options["business_id"])

        apply = options["apply"]
        count = 0
        for entry_id, booking_id in list(eligible.values_list("pk", "booking_id")):
            with transaction.atomic():
                Booking.objects.select_for_update().get(pk=booking_id)
                entry = eligible.select_related("payment", "booking").filter(pk=entry_id).first()
                if not entry:
                    continue
                if apply:
                    rows = sync_payment_ledger(entry.payment, release_held=True)
                    if not any(row.event_type == OTARevenueLedger.EventType.HELD_RELEASE for row in rows):
                        continue
                count += 1
                action = "Released" if apply else "Would release"
                self.stdout.write(f"{action} {entry.booking.booking_code}, payment {entry.payment_id}: "
                                  f"{entry.payment.currency} {entry.gross_amount}")
        self.stdout.write(f"{'Released' if apply else 'Eligible'} held payments: {count}")
