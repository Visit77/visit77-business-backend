from decimal import Decimal

from django.utils import timezone
from rest_framework import serializers

from booking.models import RoomPriceDiscount, RoomSeasonalRate

PRICE_FIELDS = ("local_mmk_price", "local_usd_price", "foreign_mmk_price", "foreign_usd_price")
API_PRICE_FIELDS = tuple(field.replace("_mmk_", "_base_") for field in PRICE_FIELDS)
API_PERCENTAGE_FIELDS = tuple(field.replace("_price", "_percentage") for field in API_PRICE_FIELDS)

PERCENTAGE_FIELDS = tuple(field.replace("_price", "_percentage") for field in PRICE_FIELDS)
EXTRA_BED_PRICE_FIELDS = tuple(f"extra_bed_{field}" for field in PRICE_FIELDS)


class ExtraBedPriceSetupSerializer(serializers.Serializer):
    local_base_price = serializers.DecimalField(source='extra_bed_local_mmk_price', max_digits=14, decimal_places=2, min_value=Decimal('0'), required=False, allow_null=True)
    local_usd_price = serializers.DecimalField(source='extra_bed_local_usd_price', max_digits=14, decimal_places=2, min_value=Decimal('0'), required=False, allow_null=True)
    foreign_base_price = serializers.DecimalField(source='extra_bed_foreign_mmk_price', max_digits=14, decimal_places=2, min_value=Decimal('0'), required=False, allow_null=True)
    foreign_usd_price = serializers.DecimalField(source='extra_bed_foreign_usd_price', max_digits=14, decimal_places=2, min_value=Decimal('0'), required=False, allow_null=True)

    def to_internal_value(self, data):
        deprecated = set(data).intersection({
            'extra_bed_local_mmk_price', 'extra_bed_foreign_mmk_price',
            'local_mmk_price', 'foreign_mmk_price',
        })
        if deprecated:
            raise serializers.ValidationError({
                field: f"Use {field.replace('_mmk_', '_base_')} instead."
                for field in deprecated
            })

        if any(f"extra_bed_{field}" in data for field in API_PRICE_FIELDS):
            data = data.copy()
            for field in API_PRICE_FIELDS:
                prefixed = f"extra_bed_{field}"
                if prefixed not in data:
                    continue
                if field in data:
                    raise serializers.ValidationError({prefixed: f"Send either {field} or {prefixed}."})
                data[field] = data[prefixed]
        return super().to_internal_value(data)

    def validate(self, attrs):
        if not self.context['room_type'].room_price_setup:
            raise serializers.ValidationError('Set the normal room price before adding an extra bed rate.')
        return attrs


class BasePriceFieldNamesMixin:
    def to_internal_value(self, data):
        deprecated = set(data).intersection({'local_mmk_price', 'foreign_mmk_price'})
        if deprecated:
            raise serializers.ValidationError({field: f"Use {field.replace('_mmk_', '_base_')} instead." for field in deprecated})
        return super().to_internal_value(data)


class RoomPriceSetupSerializer(BasePriceFieldNamesMixin, serializers.Serializer):
    local_base_price = serializers.DecimalField(source="local_mmk_price", max_digits=14, decimal_places=2, min_value=Decimal("0.01"), required=False, allow_null=True)
    local_usd_price = serializers.DecimalField(max_digits=14, decimal_places=2, min_value=Decimal("0.01"), required=False, allow_null=True)
    foreign_base_price = serializers.DecimalField(source="foreign_mmk_price", max_digits=14, decimal_places=2, min_value=Decimal("0.01"), required=False, allow_null=True)
    foreign_usd_price = serializers.DecimalField(max_digits=14, decimal_places=2, min_value=Decimal("0.01"), required=False, allow_null=True)

    def validate(self, attrs):
        room_type = self.context["room_type"]
        if not any(attrs.get(field, getattr(room_type, field)) is not None for field in PRICE_FIELDS):
            raise serializers.ValidationError("Set at least one Local/Foreigner base currency or USD room price.")
        return attrs


