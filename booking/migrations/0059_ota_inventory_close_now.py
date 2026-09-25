from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("booking", "0058_invoice_charge_kinds"),
    ]

    operations = [
        migrations.AddField(
            model_name="otainventoryclosure",
            name="closure_mode",
            field=models.CharField(
                choices=[("scheduled", "Scheduled"), ("close_now", "Close now")],
                default="scheduled",
                max_length=16,
            ),
        ),
        migrations.AddField(
            model_name="otainventoryclosure",
            name="reopened_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AlterField(
            model_name="otainventoryclosure",
            name="end_date",
            field=models.DateField(blank=True, null=True),
        ),
    ]
