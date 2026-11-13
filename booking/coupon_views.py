import json
import logging
from decimal import Decimal

from django.db import transaction
from django.shortcuts import get_object_or_404
from django.utils import timezone
from rest_framework import serializers
from rest_framework.exceptions import MethodNotAllowed
from rest_framework.parsers import FormParser, JSONParser, MultiPartParser
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.views import APIView

from booking.authentication import CoreJWTAuthentication
from booking.coupons import active_campaigns, campaign_matches_hotel, issue_coupons
from booking.integrations.core import CoreClient, CoreIntegrationError
from booking.models import BookingCoupon, CouponCampaign, Hotel
from booking.permissions import IsCoreSuperAdmin
from booking.storage import coupon_image_url
from config.response_formatter import success


class PublishAtField(serializers.DateTimeField):
    def to_internal_value(self, value):
        if value == "now":
            value = timezone.now()
        return super().to_internal_value(value)


class CampaignImageField(serializers.ImageField):
    def to_representation(self, value):
        return coupon_image_url(value, self.context.get("request"))


class CampaignSerializer(serializers.ModelSerializer):
    status = serializers.SerializerMethodField()
    banner_image = serializers.ImageField(required=False, write_only=True)
    popup_image = serializers.ImageField(required=False, write_only=True)
    banner_image_url = CampaignImageField(source="banner_image", read_only=True)
    popup_image_url = CampaignImageField(source="popup_image", read_only=True)
    publish_at = PublishAtField(required=False)
    type = serializers.ChoiceField(choices=["booking"], default="booking")
    discount_value = serializers.DecimalField(max_digits=14, decimal_places=2, min_value=Decimal("0.01"))
    quantity_per_user = serializers.IntegerField(min_value=1, max_value=100, default=1)
    state_ids = serializers.ListField(child=serializers.IntegerField(min_value=1), max_length=500, required=False)
    city_ids = serializers.ListField(child=serializers.IntegerField(min_value=1), max_length=500, required=False)
    business_ids = serializers.ListField(child=serializers.IntegerField(min_value=1), max_length=5000, required=False)
    user_ids = serializers.ListField(child=serializers.IntegerField(min_value=1), max_length=500, required=False)
    currency = serializers.RegexField(r"^[A-Z]{3}$")

    class Meta:
        model = CouponCampaign
        fields = "__all__"

    def get_status(self, campaign):
        now = timezone.now()
        if not campaign.is_active or campaign.end_at < now:
            return "expired"
        if max(campaign.publish_at, campaign.start_at) > now:
            return "upcoming"
        return "active"

    def validate(self, attrs):
        def value(name, default=None):
            return attrs.get(name, getattr(self.instance, name, default))

        if value("start_at") >= value("end_at"):
            raise serializers.ValidationError({"end_at": "Must be after start_at."})
        if value("publish_at", timezone.now()) > value("end_at"):
            raise serializers.ValidationError({"publish_at": "Must be on or before end_at."})
        if value("discount_type") == "percentage" and value("discount_value") > 100:
            raise serializers.ValidationError({"discount_value": "Percentage cannot exceed 100."})
        audience = value("audience", "public")
        if (audience == "targeted") != bool(value("user_ids", [])):
            raise serializers.ValidationError({"user_ids": "Provide user IDs for targeted campaigns only."})
        if self.instance and self.instance.coupons.exists():
            for field in ("audience", "quantity_per_user", "currency"):
                if value(field) != getattr(self.instance, field):
                    raise serializers.ValidationError({field: "Cannot change after coupons are issued."})
        return attrs


def request_user_id(request):
    return request.user.id if request.user and request.user.is_authenticated else None


