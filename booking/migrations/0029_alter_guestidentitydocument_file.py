import booking.storage
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("booking", "0028_physical_room_date_range_blocks"),
    ]

    operations = [
        migrations.AlterField(
            model_name="guestidentitydocument",
            name="file",
            field=models.FileField(
                storage=booking.storage.get_private_document_storage,
                upload_to="booking/guest-identities/%Y/%m/",
            ),
        ),
    ]
