from django.db import migrations, models


def normalize_booking_sources(apps, schema_editor):
    Booking = apps.get_model("booking", "Booking")
    Booking.objects.filter(source__in=["direct"]).update(source="ota")
    Booking.objects.filter(source__in=["phone", "walk_in"]).update(source="pms")
    # Defensive normalization for any historical/custom value.
    Booking.objects.exclude(source__in=["ota", "pms"]).update(source="pms")


class Migration(migrations.Migration):

    dependencies = [
        ("booking", "0042_sequential_invoice_and_receipt_numbers"),
    ]

    operations = [
        migrations.RunPython(normalize_booking_sources, migrations.RunPython.noop),
        migrations.AlterField(
            model_name="booking",
            name="source",
            field=models.CharField(
                choices=[("ota", "OTA"), ("pms", "PMS")],
                default="ota",
                max_length=24,
            ),
        ),
    ]
