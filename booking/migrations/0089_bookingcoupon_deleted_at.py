from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("booking", "0088_hide_booking_from_my_bookings")]

    operations = [
        migrations.AddField(
            model_name="bookingcoupon",
            name="deleted_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
    ]
