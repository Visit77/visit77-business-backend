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
        if self.instance and (
            self.instance.ended_at is not None
            or (self.instance.end_date is not None and self.instance.end_date < timezone.localdate())
        ):
            raise serializers.ValidationError("Ended or expired discounts cannot be edited. Create a new discount.")
        if not room_type.room_price_setup:
            raise serializers.ValidationError("Set the normal room price before adding a discount.")
        for field in PRICE_FIELDS:
            attrs.setdefault(field, getattr(self.instance, field, None))
        if not any(attrs.get(field) is not None for field in PRICE_FIELDS):
            raise serializers.ValidationError("Set at least one Local/Foreigner MMK or USD discount price.")
        for field in PRICE_FIELDS:
            value, normal = attrs.get(field), getattr(room_type, field)
            if value is None:
                continue
            if normal is None or value >= normal:
                raise serializers.ValidationError({field: "Set a discount price lower than its configured normal price."})
        mode = attrs.get("timing_mode", getattr(self.instance, "timing_mode", None))
        if mode == RoomPriceDiscount.TimingMode.MANUAL:
            start = (
                self.instance.start_date
                if self.instance and self.instance.timing_mode == mode
                else timezone.localdate()
            )
            attrs.update(start_date=start, end_date=None)
        else:
            start = attrs.get("start_date", getattr(self.instance, "start_date", None))
            end = attrs.get("end_date", getattr(self.instance, "end_date", None))
            if not start or not end:
                raise serializers.ValidationError("Scheduled discounts require start_date and end_date.")
            if end < start:
                raise serializers.ValidationError({"end_date": "Must be on or after start_date."})
        return attrs

    def _set_percentages(self, validated_data):
        room_type = self.context["room_type"]
        for field in PRICE_FIELDS:
            value = validated_data.get(field)
            percentage_field = field.replace("_price", "_percentage")
            if value is None:
                validated_data[percentage_field] = None
            else:
                normal = getattr(room_type, field)
                validated_data[percentage_field] = (
                    (normal - value) * Decimal("100") / normal
                ).quantize(Decimal("0.01"))

    def create(self, validated_data):
        self._set_percentages(validated_data)
        return RoomPriceDiscount.objects.create(room_type=self.context["room_type"], **validated_data)

    def update(self, instance, validated_data):
        self._set_percentages(validated_data)
        return super().update(instance, validated_data)
