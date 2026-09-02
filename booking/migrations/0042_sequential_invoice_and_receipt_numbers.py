from django.db import migrations, models


DOCUMENT_CODES_PER_SERIES = 9_999_999


def format_document_code(sequence_value, prefix):
    series_index, number = divmod(sequence_value - 1, DOCUMENT_CODES_PER_SERIES)
    if series_index >= 26:
        raise RuntimeError("Document code series A-Z has been exhausted.")
    return f"{prefix}{chr(ord('A') + series_index)}{number + 1:07d}"


def populate_document_numbers(apps, schema_editor):
    Invoice = apps.get_model("booking", "Invoice")
    InvoiceNumberSequence = apps.get_model("booking", "InvoiceNumberSequence")
    Payment = apps.get_model("booking", "Payment")
    ReceiptNumberSequence = apps.get_model("booking", "ReceiptNumberSequence")

    # Avoid collisions if an existing value already happens to use the new format.
    for invoice in Invoice.objects.order_by("issued_at", "pk").iterator(chunk_size=1000):
        invoice.invoice_number = f"MIG-{invoice.pk.hex[:28]}"
        invoice.save(update_fields=["invoice_number"])

    invoice_count = 0
    invoice_batch = []
    for invoice in Invoice.objects.order_by("issued_at", "pk").iterator(chunk_size=1000):
        invoice_count += 1
        invoice.invoice_number = format_document_code(invoice_count, "V77-INV-")
        invoice_batch.append(invoice)
        if len(invoice_batch) == 1000:
            Invoice.objects.bulk_update(invoice_batch, ["invoice_number"])
            invoice_batch = []
    if invoice_batch:
        Invoice.objects.bulk_update(invoice_batch, ["invoice_number"])
    InvoiceNumberSequence.objects.update_or_create(pk=1, defaults={"last_value": invoice_count})

    Payment.objects.update(receipt_number=None)
    receipt_count = 0
    receipt_batch = []
    receipt_statuses = ["paid", "refunded", "partially_refunded"]
    for payment in Payment.objects.filter(status__in=receipt_statuses).order_by(
        "paid_at", "created_at", "pk"
    ).iterator(chunk_size=1000):
        receipt_count += 1
        payment.receipt_number = format_document_code(receipt_count, "V77-REC-")
        receipt_batch.append(payment)
        if len(receipt_batch) == 1000:
            Payment.objects.bulk_update(receipt_batch, ["receipt_number"])
            receipt_batch = []
    if receipt_batch:
        Payment.objects.bulk_update(receipt_batch, ["receipt_number"])
    ReceiptNumberSequence.objects.update_or_create(pk=1, defaults={"last_value": receipt_count})

    # This field is retained as an invoice-number snapshot for compatibility.
    for payment in Payment.objects.select_related("invoice").exclude(invoice=None).iterator(chunk_size=1000):
        Payment.objects.filter(pk=payment.pk).update(invoice_number=payment.invoice.invoice_number)


class Migration(migrations.Migration):

    dependencies = [
        ("booking", "0041_guest_profiles_and_booking_owner"),
    ]

    operations = [
        migrations.CreateModel(
            name="InvoiceNumberSequence",
            fields=[
                (
                    "id",
                    models.PositiveSmallIntegerField(
                        default=1, editable=False, primary_key=True, serialize=False
                    ),
                ),
                ("last_value", models.PositiveBigIntegerField(default=0)),
            ],
        ),
        migrations.CreateModel(
            name="ReceiptNumberSequence",
            fields=[
                (
                    "id",
                    models.PositiveSmallIntegerField(
                        default=1, editable=False, primary_key=True, serialize=False
                    ),
                ),
                ("last_value", models.PositiveBigIntegerField(default=0)),
            ],
        ),
        migrations.AlterField(
            model_name="invoice",
            name="invoice_number",
            field=models.CharField(blank=True, editable=False, max_length=32, unique=True),
        ),
        migrations.RunPython(populate_document_numbers, migrations.RunPython.noop),
    ]
