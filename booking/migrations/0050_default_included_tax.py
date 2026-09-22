from django.db import migrations, models

import booking.models


def default_existing_hotel_tax(apps, schema_editor):
    Hotel = apps.get_model("booking", "Hotel")
    Hotel.objects.filter(invoice_charges={}).update(
        invoice_charges=booking.models.default_invoice_charges()
    )


class Migration(migrations.Migration):
    dependencies = [("booking", "0049_shared_invoice_charges")]

    operations = [
        migrations.AlterField(
            model_name="hotel",
            name="invoice_charges",
            field=models.JSONField(blank=True, default=booking.models.default_invoice_charges),
        ),
        migrations.RunPython(default_existing_hotel_tax, migrations.RunPython.noop),
    ]
