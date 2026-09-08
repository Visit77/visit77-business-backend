from django.db import migrations


DEFAULT_CANCELLATION_POLICY = {
    "type": "non_refundable",
    "name": "Non-Refundable",
}


def backfill_hotel_cancellation_policy(apps, schema_editor):
    Hotel = apps.get_model("booking", "Hotel")
    hotels_to_update = []
    for hotel in Hotel.objects.all().iterator(chunk_size=500):
        snapshot = dict(hotel.core_snapshot or {})
        if snapshot.get("hotel_cancellation_policy"):
            continue
        snapshot["hotel_cancellation_policy"] = DEFAULT_CANCELLATION_POLICY
        hotel.core_snapshot = snapshot
        hotels_to_update.append(hotel)
        if len(hotels_to_update) >= 500:
            Hotel.objects.bulk_update(hotels_to_update, ["core_snapshot"], batch_size=500)
            hotels_to_update = []
    if hotels_to_update:
        Hotel.objects.bulk_update(hotels_to_update, ["core_snapshot"], batch_size=500)


class Migration(migrations.Migration):
    dependencies = [("booking", "0045_hotel_timezone_and_default_times")]

    operations = [
        migrations.RunPython(backfill_hotel_cancellation_policy, migrations.RunPython.noop),
    ]
