from decimal import Decimal, ROUND_HALF_UP

from rest_framework import serializers

from booking.models import OTAInvoiceCharge, PMSInvoiceCharge


class ChargeRuleSerializer(serializers.Serializer):
    title = serializers.CharField(max_length=100)
    mode = serializers.ChoiceField(choices=["do_not_show", "included", "percentage", "fixed"])
    value = serializers.DecimalField(
        max_digits=12, decimal_places=2, min_value=Decimal("0"), required=False,
    )
    calculation_basis = serializers.ChoiceField(
        choices=[
            "per_booking_per_night",
            "per_guest_per_night",
            "per_room_per_night",
        ],
        required=False,
        default="per_booking_per_night",
    )
    charge_kind = serializers.ChoiceField(
        choices=["tax", "other_tax", "service_charge", "other_charge"],
        required=False,
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
                allowed = (
                    {"tax", "other_tax"}
                    if category == "taxes"
                    else {"service_charge", "other_charge"}
                )
                if rule.get("charge_kind") and rule["charge_kind"] not in allowed:
                    raise serializers.ValidationError({
                        "charge_kind": f"Invalid charge kind in {category}."
                    })
                if not rule.get("charge_kind"):
                    normalized_title = rule["title"].strip().casefold()
                    rule["charge_kind"] = (
                        "tax" if category == "taxes" and normalized_title == "tax"
                        else "other_tax" if category == "taxes"
                        else "service_charge" if normalized_title == "service charge"
                        else "other_charge"
                    )
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
            "calculation_basis": charge.calculation_basis,
            "charge_kind": charge.charge_kind,
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
            default_kind = (
                "tax" if charge_type == "tax" and normalized == "tax"
                else "other_tax" if charge_type == "tax"
                else "service_charge" if normalized == "service charge"
                else "other_charge"
            )
            records.append(model(
                hotel=hotel,
                charge_type=charge_type,
                title=title,
                mode=rule["mode"],
                value=rule.get("value") or Decimal("0"),
                calculation_basis=rule.get("calculation_basis") or "per_booking_per_night",
                charge_kind=rule.get("charge_kind") or default_kind,
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


def calculate_invoice_charges(
    config, base_subtotal, *, nights=1, guest_count=1, room_count=1,
):
    """Freeze amounts at booking time; percentages use pre-charge subtotal."""
    base_subtotal = Decimal(str(base_subtotal))
    nights = max(int(nights or 0), 0)
    guest_count = max(int(guest_count or 0), 0)
    room_count = max(int(room_count or 0), 0)
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
            calculation_basis = rule.get("calculation_basis") or "per_booking_per_night"
            default_kind = (
                "tax" if category == "taxes" and normalized == "tax"
                else "other_tax" if category == "taxes"
                else "service_charge" if normalized == "service charge"
                else "other_charge"
            )
            charge_kind = rule.get("charge_kind") or default_kind
            fixed_multiplier = {
                "per_booking_per_night": nights,
                "per_guest_per_night": guest_count * nights,
                "per_room_per_night": room_count * nights,
            }.get(calculation_basis, nights)
            amount = (
                Decimal("0") if mode in {"do_not_show", "included"} else
                (base_subtotal * value / 100).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
                if mode == "percentage" else value * fixed_multiplier
            )
            result[category].append({
                "title": title, "mode": mode,
                "value": str(value), "amount": str(amount),
                "calculation_basis": calculation_basis,
                "charge_kind": charge_kind,
                "multiplier": fixed_multiplier if mode == "fixed" else 1,
            })
    return result


def charge_totals(snapshot):
    return (
        sum((Decimal(item["amount"]) for item in snapshot.get("service_charges", [])), Decimal("0")),
        sum((Decimal(item["amount"]) for item in snapshot.get("taxes", [])), Decimal("0")),
    )


def booking_charge_dimensions(booking):
    """Use booking-create occupancy snapshots, never mutable Guest records."""
    rooms = list(booking.rooms.all())
    return {
        "nights": booking.nights,
        "guest_count": sum(room.adults + room.children for room in rooms),
        "room_count": sum(room.quantity for room in rooms),
    }