class RoomPriceDiscountSerializer(BasePriceFieldNamesMixin, serializers.ModelSerializer):
    local_base_price = serializers.DecimalField(source='local_mmk_price', max_digits=14, decimal_places=2, min_value=Decimal('0.01'), required=False, allow_null=True)
    foreign_base_price = serializers.DecimalField(source='foreign_mmk_price', max_digits=14, decimal_places=2, min_value=Decimal('0.01'), required=False, allow_null=True)
    local_base_percentage = serializers.DecimalField(source='local_mmk_percentage', max_digits=7, decimal_places=2, read_only=True, allow_null=True)
    foreign_base_percentage = serializers.DecimalField(source='foreign_mmk_percentage', max_digits=7, decimal_places=2, read_only=True, allow_null=True)
    base_currency = serializers.CharField(source='room_type.hotel.base_currency', read_only=True)

    start_date = serializers.DateField(required=False)
    status = serializers.SerializerMethodField()

    class Meta:
        model = RoomPriceDiscount
        fields = ["id", "timing_mode", "start_date", "end_date", *API_PRICE_FIELDS, *API_PERCENTAGE_FIELDS, "base_currency",
                  "status", "ended_at", "created_at", "updated_at"]
        read_only_fields = [*API_PERCENTAGE_FIELDS, "ended_at", "created_at", "updated_at"]
        extra_kwargs = {field: {"required": False, "allow_null": True, "min_value": Decimal("0.01")} for field in API_PRICE_FIELDS}

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
            raise serializers.ValidationError("Set at least one Local/Foreigner base currency or USD discount price.")
        for field in PRICE_FIELDS:
            value, normal = attrs.get(field), getattr(room_type, field)
            if value is None:
                continue
            if normal is None or value >= normal:
                raise serializers.ValidationError({field.replace("_mmk_", "_base_"): "Set a discount price lower than its configured normal price."})
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


SEASONAL_PRICE_FIELDS = ("local_base_price", "local_usd_price", "foreign_base_price", "foreign_usd_price")


class RoomSeasonalRateSerializer(serializers.ModelSerializer):
    name = serializers.CharField(max_length=120, required=False, allow_null=True, allow_blank=True)
    base_currency = serializers.CharField(source='room_type.hotel.base_currency', read_only=True)

    class Meta:
        model = RoomSeasonalRate
        fields = ['id', 'name', 'start_date', 'end_date', *SEASONAL_PRICE_FIELDS, 'base_currency', 'ended_at']
        read_only_fields = ['id', 'ended_at']
        extra_kwargs = {field: {'min_value': Decimal('0.01'), 'required': False, 'allow_null': True} for field in SEASONAL_PRICE_FIELDS}

    def validate(self, attrs):
        room = self.context['room_type']
        if not room.room_price_setup:
            raise serializers.ValidationError('Set the normal room price first.')
        if self.instance and self.instance.ended_at:
            raise serializers.ValidationError('An ended seasonal rate cannot be edited.')
        start = attrs.get('start_date', getattr(self.instance, 'start_date', None))
        end = attrs.get('end_date', getattr(self.instance, 'end_date', None))
        if end < start:
            raise serializers.ValidationError({'end_date': 'Must be on or after start_date.'})
        if not any(attrs.get(field, getattr(self.instance, field, None)) is not None for field in SEASONAL_PRICE_FIELDS):
            raise serializers.ValidationError('At least one seasonal price is required.')
        overlaps = RoomSeasonalRate.objects.filter(room_type=room, ended_at__isnull=True, start_date__lte=end, end_date__gte=start)
        if self.instance:
            overlaps = overlaps.exclude(pk=self.instance.pk)
        if overlaps.exists():
            raise serializers.ValidationError('A seasonal rate overlaps this date range.')
        return attrs

    def _set_default_name(self, validated_data):
        if 'name' not in validated_data and self.instance is not None:
            return
        if not validated_data.get('name'):
            validated_data['name'] = 'Seasonal Rate'

    def create(self, validated_data):
        from django.db import transaction
        from booking.models import RoomType
        with transaction.atomic():
            RoomType.objects.select_for_update().get(pk=self.context['room_type'].pk)
            self._set_default_name(validated_data)
            return self.Meta.model.objects.create(room_type=self.context['room_type'], **validated_data)

    def update(self, instance, validated_data):
        from django.db import transaction
        from booking.models import RoomType
        with transaction.atomic():
            RoomType.objects.select_for_update().get(pk=self.context['room_type'].pk)
            self._set_default_name(validated_data)
            return super().update(instance, validated_data)
