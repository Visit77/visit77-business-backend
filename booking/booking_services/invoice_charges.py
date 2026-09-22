from decimal import Decimal, ROUND_HALF_UP

from rest_framework import serializers


class ChargeRuleSerializer(serializers.Serializer):
    title = serializers.CharField(max_length=100)
    mode = serializers.ChoiceField(choices=["included", "percentage", "fixed"])
    value = serializers.DecimalField(
        max_digits=12, decimal_places=2, min_value=Decimal("0"), required=False,
    )

    def validate(self, attrs):
        if attrs["mode"] != "included" and "value" not in attrs:
            raise serializers.ValidationError({"value": "Required for percentage or fixed charges."})
        if attrs["mode"] == "percentage" and attrs["value"] > 100:
            raise serializers.ValidationError({"value": "Percentage cannot exceed 100."})
        return attrs


class OTAInvoiceChargesSerializer(serializers.Serializer):
    taxes = ChargeRuleSerializer(many=True, required=False, default=list)
    other_charges = ChargeRuleSerializer(many=True, required=False, default=list)

    def validate_other_charges(self, value):
        if any(item["mode"] == "included" for item in value):
            raise serializers.ValidationError("Other charges must use percentage or fixed mode.")
        return value


def calculate_ota_invoice_charges(config, base_subtotal):
    """Freeze amounts at booking time; percentages use pre-charge subtotal."""
    base_subtotal = Decimal(str(base_subtotal))
    result = {"taxes": [], "other_charges": []}
    for category in ("other_charges", "taxes"):
        for rule in (config or {}).get(category, []):
            mode = rule["mode"]
            value = Decimal(str(rule.get("value") or 0))
            amount = (
                Decimal("0") if mode == "included" else
                (base_subtotal * value / 100).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
                if mode == "percentage" else value
            )
            result[category].append({
                "title": rule["title"], "mode": mode,
                "value": str(value), "amount": str(amount),
            })
    return result


def charge_totals(snapshot):
    return (
        sum((Decimal(item["amount"]) for item in snapshot.get("other_charges", [])), Decimal("0")),
        sum((Decimal(item["amount"]) for item in snapshot.get("taxes", [])), Decimal("0")),
    )
