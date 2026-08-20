from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("booking", "0034_physicalroom_ota_sale_open")]

    operations = [
        migrations.AlterField(
            model_name="physicalroomactionhistory",
            name="action",
            field=models.CharField(
                choices=[
                    ("reserved", "Reserved"),
                    ("room_assigned", "Room assigned"),
                    ("room_unassigned", "Room unassigned"),
                    ("room_changed", "Room changed"),
                    ("checked_in", "Checked in"),
                    ("checked_out", "Checked out"),
                    ("cleaning_started", "Cleaning started"),
                    ("cleaning_completed", "Cleaning completed"),
                    ("status_changed", "Status changed"),
                    ("out_of_service_started", "Out of service started"),
                    ("out_of_service_ended", "Out of service ended"),
                    ("block_created", "Block created"),
                    ("block_updated", "Block updated"),
                    ("unblocked", "Unblocked"),
                    ("ota_sale_opened", "OTA sale opened"),
                    ("ota_sale_closed", "OTA sale closed"),
                ],
                max_length=40,
            ),
        ),
    ]
