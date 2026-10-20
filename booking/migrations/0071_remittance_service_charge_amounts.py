from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("booking", "0070_ota_remittance_ledger_and_actors"),
    ]

    operations = [
        migrations.AddField(
            model_name="otaremittance",
            name="service_charge_amount",
            field=models.DecimalField(decimal_places=2, default=0, max_digits=14),
        ),
        migrations.AddField(
            model_name="otaremittanceitem",
            name="service_charge_amount",
            field=models.DecimalField(decimal_places=2, default=0, max_digits=14),
        ),
        migrations.AddField(
            model_name="otarevenueledger",
            name="service_charge_amount",
            field=models.DecimalField(decimal_places=2, default=0, max_digits=14),
        ),
    ]
