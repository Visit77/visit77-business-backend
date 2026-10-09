from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ("booking", "0067_add_default_pms_invoice_charges"),
    ]

    operations = [
        migrations.CreateModel(
            name="OTABookingFollowUp",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("status", models.CharField(choices=[("pending", "Not contacted yet"), ("informed", "Hotel informed"), ("unreachable", "Hotel unreachable"), ("no_action_required", "No action required")], db_index=True, default="pending", max_length=24)),
                ("note", models.TextField(blank=True)),
                ("contact_channel", models.CharField(blank=True, choices=[("phone", "Phone"), ("email", "Email"), ("message", "Message"), ("other", "Other")], max_length=16)),
                ("informed_at", models.DateTimeField(blank=True, null=True)),
                ("informed_by_core_user_id", models.PositiveBigIntegerField(blank=True, null=True)),
                ("updated_by_core_user_id", models.PositiveBigIntegerField(blank=True, null=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("booking", models.OneToOneField(on_delete=django.db.models.deletion.CASCADE, related_name="ota_follow_up", to="booking.booking")),
            ],
            options={"ordering": ["-updated_at", "-id"]},
        ),
        migrations.CreateModel(
            name="OTARemittance",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("period_from", models.DateField()),
                ("period_to", models.DateField()),
                ("currency", models.CharField(default="MMK", max_length=3)),
                ("gross_booking_amount", models.DecimalField(decimal_places=2, default=0, max_digits=14)),
                ("refund_amount", models.DecimalField(decimal_places=2, default=0, max_digits=14)),
                ("commission_amount", models.DecimalField(decimal_places=2, default=0, max_digits=14)),
                ("net_remittance_amount", models.DecimalField(decimal_places=2, default=0, max_digits=14)),
                ("status", models.CharField(choices=[("pending", "Pending"), ("processing", "Processing"), ("paid", "Paid"), ("failed", "Failed")], db_index=True, default="pending", max_length=16)),
                ("reference", models.CharField(blank=True, max_length=120)),
                ("paid_at", models.DateTimeField(blank=True, null=True)),
                ("processed_by_core_user_id", models.PositiveBigIntegerField(blank=True, null=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("hotel", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="ota_remittances", to="booking.hotel")),
            ],
            options={"ordering": ["-period_to", "-id"]},
        ),
        migrations.AddConstraint(
            model_name="otaremittance",
            constraint=models.UniqueConstraint(fields=("hotel", "period_from", "period_to", "currency"), name="uniq_ota_remittance_period_currency"),
        ),
        migrations.AddIndex(
            model_name="otaremittance",
            index=models.Index(fields=["hotel", "period_from", "period_to", "status"], name="ota_remit_hotel_period_idx"),
        ),
    ]
