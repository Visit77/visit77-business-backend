from django.db import migrations, models

import booking.models


def split_charge_configs(apps, schema_editor):
    Hotel = apps.get_model("booking", "Hotel")
    Booking = apps.get_model("booking", "Booking")
    Invoice = apps.get_model("booking", "Invoice")

    def normalize(config):
        config = dict(config or {})
        return {
            "taxes": config.get("taxes", []),
            "service_charges": config.get("service_charges", config.get("other_charges", [])),
        }

    for hotel in Hotel.objects.all().iterator():
        config = normalize(hotel.ota_invoice_charges)
        hotel.ota_invoice_charges = config
        hotel.pms_invoice_charges = config
        hotel.save(update_fields=["ota_invoice_charges", "pms_invoice_charges"])

    for booking in Booking.objects.all().iterator():
        booking.invoice_charge_snapshot = normalize(booking.invoice_charge_snapshot)
        booking.save(update_fields=["invoice_charge_snapshot"])

    for invoice in Invoice.objects.select_related("booking").all().iterator():
        is_initial = invoice.invoice_type == "room_booking"
        invoice.charge_scope = invoice.booking.source if is_initial else "pms"
        invoice.charge_snapshot = (
            invoice.booking.invoice_charge_snapshot if is_initial
            else {"taxes": [], "service_charges": []}
        )
        invoice.save(update_fields=["charge_scope", "charge_snapshot"])


class Migration(migrations.Migration):
    dependencies = [("booking", "0050_default_included_tax")]

    operations = [
        migrations.RenameField(
            model_name="hotel",
            old_name="invoice_charges",
            new_name="ota_invoice_charges",
        ),
        migrations.RenameField(
            model_name="booking",
            old_name="other_charge_total",
            new_name="service_charge_total",
        ),
        migrations.AddField(
            model_name="hotel",
            name="pms_invoice_charges",
            field=models.JSONField(blank=True, default=booking.models.default_invoice_charges),
        ),
        migrations.AddField(
            model_name="invoice",
            name="charge_scope",
            field=models.CharField(
                choices=[("ota", "OTA"), ("pms", "PMS")], default="pms", max_length=8,
            ),
        ),
        migrations.AddField(
            model_name="invoice",
            name="charge_snapshot",
            field=models.JSONField(blank=True, default=dict),
        ),
        migrations.RunPython(split_charge_configs, migrations.RunPython.noop),
    ]
