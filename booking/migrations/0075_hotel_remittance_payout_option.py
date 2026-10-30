from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("booking", "0074_ota_remittance_payment_details"),
    ]

    operations = [
        migrations.AddField(
            model_name="hotel",
            name="remittance_payout_option",
            field=models.CharField(
                choices=[
                    ("weekly", "One time a week"),
                    ("twice_weekly", "Two times a week"),
                ],
                db_index=True,
                default="weekly",
                max_length=24,
            ),
        ),
    ]
