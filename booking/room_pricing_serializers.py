from decimal import Decimal

from django.utils import timezone
from rest_framework import serializers

from booking.models import RoomPriceDiscount

PRICE_FIELDS = ("local_mmk_price", "local_usd_price", "foreign_mmk_price", "foreign_usd_price")
PERCENTAGE_FIELDS = tuple(field.replace("_price", "_percentage") for field in PRICE_FIELDS)


class RoomPriceSetupSerializer(serializers.Serializer):
    local_mmk_price = serializers.DecimalField(max_digits=14, decimal_places=2, min_value=Decimal("0.01"), required=False, allow_null=True)
    local_usd_price = serializers.DecimalField(max_digits=14, decimal_places=2, min_value=Decimal("0.01"), required=False, allow_null=True)
    foreign_mmk_price = serializers.DecimalField(max_digits=14, decimal_places=2, min_value=Decimal("0.01"), required=False, allow_null=True)
    foreign_usd_price = serializers.DecimalField(max_digits=14, decimal_places=2, min_value=Decimal("0.01"), required=False, allow_null=True)

    def validate(self, attrs):
        room_type = self.context["room_type"]
        if not any(attrs.get(field, getattr(room_type, field)) is not None for field in PRICE_FIELDS):
            raise serializers.ValidationError("Set at least one Local/Foreigner MMK or USD room price.")
        return attrs


class RoomPriceDiscountSerializer(serializers.ModelSerializer):
    start_date = serializers.DateField(required=False)
    status = serializers.SerializerMethodField()

    class Meta:
        model = RoomPriceDiscount
        fields = ["id", "timing_mode", "start_date", "end_date", *PRICE_FIELDS, *PERCENTAGE_FIELDS,
                  "status", "ended_at", "created_at", "updated_at"]
        read_only_fields = [*PERCENTAGE_FIELDS, "ended_at", "created_at", "updated_at"]
        extra_kwargs = {field: {"required": False, "allow_null": True, "min_value": Decimal("0.01")} for field in PRICE_FIELDS}

    def get_status(self, obj):
        if obj.ended_at:
            return "ended"
        today = timezone.localdate()
        if obj.start_date > today:
            return "scheduled"
        if obj.end_date and obj.end_date < today:
            return "expired"
        return "active"

    def validate(self, attrs):
        room_type = self.context["room_type"]
        if not room_type.room_price_setup:
            raise serializers.ValidationError("Set the normal room price before adding a discount.")
        if not any(attrs.get(field) is not None for field in PRICE_FIELDS):
            raise serializers.ValidationError("Set at least one Local/Foreigner MMK or USD discount price.")
        for field in PRICE_FIELDS:
            value, normal = attrs.get(field), getattr(room_type, field)
            if value is None:
                continue
            if normal is None or value >= normal:
                raise serializers.ValidationError({field: "Set a discount price lower than its configured normal price."})
        if attrs["timing_mode"] == RoomPriceDiscount.TimingMode.MANUAL:
            attrs.update(start_date=timezone.localdate(), end_date=None)
        else:
            start, end = attrs.get("start_date"), attrs.get("end_date")
            if not start or not end:
                raise serializers.ValidationError("Scheduled discounts require start_date and end_date.")
            if end < start:
                raise serializers.ValidationError({"end_date": "Must be on or after start_date."})
        return attrs

    def create(self, validated_data):
        room_type = self.context["room_type"]
        for field in PRICE_FIELDS:
            value = validated_data.get(field)
            if value is not None:
                normal = getattr(room_type, field)
                validated_data[field.replace("_price", "_percentage")] = (
                    (normal - value) * Decimal("100") / normal
                ).quantize(Decimal("0.01"))
        return RoomPriceDiscount.objects.create(room_type=room_type, **validated_data)
