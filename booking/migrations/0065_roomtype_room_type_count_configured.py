from django.db import migrations, models
from django.db.models import F, Q


def mark_existing_count_inventory(apps, schema_editor):
    RoomType = apps.get_model("booking", "RoomType")
    RoomType.objects.filter(
        Q(hotel__inventory_mode="room_type_count")
        | ~Q(room_type_count_inventory=F("default_inventory")),
    ).update(room_type_count_configured=True)


class Migration(migrations.Migration):
    dependencies = [
        ("booking", "0064_roomtype_room_type_count_inventory"),
    ]

    operations = [
        migrations.AddField(
            model_name="roomtype",
            name="room_type_count_configured",
            field=models.BooleanField(default=False),
        ),
        migrations.RunPython(
            mark_existing_count_inventory,
            migrations.RunPython.noop,
        ),
    ]
