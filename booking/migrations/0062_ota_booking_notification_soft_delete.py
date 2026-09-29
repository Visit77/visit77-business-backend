from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("booking", "0061_ota_booking_notification")]

    operations = [
        migrations.AddField(
            model_name="otabookingnotification",
            name="is_deleted",
            field=models.BooleanField(default=False),
        ),
    ]
