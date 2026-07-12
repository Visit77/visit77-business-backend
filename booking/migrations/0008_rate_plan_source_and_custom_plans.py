from django.db import migrations, models


def classify_existing_rate_plans(apps, schema_editor):
    RatePlan = apps.get_model("booking", "RatePlan")
    RatePlan.objects.exclude(core_rate_plan_id="").update(source="core", is_default=True)


class Migration(migrations.Migration):
    dependencies = [("booking", "0007_single_price_per_rate_plan")]

    operations = [
        migrations.AddField(
            model_name="rateplan",
            name="source",
            field=models.CharField(
                choices=[("core", "Core generated"), ("booking", "Booking Engine")],
                default="booking",
                max_length=16,
            ),
        ),
        migrations.AddField(
            model_name="rateplan",
            name="is_default",
            field=models.BooleanField(default=False),
        ),
        migrations.RunPython(classify_existing_rate_plans, migrations.RunPython.noop),
        migrations.AddConstraint(
            model_name="rateplan",
            constraint=models.UniqueConstraint(
                condition=~models.Q(core_rate_plan_id=""),
                fields=("core_rate_plan_id",),
                name="uniq_nonblank_core_rate_plan_id",
            ),
        ),
    ]
