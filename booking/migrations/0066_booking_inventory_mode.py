from django.db import migrations, models


def backfill_booking_inventory_mode(apps, schema_editor):
    Booking = apps.get_model("booking", "Booking")
    Booking.objects.filter(
        source="pms",
        hotel__inventory_mode="room_type_count",
    ).update(inventory_mode="room_type_count")
    Booking.objects.filter(source="ota").exclude(
        rooms__assignments__isnull=False,
    ).update(inventory_mode="room_type_count")


class Migration(migrations.Migration):
    dependencies = [
        ("booking", "0065_roomtype_room_type_count_configured"),
    ]

    operations = [
        migrations.AddField(
            model_name="booking",
            name="inventory_mode",
            field=models.CharField(
                choices=[
                    ("room_type_count", "Room type count"),
                    ("physical_room", "Physical room"),
                ],
                default="physical_room",
                max_length=24,
            ),
        ),
        migrations.RunPython(
            backfill_booking_inventory_mode,
            migrations.RunPython.noop,
        ),
    ]
