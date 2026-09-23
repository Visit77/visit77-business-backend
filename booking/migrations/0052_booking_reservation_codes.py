from zoneinfo import ZoneInfo

from django.db import migrations, models


def backfill_pms_reservation_codes(apps, schema_editor):
    Booking = apps.get_model("booking", "Booking")
    HotelDocumentSequence = apps.get_model("booking", "HotelDocumentSequence")

    bookings = (
        Booking.objects.filter(source="pms", reservation_code__isnull=True)
        .select_related("hotel")
        .order_by("hotel_id", "created_at", "id")
    )
    for booking in bookings.iterator():
        created_at = booking.created_at
        try:
            year = created_at.astimezone(ZoneInfo(booking.hotel.timezone)).year
        except (KeyError, ValueError):
            year = created_at.year
        sequence, _ = HotelDocumentSequence.objects.get_or_create(
            hotel_id=booking.hotel_id,
            year=year,
            kind="RES",
            defaults={"last_value": 0},
        )
        sequence.last_value += 1
        if sequence.last_value > 999999:
            raise RuntimeError("Hotel reservation sequence exhausted during migration.")
        sequence.save(update_fields=["last_value"])
        booking.reservation_code = (
            f"{booking.hotel.document_code}-RES-{year % 100:02d}-{sequence.last_value:06d}"
        )
        booking.save(update_fields=["reservation_code"])


class Migration(migrations.Migration):

    dependencies = [
        ("booking", "0051_split_ota_pms_invoice_charges"),
    ]

    operations = [
        migrations.AddField(
            model_name="booking",
            name="reservation_code",
            field=models.CharField(
                blank=True,
                editable=False,
                max_length=24,
                null=True,
                unique=True,
            ),
        ),
        migrations.AlterField(
            model_name="hoteldocumentsequence",
            name="kind",
            field=models.CharField(
                choices=[
                    ("INV", "Invoice"),
                    ("REC", "Receipt"),
                    ("RES", "Reservation"),
                ],
                max_length=3,
            ),
        ),
        migrations.RunPython(backfill_pms_reservation_codes, migrations.RunPython.noop),
    ]
