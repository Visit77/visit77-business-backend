from django.db import migrations, models


def copy_existing_room_type_counts(apps, schema_editor):
    RoomType = apps.get_model("booking", "RoomType")
    for room_type in RoomType.objects.all().only("id", "default_inventory").iterator():
        room_type.room_type_count_inventory = room_type.default_inventory
        room_type.save(update_fields=["room_type_count_inventory"])


class Migration(migrations.Migration):
    dependencies = [
        ("booking", "0063_hotel_inventory_mode"),
    ]

    operations = [
        migrations.AddField(
            model_name="roomtype",
            name="room_type_count_inventory",
            field=models.PositiveSmallIntegerField(default=0),
        ),
        migrations.RunPython(copy_existing_room_type_counts, migrations.RunPython.noop),
    ]
