from django.db import migrations, models
from django.db.models import Count


def merge_duplicate_core_physical_rooms(apps, schema_editor):
    PhysicalRoom = apps.get_model("booking", "PhysicalRoom")
    RoomAssignment = apps.get_model("booking", "RoomAssignment")
    PhysicalRoomBlock = apps.get_model("booking", "PhysicalRoomBlock")
    PhysicalRoomActionHistory = apps.get_model("booking", "PhysicalRoomActionHistory")

    duplicate_groups = (
        PhysicalRoom.objects.exclude(core_physical_room_id__isnull=True)
        .values("hotel_id", "core_physical_room_id")
        .annotate(total=Count("id"))
        .filter(total__gt=1)
    )
    status_priority = {
        "vacant": 0,
        "cleaning": 1,
        "out_of_service": 2,
        "occupied": 3,
    }
    for group in duplicate_groups.iterator():
        rooms = list(PhysicalRoom.objects.filter(
            hotel_id=group["hotel_id"],
            core_physical_room_id=group["core_physical_room_id"],
        ).order_by("-id"))
        canonical = rooms[0]
        duplicates = rooms[1:]
        duplicate_ids = [room.id for room in duplicates]

        RoomAssignment.objects.filter(physical_room_id__in=duplicate_ids).update(
            physical_room_id=canonical.id,
        )
        PhysicalRoomBlock.objects.filter(physical_room_id__in=duplicate_ids).update(
            physical_room_id=canonical.id,
        )
        PhysicalRoomActionHistory.objects.filter(physical_room_id__in=duplicate_ids).update(
            physical_room_id=canonical.id,
        )

        status_room = max(rooms, key=lambda room: status_priority.get(room.status, 0))
        canonical.status = status_room.status
        if status_room.note:
            canonical.note = status_room.note
        canonical.save(update_fields=["status", "note"])
        PhysicalRoom.objects.filter(id__in=duplicate_ids).delete()


class Migration(migrations.Migration):
    dependencies = [
        ("booking", "0030_physical_room_action_history"),
    ]

    operations = [
        migrations.RunPython(
            merge_duplicate_core_physical_rooms,
            migrations.RunPython.noop,
        ),
        migrations.AddConstraint(
            model_name="physicalroom",
            constraint=models.UniqueConstraint(
                fields=("hotel", "core_physical_room_id"),
                condition=models.Q(core_physical_room_id__isnull=False),
                name="uniq_core_physical_room_per_hotel",
            ),
        ),
    ]
