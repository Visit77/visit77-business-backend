import secrets
import string

from django.db import migrations, models
import django.db.models.deletion


def populate_hotel_codes(apps, schema_editor):
    Hotel = apps.get_model("booking", "Hotel")
    used = set(Hotel.objects.exclude(document_code__isnull=True).values_list("document_code", flat=True))
    for hotel in Hotel.objects.order_by("pk").iterator():
        while True:
            code = "".join(secrets.choice(string.ascii_uppercase) for _ in range(4))
            if code not in used:
                break
        used.add(code)
        Hotel.objects.filter(pk=hotel.pk).update(document_code=code)


class Migration(migrations.Migration):
    dependencies = [("booking", "0047_ota_invoice_charges")]

    operations = [
        migrations.AddField(
            model_name="hotel",
            name="document_code",
            field=models.CharField(max_length=4, null=True, unique=True),
        ),
        migrations.RunPython(populate_hotel_codes, migrations.RunPython.noop),
        migrations.AlterField(
            model_name="hotel",
            name="document_code",
            field=models.CharField(editable=False, max_length=4, unique=True),
        ),
        migrations.AlterField(
            model_name="booking",
            name="booking_code",
            field=models.CharField(editable=False, max_length=20, unique=True),
        ),
        migrations.CreateModel(
            name="OTABookingYearSequence",
            fields=[
                ("year", models.PositiveSmallIntegerField(primary_key=True, serialize=False)),
                ("last_value", models.PositiveIntegerField(default=0)),
            ],
        ),
        migrations.CreateModel(
            name="HotelDocumentSequence",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("year", models.PositiveSmallIntegerField()),
                ("kind", models.CharField(choices=[("INV", "Invoice"), ("REC", "Receipt")], max_length=3)),
                ("last_value", models.PositiveIntegerField(default=0)),
                ("hotel", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, to="booking.hotel")),
            ],
        ),
        migrations.AddConstraint(
            model_name="hoteldocumentsequence",
            constraint=models.UniqueConstraint(fields=("hotel", "year", "kind"), name="uniq_hotel_document_year_kind"),
        ),
    ]
