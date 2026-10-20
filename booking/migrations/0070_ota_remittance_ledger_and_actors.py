import uuid

from django.db import migrations, models
import django.db.models.deletion


def migrate_processing_status(apps, schema_editor):
    apps.get_model("booking", "OTARemittance").objects.filter(status="processing").update(status="checked")


class Migration(migrations.Migration):
    dependencies = [("booking", "0069_booking_is_no_show")]

    operations = [
        migrations.AddField(
            model_name="booking", name="settlement_status",
            field=models.CharField(
                choices=[("unpaid", "Unpaid"), ("held", "Held"), ("available", "Available"),
                         ("pending_remittance", "Pending remittance"), ("checked", "Checked"),
                         ("paid", "Paid"), ("received", "Received")],
                db_index=True, default="unpaid", max_length=24,
            ),
        ),
        migrations.AddField(model_name="booking", name="settled_at", field=models.DateTimeField(blank=True, null=True)),
        migrations.CreateModel(
            name="OTACommissionRate",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("package", models.CharField(choices=[("free", "Free Hotel"), ("pms", "PMS Only"), ("ota", "OTA Only"), ("ota_pms", "OTA + PMS")], max_length=24, unique=True)),
                ("rate", models.DecimalField(decimal_places=4, default=5, max_digits=7)),
                ("updated_at", models.DateTimeField(auto_now=True)),
            ],
        ),
        migrations.CreateModel(
            name="OTARemittanceActor",
            fields=[
                ("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ("scope", models.CharField(choices=[("admin", "Visit77 admin"), ("hotel", "Hotel")], max_length=12)),
                ("name", models.CharField(max_length=150)),
                ("passcode_hash", models.CharField(max_length=255)),
                ("access_rights", models.JSONField(blank=True, default=list)),
                ("is_active", models.BooleanField(db_index=True, default=True)),
                ("removed_at", models.DateTimeField(blank=True, null=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("hotel", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.CASCADE, related_name="remittance_actors", to="booking.hotel")),
            ],
            options={"ordering": ["scope", "name", "id"]},
        ),
        migrations.AddField(model_name="otaremittance", name="released_held_funds_amount", field=models.DecimalField(decimal_places=2, default=0, max_digits=14)),
        migrations.AddField(model_name="otaremittance", name="tax_amount", field=models.DecimalField(decimal_places=2, default=0, max_digits=14)),
        migrations.AddField(model_name="otaremittance", name="commission_rate", field=models.DecimalField(decimal_places=4, default=5, max_digits=7)),
        migrations.AddField(model_name="otaremittance", name="system_generated", field=models.BooleanField(default=True)),
        migrations.AddField(model_name="otaremittance", name="historical_backfill", field=models.BooleanField(default=False)),
        migrations.AddField(model_name="otaremittance", name="checked_at", field=models.DateTimeField(blank=True, null=True)),
        migrations.AddField(model_name="otaremittance", name="received_at", field=models.DateTimeField(blank=True, null=True)),
        migrations.AddField(model_name="otaremittance", name="checked_by", field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="checked_remittances", to="booking.otaremittanceactor")),
        migrations.AddField(model_name="otaremittance", name="paid_by", field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="paid_remittances", to="booking.otaremittanceactor")),
        migrations.AddField(model_name="otaremittance", name="received_by", field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="received_remittances", to="booking.otaremittanceactor")),
        migrations.AlterField(
            model_name="otaremittance", name="status",
            field=models.CharField(choices=[("pending", "Pending check"), ("checked", "Checked"), ("paid", "Paid"), ("received", "Received"), ("failed", "Failed")], db_index=True, default="pending", max_length=16),
        ),
        migrations.RunPython(migrate_processing_status, migrations.RunPython.noop),
        migrations.CreateModel(
            name="OTARevenueLedger",
            fields=[
                ("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ("event_key", models.CharField(max_length=180, unique=True)),
                ("event_type", models.CharField(choices=[("booking_collection", "Booking collection"), ("held", "Held fund"), ("held_release", "Release of held funds"), ("refund", "Refund")], db_index=True, max_length=24)),
                ("gross_amount", models.DecimalField(decimal_places=2, default=0, max_digits=14)),
                ("tax_amount", models.DecimalField(decimal_places=2, default=0, max_digits=14)),
                ("refund_amount", models.DecimalField(decimal_places=2, default=0, max_digits=14)),
                ("effective_at", models.DateTimeField(db_index=True)),
                ("metadata", models.JSONField(blank=True, default=dict)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("booking", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="ota_revenue_ledger", to="booking.booking")),
                ("hotel", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="ota_revenue_ledger", to="booking.hotel")),
                ("payment", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="ota_revenue_ledger", to="booking.payment")),
            ], options={"ordering": ["effective_at", "id"]},
        ),
        migrations.CreateModel(
            name="OTARemittanceItem",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("item_type", models.CharField(choices=[("booking_collection", "Booking collection"), ("held", "Held fund"), ("held_release", "Release of held funds"), ("refund", "Refund")], max_length=24)),
                ("gross_amount", models.DecimalField(decimal_places=2, default=0, max_digits=14)),
                ("tax_amount", models.DecimalField(decimal_places=2, default=0, max_digits=14)),
                ("refund_amount", models.DecimalField(decimal_places=2, default=0, max_digits=14)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("booking", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="remittance_items", to="booking.booking")),
                ("ledger_entry", models.OneToOneField(on_delete=django.db.models.deletion.PROTECT, related_name="remittance_item", to="booking.otarevenueledger")),
                ("remittance", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="items", to="booking.otaremittance")),
            ],
        ),
        migrations.CreateModel(
            name="OTARemittanceAuditLog",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("action", models.CharField(max_length=32)),
                ("actor_name", models.CharField(default="System", max_length=150)),
                ("metadata", models.JSONField(blank=True, default=dict)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("actor", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, to="booking.otaremittanceactor")),
                ("remittance", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="audit_logs", to="booking.otaremittance")),
            ], options={"ordering": ["created_at", "id"]},
        ),
    ]
