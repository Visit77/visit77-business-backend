from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("booking", "0046_default_non_refundable_hotel_policy")]

    operations = [
        migrations.AddField(
            model_name="hotel",
            name="ota_invoice_charges",
            field=models.JSONField(blank=True, default=dict),
        ),
        migrations.AddField(
            model_name="booking",
            name="ota_invoice_charge_snapshot",
            field=models.JSONField(blank=True, default=dict),
        ),
        migrations.AddField(
            model_name="booking",
            name="other_charge_total",
            field=models.DecimalField(decimal_places=2, default=0, max_digits=14),
        ),
    ]
