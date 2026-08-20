from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("booking", "0032_stay_bill_invoices")]

    operations = [
        migrations.AddField(
            model_name="physicalroom",
            name="ota_enabled",
            field=models.BooleanField(default=True),
        ),
    ]
