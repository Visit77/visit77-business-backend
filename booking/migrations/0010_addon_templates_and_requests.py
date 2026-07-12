import django.db.models.deletion
from django.db import migrations, models
from django.utils import timezone


TEMPLATES = [
    ("airport_pickup", "Airport Pickup", ["per_booking", "per_unit"], [
        ("airport_name", "Airport Name", "text", True),
        ("flight_number", "Flight Number", "text", True),
        ("arrival_date", "Arrival Date", "date", True),
        ("arrival_time", "Arrival Time", "time", True),
    ]),
    ("car_rental_with_driver", "Car Rental (With Driver)", ["per_booking", "per_night", "per_unit"], [
        ("start_date", "Start Date", "date", True),
        ("end_date", "End Date", "date", True),
        ("pickup_location", "Pickup Location", "text", True),
        ("note", "Note", "textarea", False),
    ]),
    ("car_rental_self_drive", "Car Rental (Self Drive)", ["per_booking", "per_night", "per_unit"], [
        ("start_date", "Start Date", "date", True),
        ("end_date", "End Date", "date", True),
        ("driving_license_number", "Driving License Number", "text", True),
    ]),
    ("motorcycle_rental", "Motorcycle Rental", ["per_night", "per_unit"], [
        ("start_date", "Start Date", "date", True),
        ("end_date", "End Date", "date", True),
        ("driving_license_number", "Driving License Number", "text", True),
    ]),
    ("bicycle_rental", "Bicycle Rental", ["per_night", "per_unit"], [
        ("start_date", "Start Date", "date", True),
        ("end_date", "End Date", "date", True),
    ]),
    ("early_check_in", "Early Check-in", ["per_booking", "per_unit"], [
        ("requested_time", "Requested Check-in Time", "time", True),
    ]),
    ("late_check_out", "Late Check-out", ["per_booking", "per_unit"], [
        ("requested_time", "Requested Check-out Time", "time", True),
    ]),
    ("custom", "Custom Add-on", ["per_booking", "per_night", "per_unit"], []),
]


def seed_templates(apps, schema_editor):
    AddOn = apps.get_model("booking", "AddOn")
    AddOnTemplate = apps.get_model("booking", "AddOnTemplate")
    templates = {}
    for code, name, pricing_units, fields in TEMPLATES:
        template = AddOnTemplate.objects.create(
            code=code,
            version=1,
            name=name,
            allowed_pricing_units=pricing_units,
            configuration_schema={
                "version": 1,
                "fields": [
                    {"key": key, "label": label, "type": field_type, "required": required}
                    for key, label, field_type, required in fields
                ],
            },
            status="archived" if code == "custom" else "published",
            published_at=None if code == "custom" else timezone.now(),
        )
        templates[code] = template
    for add_on in AddOn.objects.all().iterator():
        add_on.template = templates.get(add_on.service_type) or templates["custom"]
        add_on.save(update_fields=["template"])


class Migration(migrations.Migration):
    dependencies = [("booking", "0009_addon_service_type_and_schema")]

    operations = [
        migrations.CreateModel(
            name="AddOnTemplate",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("code", models.SlugField(max_length=80)),
                ("version", models.PositiveIntegerField(default=1)),
                ("name", models.CharField(max_length=255)),
                ("description", models.TextField(blank=True)),
                ("allowed_pricing_units", models.JSONField(default=list)),
                ("configuration_schema", models.JSONField(default=dict)),
                ("status", models.CharField(choices=[("draft", "Draft"), ("published", "Published"), ("archived", "Archived")], default="draft", max_length=16)),
                ("created_by_core_user_id", models.PositiveBigIntegerField(blank=True, null=True)),
                ("published_at", models.DateTimeField(blank=True, null=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
            ],
            options={"ordering": ["name", "-version", "id"]},
        ),
        migrations.CreateModel(
            name="AddOnTemplateRequest",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("requested_name", models.CharField(max_length=255)),
                ("description", models.TextField(blank=True)),
                ("suggested_pricing_units", models.JSONField(blank=True, default=list)),
                ("suggested_schema", models.JSONField(blank=True, default=dict)),
                ("status", models.CharField(choices=[("pending", "Pending"), ("reviewing", "Reviewing"), ("approved", "Approved"), ("rejected", "Rejected")], default="pending", max_length=16)),
                ("requested_by_core_user_id", models.PositiveBigIntegerField(blank=True, null=True)),
                ("reviewed_by_core_user_id", models.PositiveBigIntegerField(blank=True, null=True)),
                ("admin_note", models.TextField(blank=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("reviewed_at", models.DateTimeField(blank=True, null=True)),
                ("approved_template", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="approved_requests", to="booking.addontemplate")),
                ("hotel", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="add_on_template_requests", to="booking.hotel")),
            ],
            options={"ordering": ["-created_at", "-id"]},
        ),
        migrations.AddField(
            model_name="addon",
            name="template",
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name="add_ons", to="booking.addontemplate"),
        ),
        migrations.AlterField(
            model_name="addon",
            name="service_type",
            field=models.CharField(default="custom", max_length=80),
        ),
        migrations.AddConstraint(
            model_name="addontemplate",
            constraint=models.UniqueConstraint(fields=("code", "version"), name="uniq_add_on_template_version"),
        ),
        migrations.AddConstraint(
            model_name="addontemplate",
            constraint=models.UniqueConstraint(condition=models.Q(("status", "published")), fields=("code",), name="uniq_published_add_on_template_code"),
        ),
        migrations.RunPython(seed_templates, migrations.RunPython.noop),
    ]
