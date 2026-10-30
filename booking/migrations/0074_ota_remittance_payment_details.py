import booking.storage
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("booking", "0073_ota_remittance_accruing_status"),
    ]

    operations = [
        migrations.AddField(
            model_name="otaremittance",
            name="adjustment_amount",
            field=models.DecimalField(decimal_places=2, default=0, max_digits=14),
        ),
        migrations.AddField(
            model_name="otaremittance",
            name="adjustment_note",
            field=models.TextField(blank=True),
        ),
        migrations.AddField(
            model_name="otaremittance",
            name="payment_reference",
            field=models.CharField(blank=True, max_length=255),
        ),
        migrations.AddField(
            model_name="otaremittance",
            name="payment_receipt",
            field=models.FileField(
                blank=True,
                editable=False,
                null=True,
                storage=booking.storage.get_private_document_storage,
                upload_to="booking/remittance-receipts/%Y/%m/",
            ),
        ),
    ]