def campaign_payload(campaign, request=None, tickets=()):
    remaining = sum(ticket.booking_id is None and ticket.used_at is None for ticket in tickets)
    payload = {
        "id": campaign.id, "title": campaign.title, "type": campaign.type,
        "discount_type": campaign.discount_type, "discount_value": campaign.discount_value,
        "currency": campaign.currency, "quantity_per_user": campaign.quantity_per_user,
        "category_id": campaign.category_id,
        "start_at": campaign.start_at, "end_at": campaign.end_at,
        "claim_status": "claimed" if tickets else "claimable",
        "remaining_quantity": remaining,
    }
    for field in ("banner_image", "popup_image"):
        payload[f"{field}_url"] = coupon_image_url(getattr(campaign, field), request)
    return payload


def available_campaign_payloads(hotels, user_id=None, request=None, country_id=None):
    hotels = list(hotels)
    if not hotels:
        return {}
    queryset = active_campaigns()
    if country_id is not None:
        queryset = queryset.filter(country_id=country_id)
    campaigns = [
        campaign for campaign in queryset.order_by("end_at", "id")
        if campaign.audience == "public" or user_id in campaign.user_ids
    ]
    tickets_by_campaign = {}
    if user_id and campaigns:
        for ticket in BookingCoupon.objects.filter(user_id=user_id, campaign__in=campaigns):
            tickets_by_campaign.setdefault(ticket.campaign_id, []).append(ticket)
    return {
        hotel.core_business_id: [
            campaign_payload(campaign, request, tickets_by_campaign.get(campaign.id, ()))
            for campaign in campaigns if campaign_matches_hotel(campaign, hotel)
        ] for hotel in hotels
    }


def ticket_status(ticket):
    state = "used" if ticket.used_at else "reserved" if ticket.booking_id else "available"
    if state == "available" and (
        not ticket.campaign.is_active or ticket.campaign.end_at < timezone.now()
        or (ticket.campaign.audience == "targeted" and ticket.user_id not in ticket.campaign.user_ids)
    ):
        state = "expired"
    if state == "available" and max(ticket.campaign.start_at, ticket.campaign.publish_at) > timezone.now():
        state = "scheduled"
    return state


def ticket_payload(ticket, request=None, *, status=None):
    return {
        "id": str(ticket.id), "code": ticket.code, "status": status or ticket_status(ticket),
        "campaign": campaign_payload(ticket.campaign, request, [ticket]),
    }


class CouponCampaignAdminView(APIView):
    authentication_classes = [CoreJWTAuthentication]
    permission_classes = [IsCoreSuperAdmin]
    parser_classes = [JSONParser, MultiPartParser, FormParser]

    def get(self, request, campaign_id=None):
        campaigns = (
            [get_object_or_404(CouponCampaign, pk=campaign_id)]
            if campaign_id is not None
            else list(CouponCampaign.objects.order_by("-id"))
        )
        rows = CampaignSerializer(campaigns, many=True, context={"request": request}).data
        scopes = {"countries": set(), "states": set(), "cities": set(), "businesses": set()}
        for campaign in campaigns:
            scopes["countries"].add(campaign.country_id)
            scopes["states"].update(campaign.state_ids)
            scopes["cities"].update(campaign.city_ids)
            scopes["businesses"].update(campaign.business_ids)

        names = {}
        if campaigns:
            try:
                names = CoreClient().post(
                    "booking-integrations/coupons/scope-names/",
                    json={key: sorted(ids) for key, ids in scopes.items()},
                )
            except CoreIntegrationError:
                logging.getLogger(__name__).warning("Could not resolve coupon scope names from Core.")

        for row in rows:
            row["country_name"] = names.get("countries", {}).get(str(row["country_id"]))
            for scope in ("state", "city", "business"):
                lookup = names.get({"state": "states", "city": "cities", "business": "businesses"}[scope], {})
                row[f"{scope}_names"] = [lookup.get(str(pk)) for pk in row[f"{scope}_ids"]]
        return success(rows[0] if campaign_id is not None else rows)

    @transaction.atomic
    def post(self, request, campaign_id=None):
        if campaign_id is not None:
            raise MethodNotAllowed(request.method)
        return self._save(request)

    @transaction.atomic
    def patch(self, request, campaign_id=None):
        if campaign_id is None:
            raise MethodNotAllowed(request.method)
        campaign = get_object_or_404(CouponCampaign.objects.select_for_update(), pk=campaign_id)
        return self._save(request, campaign)

    @staticmethod
    def _save(request, campaign=None):
        data = request.data
        if hasattr(data, "dict"):
            data = data.dict()
            for field in ("state_ids", "city_ids", "business_ids", "user_ids"):
                if field in data:
                    try:
                        data[field] = json.loads(data[field])
                    except (ValueError, TypeError) as exc:
                        raise serializers.ValidationError({field: "Use a JSON array of IDs."}) from exc
        serializer = CampaignSerializer(campaign, data=data, partial=campaign is not None, context={"request": request})
        serializer.is_valid(raise_exception=True)
        row = serializer.save()
        if row.audience == "targeted" and (campaign is None or "user_ids" in serializer.validated_data):
            for user_id in set(row.user_ids):
                issue_coupons(row, user_id)
        code = 201 if campaign is None else 200
        return success(serializer.data, status_code=code, status=code)


