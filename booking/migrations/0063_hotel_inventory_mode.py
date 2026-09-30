from django.db import migrations, models


def set_existing_inventory_modes(apps, schema_editor):
    Hotel = apps.get_model("booking", "Hotel")
    Hotel.objects.filter(package="ota").update(inventory_mode="room_type_count")
    Hotel.objects.exclude(package="ota").update(inventory_mode="physical_room")


class Migration(migrations.Migration):
    dependencies = [
        ("booking", "0062_ota_booking_notification_soft_delete"),
    ]

    operations = [
        migrations.AddField(
            model_name="hotel",
            name="inventory_mode",
            field=models.CharField(
                choices=[
                    ("room_type_count", "Room type count"),
                    ("physical_room", "Physical room"),
                ],
                default="room_type_count",
                max_length=24,
            ),
        ),
        migrations.RunPython(set_existing_inventory_modes, migrations.RunPython.noop),
    ]
