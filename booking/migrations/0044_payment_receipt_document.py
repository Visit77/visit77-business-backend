from django.db import migrations, models
import booking.storage


class Migration(migrations.Migration):

    dependencies = [
        ("booking", "0043_normalize_booking_source"),
    ]

    operations = [
        migrations.AddField(
            model_name="payment",
            name="receipt_pdf",
            field=models.FileField(
                blank=True,
                editable=False,
                null=True,
                storage=booking.storage.get_private_document_storage,
                upload_to="booking/receipts/%Y/%m/",
            ),
        ),
        migrations.AddField(
            model_name="payment",
            name="receipt_pdf_generated_at",
            field=models.DateTimeField(blank=True, editable=False, null=True),
        ),
        migrations.AddField(
            model_name="payment",
            name="receipt_snapshot",
            field=models.JSONField(blank=True, default=dict),
        ),
    ]
