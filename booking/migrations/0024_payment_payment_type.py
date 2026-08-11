from django.db import migrations, models


VALID_PAYMENT_TYPES = {"deposit", "full_payment", "balance"}


def backfill_payment_types(apps, schema_editor):
    Payment = apps.get_model("booking", "Payment")
    for payment in Payment.objects.only("id", "metadata").iterator():
        metadata = payment.metadata or {}
        payment_type = metadata.get("payment_type")
        if payment_type in VALID_PAYMENT_TYPES:
            Payment.objects.filter(pk=payment.pk).update(payment_type=payment_type)


class Migration(migrations.Migration):

    dependencies = [
        ("booking", "0023_identity_photo_document_type"),
    ]

    operations = [
        migrations.AddField(
            model_name="payment",
            name="payment_type",
            field=models.CharField(
                choices=[
                    ("deposit", "Deposit"),
                    ("full_payment", "Full payment"),
                    ("balance", "Remaining balance"),
                ],
                default="full_payment",
                max_length=24,
            ),
        ),
        migrations.RunPython(backfill_payment_types, migrations.RunPython.noop),
    ]
