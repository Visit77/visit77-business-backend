from django.db import migrations, models


def copy_existing_prices(apps, schema_editor):
    RatePlan = apps.get_model("booking", "RatePlan")
    RatePeriod = apps.get_model("booking", "RatePeriod")
    DailyRate = apps.get_model("booking", "DailyRate")

    for plan in RatePlan.objects.all().iterator():
        plan.base_price = plan.default_price
        plan.extra_bed_base_price = plan.extra_bed_price
        if plan.currency == "USD":
            plan.usd_display_price = plan.default_price
            plan.extra_bed_usd_display_price = plan.extra_bed_price
        plan.save(update_fields=[
            "base_price",
            "usd_display_price",
            "extra_bed_base_price",
            "extra_bed_usd_display_price",
        ])

    for period in RatePeriod.objects.all().iterator():
        period.base_price = period.price
        if period.rate_plan.currency == "USD":
            period.usd_display_price = period.price
        period.save(update_fields=["base_price", "usd_display_price"])

    for daily_rate in DailyRate.objects.all().iterator():
        daily_rate.base_price = daily_rate.price
        if daily_rate.rate_plan.currency == "USD":
            daily_rate.usd_display_price = daily_rate.price
        daily_rate.save(update_fields=["base_price", "usd_display_price"])


class Migration(migrations.Migration):

    dependencies = [
        ("booking", "0010_addon_templates_and_requests"),
    ]

    operations = [
        migrations.AddField(
            model_name="hotel",
            name="base_currency",
            field=models.CharField(default="MMK", max_length=3),
        ),
        migrations.AddField(
            model_name="rateplan",
            name="base_price",
            field=models.DecimalField(decimal_places=2, default=0, max_digits=14),
        ),
        migrations.AddField(
            model_name="rateplan",
            name="usd_display_price",
            field=models.DecimalField(blank=True, decimal_places=2, max_digits=14, null=True),
        ),
        migrations.AddField(
            model_name="rateplan",
            name="extra_bed_base_price",
            field=models.DecimalField(decimal_places=2, default=0, max_digits=14),
        ),
        migrations.AddField(
            model_name="rateplan",
            name="extra_bed_usd_display_price",
            field=models.DecimalField(blank=True, decimal_places=2, max_digits=14, null=True),
        ),
        migrations.AddField(
            model_name="rateperiod",
            name="base_price",
            field=models.DecimalField(decimal_places=2, default=0, max_digits=14),
        ),
        migrations.AddField(
            model_name="rateperiod",
            name="usd_display_price",
            field=models.DecimalField(blank=True, decimal_places=2, max_digits=14, null=True),
        ),
        migrations.AddField(
            model_name="dailyrate",
            name="base_price",
            field=models.DecimalField(decimal_places=2, default=0, max_digits=14),
        ),
        migrations.AddField(
            model_name="dailyrate",
            name="usd_display_price",
            field=models.DecimalField(blank=True, decimal_places=2, max_digits=14, null=True),
        ),
        migrations.RunPython(copy_existing_prices, migrations.RunPython.noop),
    ]
