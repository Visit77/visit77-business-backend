from decimal import Decimal

from django.db import migrations, models
import django.db.models.deletion


def copy_json_charges_to_tables(apps, schema_editor):
    Hotel = apps.get_model("booking", "Hotel")
    OTAInvoiceCharge = apps.get_model("booking", "OTAInvoiceCharge")
    PMSInvoiceCharge = apps.get_model("booking", "PMSInvoiceCharge")

    for hotel in Hotel.objects.all().iterator():
        for model, field_name in (
            (OTAInvoiceCharge, "ota_invoice_charges"),
            (PMSInvoiceCharge, "pms_invoice_charges"),
        ):
            config = getattr(hotel, field_name) or {}
            records = []
            seen = set()
            for charge_type, category in (("tax", "taxes"), ("service", "service_charges")):
                for index, rule in enumerate(config.get(category, []) or []):
                    title = str(rule.get("title") or "").strip()
                    key = (charge_type, title)
                    if not title or key in seen:
                        continue
                    seen.add(key)
                    records.append(model(
                        hotel_id=hotel.id,
                        charge_type=charge_type,
                        title=title,
                        mode=rule.get("mode") or "fixed",
                        value=Decimal(str(rule.get("value") or 0)),
                        sort_order=index,
                    ))
            model.objects.bulk_create(records)


class Migration(migrations.Migration):

    dependencies = [
        ("booking", "0052_booking_reservation_codes"),
    ]

    operations = [
        migrations.CreateModel(
            name="OTAInvoiceCharge",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("charge_type", models.CharField(choices=[("tax", "Tax"), ("service", "Service")], max_length=16)),
                ("title", models.CharField(max_length=100)),
                ("mode", models.CharField(choices=[("do_not_show", "Do not show"), ("included", "Included"), ("percentage", "Percentage"), ("fixed", "Fixed amount")], max_length=20)),
                ("value", models.DecimalField(decimal_places=2, default=0, max_digits=12)),
                ("sort_order", models.PositiveSmallIntegerField(default=0)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("hotel", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, to="booking.hotel")),
            ],
            options={"ordering": ["charge_type", "sort_order", "id"]},
        ),
        migrations.CreateModel(
            name="PMSInvoiceCharge",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("charge_type", models.CharField(choices=[("tax", "Tax"), ("service", "Service")], max_length=16)),
                ("title", models.CharField(max_length=100)),
                ("mode", models.CharField(choices=[("do_not_show", "Do not show"), ("included", "Included"), ("percentage", "Percentage"), ("fixed", "Fixed amount")], max_length=20)),
                ("value", models.DecimalField(decimal_places=2, default=0, max_digits=12)),
                ("sort_order", models.PositiveSmallIntegerField(default=0)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("hotel", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, to="booking.hotel")),
            ],
            options={"ordering": ["charge_type", "sort_order", "id"]},
        ),
        migrations.AddConstraint(
            model_name="otainvoicecharge",
            constraint=models.UniqueConstraint(fields=("hotel", "charge_type", "title"), name="uniq_ota_invoice_charge_title"),
        ),
        migrations.AddConstraint(
            model_name="pmsinvoicecharge",
            constraint=models.UniqueConstraint(fields=("hotel", "charge_type", "title"), name="uniq_pms_invoice_charge_title"),
        ),
        migrations.RunPython(copy_json_charges_to_tables, migrations.RunPython.noop),
    ]
