from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("booking", "0072_hotel_remittance_actor_payee_right"),
    ]

    operations = [
        migrations.AlterField(
            model_name="otaremittance",
            name="status",
            field=models.CharField(
                choices=[
                    ("accruing", "Current week accruing"),
                    ("pending", "Pending check"),
                    ("checked", "Checked"),
                    ("paid", "Paid"),
                    ("received", "Received"),
                    ("failed", "Failed"),
                ],
                db_index=True,
                default="pending",
                max_length=16,
            ),
        ),
    ]
