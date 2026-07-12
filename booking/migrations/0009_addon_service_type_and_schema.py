from django.db import migrations, models


LEGACY_TYPE_MAP = {
    "string": "text",
    "text": "text",
    "number": "number",
    "integer": "integer",
    "date": "date",
    "time": "time",
    "datetime": "datetime",
    "boolean": "boolean",
}


def normalize_existing_add_ons(apps, schema_editor):
    AddOn = apps.get_model("booking", "AddOn")
    for add_on in AddOn.objects.all().iterator():
        schema = add_on.configuration_schema or {}
        if "fields" not in schema:
            fields = []
            for key, legacy_type in schema.items():
                fields.append({
                    "key": key,
                    "label": key.replace("_", " ").title(),
                    "type": LEGACY_TYPE_MAP.get(legacy_type, "text"),
                    "required": False,
                })
            add_on.configuration_schema = {"version": 1, "fields": fields}
        code = add_on.code.lower()
        if "airport" in code and "pickup" in code:
            add_on.service_type = "airport_pickup"
        elif "early" in code and "check" in code:
            add_on.service_type = "early_check_in"
        elif "late" in code and "check" in code:
            add_on.service_type = "late_check_out"
        add_on.save(update_fields=["service_type", "configuration_schema"])


class Migration(migrations.Migration):
    dependencies = [("booking", "0008_rate_plan_source_and_custom_plans")]

    operations = [
        migrations.AddField(
            model_name="addon",
            name="service_type",
            field=models.CharField(default="custom", max_length=50),
        ),
        migrations.RunPython(normalize_existing_add_ons, migrations.RunPython.noop),
    ]
