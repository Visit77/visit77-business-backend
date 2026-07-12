from django.db import migrations, models


def copy_existing_prices(apps, schema_editor):
    DailyRate = apps.get_model("booking", "DailyRate")
    RatePeriod = apps.get_model("booking", "RatePeriod")
    for model in (DailyRate, RatePeriod):
        model.objects.update(local_price=models.F("price"), foreigner_price=models.F("price"))


class Migration(migrations.Migration):
    dependencies = [("booking", "0005_rateperiod")]

    operations = [
        migrations.AddField(
            model_name="dailyrate",
            name="local_price",
            field=models.DecimalField(decimal_places=2, max_digits=14, null=True),
        ),
        migrations.AddField(
            model_name="dailyrate",
            name="foreigner_price",
            field=models.DecimalField(decimal_places=2, max_digits=14, null=True),
        ),
        migrations.AddField(
            model_name="rateperiod",
            name="local_price",
            field=models.DecimalField(decimal_places=2, max_digits=14, null=True),
        ),
        migrations.AddField(
            model_name="rateperiod",
            name="foreigner_price",
            field=models.DecimalField(decimal_places=2, max_digits=14, null=True),
        ),
        migrations.RunPython(copy_existing_prices, migrations.RunPython.noop),
        migrations.AlterField(
            model_name="dailyrate",
            name="local_price",
            field=models.DecimalField(decimal_places=2, max_digits=14),
        ),
        migrations.AlterField(
            model_name="dailyrate",
            name="foreigner_price",
            field=models.DecimalField(decimal_places=2, max_digits=14),
        ),
        migrations.AlterField(
            model_name="rateperiod",
            name="local_price",
            field=models.DecimalField(decimal_places=2, max_digits=14),
        ),
        migrations.AlterField(
            model_name="rateperiod",
            name="foreigner_price",
            field=models.DecimalField(decimal_places=2, max_digits=14),
        ),
        migrations.RemoveField(model_name="dailyrate", name="price"),
        migrations.RemoveField(model_name="rateperiod", name="price"),
    ]
