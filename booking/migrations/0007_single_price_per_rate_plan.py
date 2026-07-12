from django.db import migrations, models
from django.db.models import F


def copy_market_price(apps, schema_editor):
    for model_name in ("DailyRate", "RatePeriod"):
        model = apps.get_model("booking", model_name)
        model.objects.filter(rate_plan__guest_market="foreign").update(price=F("foreigner_price"))
        model.objects.exclude(rate_plan__guest_market="foreign").update(price=F("local_price"))


def restore_dual_prices(apps, schema_editor):
    for model_name in ("DailyRate", "RatePeriod"):
        model = apps.get_model("booking", model_name)
        model.objects.update(local_price=F("price"), foreigner_price=F("price"))


class Migration(migrations.Migration):
    dependencies = [("booking", "0006_local_and_foreigner_override_prices")]

    operations = [
        migrations.AddField(
            model_name="dailyrate",
            name="price",
            field=models.DecimalField(decimal_places=2, max_digits=14, null=True),
        ),
        migrations.AddField(
            model_name="rateperiod",
            name="price",
            field=models.DecimalField(decimal_places=2, max_digits=14, null=True),
        ),
        migrations.RunPython(copy_market_price, restore_dual_prices),
        migrations.AlterField(
            model_name="dailyrate",
            name="price",
            field=models.DecimalField(decimal_places=2, max_digits=14),
        ),
        migrations.AlterField(
            model_name="rateperiod",
            name="price",
            field=models.DecimalField(decimal_places=2, max_digits=14),
        ),
        migrations.RemoveField(model_name="dailyrate", name="local_price"),
        migrations.RemoveField(model_name="dailyrate", name="foreigner_price"),
        migrations.RemoveField(model_name="rateperiod", name="local_price"),
        migrations.RemoveField(model_name="rateperiod", name="foreigner_price"),
    ]
