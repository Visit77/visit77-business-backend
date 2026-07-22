from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("booking", "0013_bookingroom_option_total_and_more"),
    ]

    operations = [
        migrations.AddField(
            model_name="physicalroom",
            name="core_building_id",
            field=models.PositiveBigIntegerField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="physicalroom",
            name="core_floor_id",
            field=models.PositiveBigIntegerField(blank=True, null=True),
        ),
        migrations.AddIndex(
            model_name="physicalroom",
            index=models.Index(fields=["hotel", "core_building_id"], name="booking_phy_hotel_i_2e5f4d_idx"),
        ),
        migrations.AddIndex(
            model_name="physicalroom",
            index=models.Index(fields=["hotel", "core_floor_id"], name="booking_phy_hotel_i_c2a08a_idx"),
        ),
    ]
