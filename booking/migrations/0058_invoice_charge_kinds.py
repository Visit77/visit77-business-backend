from django.db import migrations, models


def populate_charge_kinds(apps, schema_editor):
    for model_name in ("OTAInvoiceCharge", "PMSInvoiceCharge"):
        model = apps.get_model("booking", model_name)
        for charge in model.objects.all().iterator():
            normalized = (charge.title or "").strip().casefold()
            if charge.charge_type == "tax":
                charge.charge_kind = "tax" if normalized == "tax" else "other_tax"
            else:
                charge.charge_kind = (
                    "service_charge" if normalized == "service charge" else "other_charge"
                )
            charge.save(update_fields=["charge_kind"])


class Migration(migrations.Migration):

    dependencies = [
        ("booking", "0057_invoice_charge_calculation_basis"),
    ]

    operations = [
        migrations.AddField(
            model_name="otainvoicecharge",
            name="charge_kind",
            field=models.CharField(default="", max_length=32),
            preserve_default=False,
        ),
        migrations.AddField(
            model_name="pmsinvoicecharge",
            name="charge_kind",
            field=models.CharField(default="", max_length=32),
            preserve_default=False,
        ),
        migrations.RunPython(populate_charge_kinds, migrations.RunPython.noop),
        migrations.AlterField(
            model_name="otainvoicecharge",
            name="charge_kind",
            field=models.CharField(
                choices=[
                    ("tax", "Tax"),
                    ("other_tax", "Other Tax"),
                    ("service_charge", "Service Charge"),
                    ("other_charge", "Other Charge"),
                ],
                max_length=32,
            ),
        ),
        migrations.AlterField(
            model_name="pmsinvoicecharge",
            name="charge_kind",
            field=models.CharField(
                choices=[
                    ("tax", "Tax"),
                    ("other_tax", "Other Tax"),
                    ("service_charge", "Service Charge"),
                    ("other_charge", "Other Charge"),
                ],
                max_length=32,
            ),
        ),
    ]
