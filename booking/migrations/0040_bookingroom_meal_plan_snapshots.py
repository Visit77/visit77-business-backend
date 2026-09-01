from django.db import migrations, models


def copy_existing_snapshot(apps, schema_editor):
    BookingRoom = apps.get_model("booking", "BookingRoom")
    batch = []
    for room in BookingRoom.objects.exclude(meal_plan_snapshot={}).iterator(chunk_size=1000):
        room.meal_plan_snapshots = [room.meal_plan_snapshot]
        batch.append(room)
        if len(batch) == 1000:
            BookingRoom.objects.bulk_update(batch, ["meal_plan_snapshots"])
            batch = []
    if batch:
        BookingRoom.objects.bulk_update(batch, ["meal_plan_snapshots"])


class Migration(migrations.Migration):

    dependencies = [("booking", "0039_booking_code")]

    operations = [
        migrations.AddField(
            model_name="bookingroom",
            name="meal_plan_snapshots",
            field=models.JSONField(blank=True, default=list),
        ),
        migrations.RunPython(copy_existing_snapshot, migrations.RunPython.noop),
    ]
