from django.db import migrations, models

import booking.models


def remove_default_ota_tax(apps, schema_editor):
    Hotel = apps.get_model("booking", "Hotel")
    OTAInvoiceCharge = apps.get_model("booking", "OTAInvoiceCharge")

    OTAInvoiceCharge.objects.filter(
        charge_type="tax",
        charge_kind="tax",
        title__iexact="Tax",
        mode="included",
        value=0,
    ).delete()

    for hotel in Hotel.objects.all().iterator():
        config = dict(hotel.ota_invoice_charges or {})
        taxes = [
            rule for rule in config.get("taxes", [])
            if not (
                str(rule.get("title", "")).strip().casefold() == "tax"
                and rule.get("mode") == "included"
                and str(rule.get("value") or "0") in {"0", "0.0", "0.00"}
                and rule.get("charge_kind", "tax") == "tax"
            )
        ]
        normalized = {
            "taxes": taxes,
            "service_charges": list(config.get("service_charges", [])),
        }
        if normalized != hotel.ota_invoice_charges:
            hotel.ota_invoice_charges = normalized
            hotel.save(update_fields=["ota_invoice_charges"])


class Migration(migrations.Migration):

    dependencies = [
        ("booking", "0059_ota_inventory_close_now"),
    ]

    operations = [
        migrations.AlterField(
            model_name="hotel",
            name="ota_invoice_charges",
            field=models.JSONField(
                blank=True,
                default=booking.models.default_ota_invoice_charges,
            ),
        ),
        migrations.RunPython(remove_default_ota_tax, migrations.RunPython.noop),
    ]
