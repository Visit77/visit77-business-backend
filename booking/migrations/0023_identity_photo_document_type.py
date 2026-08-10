from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("booking", "0022_booking_check_in_verification_note_and_more"),
    ]

    operations = [
        migrations.AlterField(
            model_name="guestidentitydocument",
            name="document_type",
            field=models.CharField(
                choices=[("identity_photo", "Identity Photo")],
                max_length=24,
            ),
        ),
    ]
