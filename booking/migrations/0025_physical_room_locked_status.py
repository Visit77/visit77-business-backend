from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("booking", "0024_payment_payment_type"),
    ]

    operations = [
        migrations.AlterField(
            model_name="physicalroom",
            name="status",
            field=models.CharField(
                choices=[
                    ("vacant", "Vacant"),
                    ("occupied", "Occupied"),
                    ("cleaning", "Cleaning"),
                    ("out_of_service", "Out of service"),
                    ("locked", "Locked"),
                ],
                default="vacant",
                max_length=32,
            ),
        ),
    ]
