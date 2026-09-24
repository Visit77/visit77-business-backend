from decimal import Decimal, ROUND_HALF_UP

from rest_framework import serializers

from booking.models import OTAInvoiceCharge, PMSInvoiceCharge


class ChargeRuleSerializer(serializers.Serializer):
    title = serializers.CharField(max_length=100)
    mode = serializers.ChoiceField(choices=["do_not_show", "included", "percentage", "fixed"])
    value = serializers.DecimalField(
        max_digits=12, decimal_places=2, min_value=Decimal("0"), required=False,
    )

    def validate(self, attrs):
        if attrs["mode"] not in {"do_not_show", "included"} and "value" not in attrs:
            raise serializers.ValidationError({"value": "Required for percentage or fixed charges."})
        if attrs["mode"] == "percentage" and attrs["value"] > 100:
            raise serializers.ValidationError({"value": "Percentage cannot exceed 100."})
        return attrs

    def validate_title(self, value):
        title = value.strip()
        if not title:
            raise serializers.ValidationError("Name cannot be blank.")
        return title


class InvoiceChargesSerializer(serializers.Serializer):
    taxes = ChargeRuleSerializer(many=True, required=False, default=list)
    service_charges = ChargeRuleSerializer(many=True, required=False, default=list)

    def validate_service_charges(self, value):
        if any(item["mode"] == "do_not_show" for item in value):
            raise serializers.ValidationError("Service charges cannot use do_not_show mode.")
        return value

    def validate(self, attrs):
        seen = set()
        for category in ("taxes", "service_charges"):
            for rule in attrs.get(category, []):
                normalized = rule["title"].casefold()
                if normalized in seen:
                    raise serializers.ValidationError({
                        "name": f'Charge name "{rule["title"]}" must be unique.',
                    })
                seen.add(normalized)
        return attrs


CHARGE_MODEL_CONFIG = {
    OTAInvoiceCharge: "ota_invoice_charges",
    PMSInvoiceCharge: "pms_invoice_charges",
}


def config_from_charge_records(queryset):
    config = {"taxes": [], "service_charges": []}
    for charge in queryset.order_by("charge_type", "sort_order", "id"):
        category = "taxes" if charge.charge_type == charge.ChargeType.TAX else "service_charges"
        config[category].append({
            "title": charge.title,
            "mode": charge.mode,
            "value": str(charge.value),
        })
    return config


def sync_hotel_charge_config(hotel, model):
    field_name = CHARGE_MODEL_CONFIG[model]
    config = config_from_charge_records(model.objects.filter(hotel=hotel))
    setattr(hotel, field_name, config)
    hotel.save(update_fields=[field_name])
    return config


def replace_charge_records(hotel, model, config):
    model.objects.filter(hotel=hotel).delete()
    records = []
    seen = set()
    for charge_type, category in (("tax", "taxes"), ("service", "service_charges")):
        for index, rule in enumerate(config.get(category, [])):
            title = str(rule["title"]).strip()
            normalized = title.casefold()
            if not title or normalized in seen:
                continue
            seen.add(normalized)
            records.append(model(
                hotel=hotel,
                charge_type=charge_type,
                title=title,
                mode=rule["mode"],
                value=rule.get("value") or Decimal("0"),
                sort_order=index,
            ))
    model.objects.bulk_create(records)
    return config


def ensure_charge_records(hotel, model):
    queryset = model.objects.filter(hotel=hotel)
    if queryset.exists():
        return queryset
    field_name = CHARGE_MODEL_CONFIG[model]
    replace_charge_records(hotel, model, getattr(hotel, field_name) or {})
    return model.objects.filter(hotel=hotel)


def calculate_invoice_charges(config, base_subtotal):
    """Freeze amounts at booking time; percentages use pre-charge subtotal."""
    base_subtotal = Decimal(str(base_subtotal))
    result = {"taxes": [], "service_charges": []}
    seen_names = set()
    for category in ("service_charges", "taxes"):
        for rule in (config or {}).get(category, []):
            title = str(rule["title"]).strip()
            normalized = title.casefold()
            if not title or normalized in seen_names:
                continue
            seen_names.add(normalized)
            mode = rule["mode"]
            value = Decimal(str(rule.get("value") or 0))
            amount = (
                Decimal("0") if mode in {"do_not_show", "included"} else
                (base_subtotal * value / 100).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
                if mode == "percentage" else value
            )
            result[category].append({
                "title": title, "mode": mode,
                "value": str(value), "amount": str(amount),
            })
    return result


def charge_totals(snapshot):
    return (
        sum((Decimal(item["amount"]) for item in snapshot.get("service_charges", [])), Decimal("0")),
        sum((Decimal(item["amount"]) for item in snapshot.get("taxes", [])), Decimal("0")),
    )
