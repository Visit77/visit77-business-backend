from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("booking", "0056_ota_document_year_sequence"),
    ]

    operations = [
        migrations.AddField(
            model_name="otainvoicecharge",
            name="calculation_basis",
            field=models.CharField(
                choices=[
                    ("per_booking_per_night", "Per booking / night"),
                    ("per_guest_per_night", "Per guest / night"),
                    ("per_room_per_night", "Per room / night"),
                ],
                default="per_booking_per_night",
                max_length=32,
            ),
        ),
        migrations.AddField(
            model_name="pmsinvoicecharge",
            name="calculation_basis",
            field=models.CharField(
                choices=[
                    ("per_booking_per_night", "Per booking / night"),
                    ("per_guest_per_night", "Per guest / night"),
                    ("per_room_per_night", "Per room / night"),
                ],
                default="per_booking_per_night",
                max_length=32,
            ),
        ),
    ]
