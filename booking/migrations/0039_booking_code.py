from django.db import migrations, models


BOOKING_CODES_PER_SERIES = 9_999_999


def format_booking_code(sequence_value):
    series_index, number = divmod(sequence_value - 1, BOOKING_CODES_PER_SERIES)
    if series_index >= 26:
        raise RuntimeError("Booking code series A-Z has been exhausted.")
    return f"V77H-{chr(ord('A') + series_index)}{number + 1:08d}"


def populate_booking_codes(apps, schema_editor):
    Booking = apps.get_model("booking", "Booking")
    BookingCodeSequence = apps.get_model("booking", "BookingCodeSequence")

    last_value = 0
    batch = []
    for booking in Booking.objects.order_by("created_at", "pk").iterator(chunk_size=1000):
        last_value += 1
        booking.booking_code = format_booking_code(last_value)
        batch.append(booking)
        if len(batch) == 1000:
            Booking.objects.bulk_update(batch, ["booking_code"])
            batch = []
    if batch:
        Booking.objects.bulk_update(batch, ["booking_code"])

    BookingCodeSequence.objects.update_or_create(pk=1, defaults={"last_value": last_value})


class Migration(migrations.Migration):

    dependencies = [
        ("booking", "0038_alter_hotel_phone"),
    ]

    operations = [
        migrations.CreateModel(
            name="BookingCodeSequence",
            fields=[
                (
                    "id",
                    models.PositiveSmallIntegerField(
                        default=1, editable=False, primary_key=True, serialize=False
                    ),
                ),
                ("last_value", models.PositiveBigIntegerField(default=0)),
            ],
        ),
        migrations.AddField(
            model_name="booking",
            name="booking_code",
            field=models.CharField(blank=True, max_length=15, null=True, unique=True),
        ),
        migrations.RunPython(populate_booking_codes, migrations.RunPython.noop),
        migrations.AlterField(
            model_name="booking",
            name="booking_code",
            field=models.CharField(editable=False, max_length=15, unique=True),
        ),
    ]
