import uuid

from django.db import migrations, models
import django.db.models.deletion


def backfill_invoices(apps, schema_editor):
    Booking = apps.get_model("booking", "Booking")
    Invoice = apps.get_model("booking", "Invoice")
    InvoiceLine = apps.get_model("booking", "InvoiceLine")
    Payment = apps.get_model("booking", "Payment")

    for booking in Booking.objects.all().iterator():
        payments = list(Payment.objects.filter(booking=booking).order_by("created_at", "id"))
        invoice_number = payments[0].invoice_number if payments else f"IV-{str(booking.id)[:8]}"
        paid = sum(
            (payment.amount - payment.refunded_amount for payment in payments if payment.status in ["paid", "partially_refunded"]),
            0,
        )
        status = "paid" if booking.grand_total > 0 and paid >= booking.grand_total else "partially_paid" if paid > 0 else "open"
        invoice = Invoice.objects.create(
            id=uuid.uuid4(),
            booking=booking,
            invoice_number=invoice_number,
            invoice_type="room_booking",
            status=status,
            currency=booking.currency,
            subtotal=booking.room_total + booking.add_on_total,
            tax_total=booking.tax_total,
            discount_total=booking.discount_total,
            total=booking.grand_total,
            note="Backfilled from the legacy booking bill.",
        )
        InvoiceLine.objects.create(
            invoice=invoice,
            description="Legacy stay charges",
            quantity=1,
            unit_price=booking.room_total + booking.add_on_total,
            total=booking.room_total + booking.add_on_total,
            metadata={"backfilled": True},
        )
        Payment.objects.filter(booking=booking).update(invoice=invoice)


class Migration(migrations.Migration):
    dependencies = [("booking", "0031_deduplicate_core_physical_rooms")]

    operations = [
        migrations.CreateModel(
            name="Invoice",
            fields=[
                ("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ("invoice_number", models.CharField(max_length=32, unique=True)),
                ("invoice_type", models.CharField(choices=[("room_booking", "Room booking"), ("stay_extension", "Stay extension"), ("extra_service", "Extra service"), ("damage", "Damage charge"), ("other", "Other")], default="other", max_length=24)),
                ("status", models.CharField(choices=[("open", "Open"), ("partially_paid", "Partially paid"), ("paid", "Paid"), ("void", "Void")], default="open", max_length=24)),
                ("currency", models.CharField(max_length=3)),
                ("subtotal", models.DecimalField(decimal_places=2, default=0, max_digits=14)),
                ("tax_total", models.DecimalField(decimal_places=2, default=0, max_digits=14)),
                ("discount_total", models.DecimalField(decimal_places=2, default=0, max_digits=14)),
                ("total", models.DecimalField(decimal_places=2, default=0, max_digits=14)),
                ("note", models.TextField(blank=True)),
                ("issued_at", models.DateTimeField(auto_now_add=True)),
                ("voided_at", models.DateTimeField(blank=True, null=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("booking", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="invoices", to="booking.booking")),
            ],
            options={"ordering": ["issued_at", "id"]},
        ),
        migrations.CreateModel(
            name="InvoiceLine",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("description", models.CharField(max_length=255)),
                ("quantity", models.DecimalField(decimal_places=2, default=1, max_digits=10)),
                ("unit_price", models.DecimalField(decimal_places=2, max_digits=14)),
                ("total", models.DecimalField(decimal_places=2, max_digits=14)),
                ("metadata", models.JSONField(blank=True, default=dict)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("invoice", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="lines", to="booking.invoice")),
            ],
            options={"ordering": ["id"]},
        ),
        migrations.AddField(
            model_name="payment",
            name="invoice",
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name="receipts", to="booking.invoice"),
        ),
        migrations.AlterField(
            model_name="payment",
            name="invoice_number",
            field=models.CharField(max_length=32),
        ),
        migrations.RunPython(backfill_invoices, migrations.RunPython.noop),
    ]
