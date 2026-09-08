import datetime

from django.db import migrations, models


def backfill_default_times(apps, schema_editor):
    Hotel = apps.get_model("booking", "Hotel")
    Hotel.objects.filter(check_in_time__isnull=True).update(check_in_time=datetime.time(12, 0))
    Hotel.objects.filter(check_out_time__isnull=True).update(check_out_time=datetime.time(12, 0))


class Migration(migrations.Migration):
    dependencies = [("booking", "0044_payment_receipt_document")]

    operations = [
        migrations.AddField(
            model_name="hotel",
            name="timezone",
            field=models.CharField(default="UTC", max_length=64),
        ),
        migrations.RunPython(backfill_default_times, migrations.RunPython.noop),
        migrations.AlterField(
            model_name="hotel",
            name="check_in_time",
            field=models.TimeField(default=datetime.time(12, 0)),
        ),
        migrations.AlterField(
            model_name="hotel",
            name="check_out_time",
            field=models.TimeField(default=datetime.time(12, 0)),
        ),
    ]
