from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("booking", "0055_remove_otainvoicecharge_uniq_ota_invoice_charge_title_and_more"),
    ]

    operations = [
        migrations.CreateModel(
            name="OTADocumentYearSequence",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("year", models.PositiveSmallIntegerField()),
                ("kind", models.CharField(choices=[("INV", "Invoice"), ("REC", "Receipt")], max_length=3)),
                ("last_value", models.PositiveIntegerField(default=0)),
            ],
        ),
        migrations.AddConstraint(
            model_name="otadocumentyearsequence",
            constraint=models.UniqueConstraint(
                fields=("year", "kind"),
                name="uniq_ota_document_year_kind",
            ),
        ),
    ]
