from django.db import migrations, models


def rename_locked_to_blocked(apps, schema_editor):
    PhysicalRoom = apps.get_model("booking", "PhysicalRoom")
    PhysicalRoom.objects.filter(status="locked").update(status="blocked")


def rename_blocked_to_locked(apps, schema_editor):
    PhysicalRoom = apps.get_model("booking", "PhysicalRoom")
    PhysicalRoom.objects.filter(status="blocked").update(status="locked")


class Migration(migrations.Migration):

    dependencies = [
        ("booking", "0025_physical_room_locked_status"),
    ]

    operations = [
        migrations.RunPython(rename_locked_to_blocked, rename_blocked_to_locked),
        migrations.AlterField(
            model_name="physicalroom",
            name="status",
            field=models.CharField(
                choices=[
                    ("vacant", "Vacant"),
                    ("occupied", "Occupied"),
                    ("cleaning", "Cleaning"),
                    ("out_of_service", "Out of service"),
                    ("blocked", "Blocked"),
                ],
                default="vacant",
                max_length=32,
            ),
        ),
    ]
