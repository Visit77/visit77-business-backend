from django.db import migrations, models
import django.db.models.deletion


def reset_legacy_blocked_status(apps, schema_editor):
    PhysicalRoom = apps.get_model("booking", "PhysicalRoom")
    PhysicalRoom.objects.filter(status="blocked").update(status="vacant")


class Migration(migrations.Migration):

    dependencies = [
        ("booking", "0027_physical_room_scheduled_block"),
    ]

    operations = [
        migrations.CreateModel(
            name="PhysicalRoomBlock",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("start_date", models.DateField()),
                ("end_date", models.DateField()),
                ("note", models.TextField(blank=True)),
                ("is_active", models.BooleanField(default=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("physical_room", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="blocks", to="booking.physicalroom")),
            ],
            options={
                "ordering": ["start_date", "end_date", "id"],
                "indexes": [models.Index(fields=["physical_room", "start_date", "end_date"], name="booking_room_block_dates_idx")],
            },
        ),
        migrations.RunPython(reset_legacy_blocked_status, migrations.RunPython.noop),
        migrations.RemoveField(model_name="physicalroom", name="block_after_checkout"),
        migrations.RemoveField(model_name="physicalroom", name="blocked_from"),
        migrations.AlterField(
            model_name="physicalroom",
            name="status",
            field=models.CharField(
                choices=[
                    ("vacant", "Vacant"),
                    ("occupied", "Occupied"),
                    ("cleaning", "Cleaning"),
                    ("out_of_service", "Out of service"),
                ],
                default="vacant",
                max_length=32,
            ),
        ),
    ]
