from uuid import UUID

from django.core.management.base import BaseCommand
from django.db import transaction

from booking.models import Booking, Invoice, Payment
from booking.money_display import round_mmk
from booking.services import _apply_invoice_rounding, _sync_invoice_status


class Command(BaseCommand):
    help = "Close MMK invoices whose recorded payments cover the rounded payable total."

    def add_arguments(self, parser):
        parser.add_argument("--apply", action="store_true", help="Save changes. The default is preview.")
        parser.add_argument("--business-id", type=int, help="Limit to a Core business ID.")
        parser.add_argument("--invoice-id", type=UUID, help="Limit to one invoice UUID.")

    def handle(self, *args, **options):
        invoices = Invoice.objects.filter(
            currency="MMK",
            status__in=[Invoice.Status.OPEN, Invoice.Status.PARTIALLY_PAID],
            receipts__status=Payment.Status.PAID,
        ).distinct()
        if options["business_id"] is not None:
            invoices = invoices.filter(booking__hotel__core_business_id=options["business_id"])
        if options["invoice_id"]:
            invoices = invoices.filter(pk=options["invoice_id"])

        apply = options["apply"]
        count = 0
        for invoice_id, booking_id in list(invoices.values_list("pk", "booking_id")):
            result = self.close_invoice(invoice_id, booking_id, apply)
            if result:
                number, old_total, paid = result
                prefix = "Closed" if apply else "Would close"
                self.stdout.write(f"{prefix} {number} ({invoice_id}): MMK {old_total} → {paid}")
                count += 1
        self.stdout.write(f"{'Closed' if apply else 'Eligible'} invoices: {count}")

    @staticmethod
    @transaction.atomic
    def close_invoice(invoice_id, booking_id, apply):
        # Payment recording locks the booking first.
        booking = Booking.objects.select_for_update().get(pk=booking_id)
        invoice = Invoice.objects.select_for_update().filter(pk=invoice_id, booking=booking).first()
        if (
            not invoice or invoice.currency != "MMK" or booking.currency != "MMK"
            or invoice.status not in (Invoice.Status.OPEN, Invoice.Status.PARTIALLY_PAID)
        ):
            return None
        if invoice.receipts.filter(status__in=[
            Payment.Status.PENDING, Payment.Status.REFUNDED, Payment.Status.PARTIALLY_REFUNDED,
        ]).exists():
            return None

        successful = invoice.receipts.filter(status=Payment.Status.PAID)
        if successful.filter(paid_at__isnull=True).exists() or successful.filter(refunded_amount__gt=0).exists():
            return None

        paid = invoice.paid_amount
        old_total = invoice.total
        difference = old_total - paid
        if (
            paid <= 0 or difference <= 0 or round_mmk(old_total, "MMK") != paid
            or invoice.subtotal + invoice.tax_total - invoice.discount_total != old_total
            or booking.grand_total < difference
        ):
            return None
        if apply:
            _apply_invoice_rounding(invoice)
            invoice.save(update_fields=["subtotal", "total", "updated_at"])
            _sync_invoice_status(invoice)
            booking.grand_total -= difference
            booking.save(update_fields=["grand_total", "updated_at"])
        return invoice.invoice_number, old_total, paid
