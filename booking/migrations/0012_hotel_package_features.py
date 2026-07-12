from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("booking", "0011_base_currency_and_display_prices"),
    ]

    operations = [
        migrations.AddField(
            model_name="hotel",
            name="package",
            field=models.CharField(
                choices=[
                    ("free", "Free Hotel"),
                    ("pms", "PMS Only"),
                    ("ota", "OTA Only"),
                    ("ota_pms", "OTA + PMS"),
                ],
                default="ota",
                max_length=24,
            ),
        ),
        migrations.AddField(
            model_name="hotel",
            name="features",
            field=models.JSONField(blank=True, default=dict),
        ),
    ]
