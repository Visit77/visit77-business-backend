from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("booking", "0026_rename_locked_room_status_to_blocked"),
    ]

    operations = [
        migrations.AddField(
            model_name="physicalroom",
            name="block_after_checkout",
            field=models.BooleanField(default=False),
        ),
        migrations.AddField(
            model_name="physicalroom",
            name="blocked_from",
            field=models.DateField(blank=True, null=True),
        ),
    ]
