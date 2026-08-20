from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("booking", "0033_physicalroom_ota_enabled")]

    operations = [
        migrations.AddField(
            model_name="physicalroom",
            name="ota_sale_open",
            field=models.BooleanField(default=True),
        ),
    ]
