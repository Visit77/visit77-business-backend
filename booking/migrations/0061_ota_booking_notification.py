from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    dependencies = [("booking", "0060_remove_default_ota_tax")]

    operations = [
        migrations.CreateModel(
            name="OTABookingNotification",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("event_type", models.CharField(choices=[("new", "New booking"), ("updated", "Updated booking"), ("cancelled", "Cancelled booking")], max_length=16)),
                ("body", models.CharField(max_length=225)),
                ("payload", models.JSONField(blank=True, default=dict)),
                ("read", models.BooleanField(default=False)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("booking", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="ota_notifications", to="booking.booking")),
                ("hotel", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="ota_notifications", to="booking.hotel")),
            ],
            options={"ordering": ["-created_at", "-id"]},
        ),
        migrations.AddIndex(
            model_name="otabookingnotification",
            index=models.Index(fields=["hotel", "event_type", "read", "created_at"], name="ota_noti_hotel_filter_idx"),
        ),
    ]
