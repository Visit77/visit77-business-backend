from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("booking", "0068_ota_follow_up_and_remittance"),
    ]

    operations = [
        migrations.AddField(
            model_name="booking",
            name="is_no_show",
            field=models.BooleanField(db_index=True, default=False),
        ),
    ]
