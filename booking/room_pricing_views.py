from django.db import transaction
from django.utils import timezone
from rest_framework.decorators import action
from rest_framework.exceptions import MethodNotAllowed, NotFound

from booking.models import RoomPriceDiscount, RoomType
from booking.room_pricing import sync_market_price, visible_room_discounts
from booking.room_pricing_serializers import EXTRA_BED_PRICE_FIELDS, ExtraBedPriceSetupSerializer, PRICE_FIELDS, RoomPriceDiscountSerializer, RoomPriceSetupSerializer
from booking.serializers import RatePlanSerializer
from config.response_formatter import success


class RoomPricingMixin:
    def create(self, request, *args, **kwargs):
        raise MethodNotAllowed(request.method)

    def update(self, request, *args, **kwargs):
        if request.method == "PUT":
            raise MethodNotAllowed(request.method)
        return super().update(request, *args, **kwargs)

    @staticmethod
    def _room_price_payload(room_type):
        return {
            "room_type_id": room_type.id, "core_room_type_id": room_type.core_room_type_id,
            "room_price_setup": room_type.room_price_setup,
            "discounts": RoomPriceDiscountSerializer(
                visible_room_discounts(room_type), many=True, context={"room_type": room_type},
            ).data,
            **{field: getattr(room_type, field) for field in PRICE_FIELDS},
            **{field: getattr(room_type, field) for field in EXTRA_BED_PRICE_FIELDS},
            "extra_bed_local_base_price": room_type.extra_bed_local_mmk_price,
            "extra_bed_foreign_base_price": room_type.extra_bed_foreign_mmk_price,
            "extra_bed_base_currency": "MMK",
            "rate_plans": RatePlanSerializer(room_type.rate_plans.filter(is_active=True).order_by("id"), many=True).data,
        }

    @action(detail=True, methods=["get", "put", "patch"], url_path="room-price")
    @transaction.atomic
    def room_price(self, request, pk=None):
        room_type = RoomType.objects.select_for_update().select_related("hotel").get(pk=self.get_object().pk)
        if request.method != "GET":
            serializer = RoomPriceSetupSerializer(data=request.data, context={"room_type": room_type})
            serializer.is_valid(raise_exception=True)
            for field, value in serializer.validated_data.items():
                setattr(room_type, field, value)
            room_type.room_price_setup = True
            room_type.save(update_fields=[*PRICE_FIELDS, "room_price_setup"])
            for market in ("local", "foreign"):
                sync_market_price(room_type, market)
        return success(self._room_price_payload(room_type))

    @action(detail=True, methods=['get', 'put', 'patch'], url_path='extra-bed-price')
    @transaction.atomic
    def extra_bed_price(self, request, pk=None):
        room_type = RoomType.objects.select_for_update().select_related('hotel').get(pk=self.get_object().pk)
        if request.method != 'GET':
            serializer = ExtraBedPriceSetupSerializer(data=request.data, context={'room_type': room_type})
            serializer.is_valid(raise_exception=True)
            values = serializer.validated_data
            if request.method == 'PUT':
                values = {field: values.get(field) for field in EXTRA_BED_PRICE_FIELDS}
            for field, value in values.items():
                setattr(room_type, field, value)
            if values:
                room_type.save(update_fields=list(values))
        return success({**self._room_price_payload(room_type), "base_currency": "MMK"})

    @action(detail=True, methods=["get", "post"], url_path="discounts")
    @transaction.atomic
    def discounts(self, request, pk=None):
        room_type = RoomType.objects.select_for_update().get(pk=self.get_object().pk)
        context = {"room_type": room_type}
        if request.method == "GET":
            return success({"room_type_id": room_type.id, "discounts": RoomPriceDiscountSerializer(
                visible_room_discounts(room_type), many=True, context=context,
            ).data})
        serializer = RoomPriceDiscountSerializer(data=request.data, context=context)
        serializer.is_valid(raise_exception=True)
        discount = serializer.save()
        return success(RoomPriceDiscountSerializer(discount, context=context).data, status_code=201)

    @action(detail=True, methods=["get", "patch", "put"], url_path=r"discounts/(?P<discount_id>[0-9]+)")
    @transaction.atomic
    def discount_detail(self, request, pk=None, discount_id=None):
        room_type = RoomType.objects.select_for_update().get(pk=self.get_object().pk)
        try:
            discount = room_type.price_discounts.select_for_update().get(pk=discount_id)
        except RoomPriceDiscount.DoesNotExist:
            raise NotFound("Room price discount was not found.")
        context = {"room_type": room_type}
        if request.method == "GET":
            return success(RoomPriceDiscountSerializer(discount, context=context).data)
        serializer = RoomPriceDiscountSerializer(
            discount, data=request.data, partial=request.method == "PATCH", context=context,
        )
        serializer.is_valid(raise_exception=True)
        serializer.save()
        return success(serializer.data)

    @action(detail=True, methods=["post"], url_path=r"discounts/(?P<discount_id>[0-9]+)/end")
    @transaction.atomic
    def end_discount(self, request, pk=None, discount_id=None):
        room_type = RoomType.objects.select_for_update().get(pk=self.get_object().pk)
        try:
            discount = room_type.price_discounts.select_for_update().get(pk=discount_id)
        except RoomPriceDiscount.DoesNotExist:
            raise NotFound("Room price discount was not found.")
        if discount.ended_at is None:
            discount.ended_at = timezone.now()
            discount.save(update_fields=["ended_at", "updated_at"])
        return success(RoomPriceDiscountSerializer(discount).data)