class CouponCampaignDiscoveryView(APIView):
    authentication_classes = [CoreJWTAuthentication]
    permission_classes = [AllowAny]

    def get(self, request):
        country_id = serializers.IntegerField(min_value=1).run_validation(request.query_params.get("country_id"))
        user_id = request_user_id(request)
        business_id = request.query_params.get("business_id")
        if business_id is not None:
            hotel = get_object_or_404(
                Hotel, core_business_id=serializers.IntegerField(min_value=1).run_validation(business_id),
            )
            return success(available_campaign_payloads([hotel], user_id, request, country_id)[hotel.core_business_id])
        campaigns = active_campaigns().filter(country_id=country_id).order_by("end_at", "id")
        tickets = {}
        if user_id:
            for coupon in BookingCoupon.objects.filter(user_id=user_id, campaign__in=campaigns):
                tickets.setdefault(coupon.campaign_id, []).append(coupon)
        return success([
            campaign_payload(campaign, request, tickets.get(campaign.id, ())) for campaign in campaigns
            if campaign.audience == "public" or user_id in campaign.user_ids
        ])


class CouponClaimView(APIView):
    authentication_classes = [CoreJWTAuthentication]
    permission_classes = [IsAuthenticated]

    @transaction.atomic
    def post(self, request, campaign_id):
        campaign = get_object_or_404(active_campaigns().select_for_update(), pk=campaign_id)
        user_id = request.user.id
        if campaign.audience == "targeted" and user_id not in campaign.user_ids:
            raise serializers.ValidationError({"coupon": "This campaign is unavailable."})
        if campaign.coupons.filter(user_id=user_id).exists():
            raise serializers.ValidationError({"coupon": "Already claimed."})
        return success([ticket_payload(ticket, request) for ticket in issue_coupons(campaign, user_id)])


class MyCouponsView(APIView):
    authentication_classes = [CoreJWTAuthentication]
    permission_classes = [IsAuthenticated]

    def get(self, request):
        requested_status = request.query_params.get("status")
        if requested_status is not None:
            requested_status = serializers.ChoiceField(
                choices=("available", "expired", "scheduled", "reserved", "used"),
            ).run_validation(requested_status)
        business_id = request.query_params.get("business_id")
        hotel = None
        if business_id is not None:
            business_id = serializers.IntegerField(min_value=1).run_validation(business_id)
            hotel = get_object_or_404(Hotel, core_business_id=business_id)
            if requested_status is None:
                requested_status = "available"
        tickets = BookingCoupon.objects.select_related("campaign").filter(user_id=request.user.id).order_by("campaign__end_at", "slot")
        results = []
        for ticket in tickets:
            if hotel is not None and not campaign_matches_hotel(ticket.campaign, hotel):
                continue
            status = ticket_status(ticket)
            if requested_status is None or status == requested_status:
                results.append(ticket_payload(ticket, request, status=status))
        return success(results)
