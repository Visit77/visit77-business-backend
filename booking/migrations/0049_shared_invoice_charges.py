from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [("booking", "0048_hotel_document_codes")]

    operations = [
        migrations.RenameField(
            model_name="hotel",
            old_name="ota_invoice_charges",
            new_name="invoice_charges",
        ),
        migrations.RenameField(
            model_name="booking",
            old_name="ota_invoice_charge_snapshot",
            new_name="invoice_charge_snapshot",
        ),
    ]
