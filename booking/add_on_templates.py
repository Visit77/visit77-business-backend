from decimal import Decimal, InvalidOperation
import re

from django.utils.dateparse import parse_date, parse_datetime, parse_time


FIELD_TYPES = {"text", "textarea", "number", "integer", "date", "time", "datetime", "boolean", "select"}
FIELD_KEY_PATTERN = re.compile(r"^[a-z][a-z0-9_]{0,63}$")


def normalize_configuration_schema(schema):
    if schema in (None, {}):
        return {"version": 1, "fields": []}
    if not isinstance(schema, dict):
        raise ValueError("configuration_schema must be an object.")
    fields = schema.get("fields")
    if not isinstance(fields, list):
        raise ValueError("configuration_schema.fields must be a list.")

    normalized_fields = []
    seen_keys = set()
    for index, field in enumerate(fields):
        if not isinstance(field, dict):
            raise ValueError(f"Field {index + 1} must be an object.")
        key = field.get("key", "")
        label = field.get("label", "")
        field_type = field.get("type", "text")
        if not isinstance(key, str) or not FIELD_KEY_PATTERN.fullmatch(key):
            raise ValueError(f"Field {index + 1} has an invalid key.")
        if key in seen_keys:
            raise ValueError(f"Field key '{key}' is duplicated.")
        if not isinstance(label, str) or not label.strip():
            raise ValueError(f"Field '{key}' requires a label.")
        if field_type not in FIELD_TYPES:
            raise ValueError(f"Field '{key}' has unsupported type '{field_type}'.")
        normalized = {
            "key": key,
            "label": label.strip(),
            "type": field_type,
            "required": bool(field.get("required", False)),
        }
        if field_type == "select":
            options = field.get("options")
            if not isinstance(options, list) or not options or not all(isinstance(option, str) for option in options):
                raise ValueError(f"Select field '{key}' requires a non-empty string options list.")
            normalized["options"] = options
        if field.get("placeholder"):
            normalized["placeholder"] = str(field["placeholder"])
        normalized_fields.append(normalized)
        seen_keys.add(key)
    return {"version": 1, "fields": normalized_fields}


def validate_configuration_values(schema, configuration):
    if not isinstance(configuration, dict):
        return {"configuration": "Must be an object."}
    fields = normalize_configuration_schema(schema)["fields"]
    declared_keys = {field["key"] for field in fields}
    errors = {}
    for key in configuration.keys() - declared_keys:
        errors[key] = "This field is not supported for the selected add-on."
    for field in fields:
        key = field["key"]
        value = configuration.get(key)
        if field["required"] and (value is None or value == ""):
            errors[key] = "This field is required."
            continue
        if value is None or value == "":
            continue
        field_type = field["type"]
        valid = True
        if field_type in {"text", "textarea"}:
            valid = isinstance(value, str)
        elif field_type == "integer":
            valid = isinstance(value, int) and not isinstance(value, bool)
        elif field_type == "number":
            try:
                Decimal(str(value))
            except (InvalidOperation, TypeError, ValueError):
                valid = False
        elif field_type == "boolean":
            valid = isinstance(value, bool)
        elif field_type == "date":
            valid = isinstance(value, str) and parse_date(value) is not None
        elif field_type == "time":
            valid = isinstance(value, str) and parse_time(value) is not None
        elif field_type == "datetime":
            valid = isinstance(value, str) and parse_datetime(value) is not None
        elif field_type == "select":
            valid = value in field["options"]
        if not valid:
            errors[key] = f"Must be a valid {field_type} value."
    return errors
