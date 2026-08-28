from collections import defaultdict
from datetime import timedelta
from decimal import Decimal
import json
import re

from django.conf import settings
from django.shortcuts import redirect
from django.db import transaction
from django.db.models import Count, Max, Prefetch, Q
from django.utils import timezone
from django.utils.dateparse import parse_date
from django.utils.text import slugify
from rest_framework import mixins, status, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import NotFound, PermissionDenied, ValidationError
from rest_framework.permissions import AllowAny
from rest_framework.views import APIView

from booking.authentication import CoreJWTAuthentication
from booking.integrations.core import CoreClient, sync_business_from_core
from booking.models import AddOn, AddOnTemplate, AddOnTemplateRequest, Booking, BookingRoom, CoreIntegrationEvent, DailyInventory, DailyRate, Guest, GuestIdentityDocument, Hotel, Invoice, MealPlan, Payment, PhysicalRoom, PhysicalRoomActionHistory, PhysicalRoomBlock, RatePlan, RatePeriod, RoomAssignment, RoomType, RoomTypeMealPlan
from booking.permissions import HasBookingAdminKey, IsCoreSuperAdmin
from booking.serializers import (
    AddOnSerializer,
    AddOnTemplateApprovalSerializer,
    AddOnTemplateRejectionSerializer,
    AddOnTemplateRequestSerializer,
    AddOnTemplateSerializer,
    AvailabilitySearchQuerySerializer,
    AdminReservationCreateSerializer,
    BookingCreateSerializer,
    BookingEstimateSerializer,
    BookingSerializer,
    CheckInConfirmSerializer,
    CheckInFormUpdateSerializer,
    CorePaymentSuccessSerializer,
    CoreEventSerializer,
    DailyInventorySerializer,
    DailyInventoryBulkUpsertSerializer,
    DailyRateSerializer,
    DailyRateBulkUpsertSerializer,
    HotelSerializer,
    InvoiceCreateSerializer,
    InvoiceSerializer,
    GuestIdentityDocumentSerializer,
    GuestIdentityDocumentUploadSerializer,
    MealPlanSerializer,
    OTARoomSelectionUpdateSerializer,
    OTARoomSaleStatusSerializer,
    OTARoomTimelineQuerySerializer,
    PaymentCreateSerializer,
    PaymentSerializer,
    PMSAvailableRoomSearchSerializer,
    PhysicalRoomSerializer,
    PhysicalRoomActionHistorySerializer,
    PhysicalRoomBlockSerializer,
    PublicOTARoomTypeCatalogQuerySerializer,
    PublicOTARoomTypeCatalogSerializer,
    PublicHotelSerializer,
    RatePlanSerializer,
    RatePeriodSerializer,
    RefundCreateSerializer,
    RoomAssignmentCreateSerializer,
    RoomAssignmentSerializer,
    RoomChangeSerializer,
    RoomUnassignmentSerializer,
    RoomBoardQuerySerializer,
    RoomTypeMealPlanSerializer,
    RoomTypeSerializer,
    WalkInBookingCreateSerializer,
)
from booking.services import availability_for_hotel_with_display, availability_for_hotels, cancel_booking, create_admin_reservation, create_booking, create_invoice, create_walk_in_booking, deprovision_hotel, ensure_daily_inventory_for_room_type, estimate_booking, format_money, record_payment, refund_payment, refund_quote as calculate_refund_quote, release_checked_in_booking_inventory, update_reservation_for_check_in, validate_assignment_preferences
from config.response_formatter import success


def _pluralize_day_label(days):
    return "Day" if days == 1 else "Days"


def _pluralize_night_label(nights):
    return "Night" if nights == 1 else "Nights"


def _room_history_actor_type(request=None, booking=None):
    if booking and booking.source == Booking.Source.OTA:
        return PhysicalRoomActionHistory.ActorType.OTA
    if request is not None and _core_user_id(request):
        return PhysicalRoomActionHistory.ActorType.HOTEL_ADMIN
    if booking and booking.source == Booking.Source.DIRECT:
        return PhysicalRoomActionHistory.ActorType.GUEST
    return PhysicalRoomActionHistory.ActorType.SYSTEM


def _record_room_history(
    room, action, *, request=None, booking=None, block=None,
    old_status="", new_status="", note="", metadata=None,
):
    event_metadata = dict(metadata or {})
    if request is not None:
        actor_name = request.headers.get("X-Core-User-Name", "").strip()
        actor_email = request.headers.get("X-Core-User-Email", "").strip()
        user = getattr(request, "user", None)
        if user is not None and getattr(user, "is_authenticated", False):
            get_full_name = getattr(user, "get_full_name", None)
            if callable(get_full_name):
                actor_name = actor_name or get_full_name().strip()
            actor_email = actor_email or str(getattr(user, "email", "") or "").strip()
            actor_name = actor_name or actor_email or str(getattr(user, "username", "") or "").strip()
        if actor_name:
            event_metadata.setdefault("actor_name", actor_name)
        if actor_email:
            event_metadata.setdefault("actor_email", actor_email)
    return PhysicalRoomActionHistory.objects.create(
        physical_room=room,
        booking=booking,
        block=block,
        action=action,
        actor_type=_room_history_actor_type(request=request, booking=booking),
        actor_core_user_id=_core_user_id(request) if request is not None else None,
        old_status=old_status or "",
        new_status=new_status or "",
        note=note or "",
        metadata=event_metadata,
    )


def _record_booking_room_assignments(booking, request, action):
    assignments = RoomAssignment.objects.filter(
        booking_room__booking=booking,
        released_at__isnull=True,
    ).select_related("physical_room")
    for assignment in assignments:
        room = assignment.physical_room
        _record_room_history(
            room,
            action,
            request=request,
            booking=booking,
            old_status=(
                PhysicalRoom.Status.VACANT
                if action == PhysicalRoomActionHistory.Action.CHECKED_IN
                else room.status
            ),
            new_status=room.status,
            note=booking.special_request,
            metadata={
                "assignment_id": assignment.id,
                "check_in": str(booking.check_in),
                "check_out": str(booking.check_out),
                "booking_source": booking.source,
            },
        )


def _check_in_readiness(booking):
    booking = Booking.objects.prefetch_related(
        "rooms__assignments__physical_room",
        "guests__identity_documents",
        "payments",
    ).get(pk=booking.pk)
    missing = []
    guests = list(booking.guests.all())
    primary_guest = next((guest for guest in guests if guest.is_primary), None)
    if not primary_guest:
        missing.append("primary_guest")

    unassigned = []
    non_vacant = []
    for room in booking.rooms.all():
        assignments = [item for item in room.assignments.all() if item.released_at is None]
        if len(assignments) < room.quantity:
            unassigned.append({"booking_room_id": room.id, "quantity_unassigned": room.quantity - len(assignments)})
        non_vacant.extend(
            assignment.physical_room.room_number
            for assignment in assignments
            if assignment.physical_room.status != PhysicalRoom.Status.VACANT
        )
    if unassigned:
        missing.append("room_assignments")
    if non_vacant:
        missing.append("assigned_rooms_not_vacant")
    if booking.amount_paid <= 0:
        payment_status = "unpaid"
    elif booking.amount_paid >= booking.grand_total:
        payment_status = "paid"
    else:
        payment_status = "partially_paid"
    return {
        "guest_information_complete": primary_guest is not None,
        # Identity numbers and uploaded identity photos are currently optional.
        "identity_information_complete": True,
        # Backward-compatible field retained for existing mobile clients.
        "identity_documents_complete": True,
        "all_rooms_assigned": not unassigned,
        "assigned_rooms_vacant": not non_vacant,
        "payment_status": payment_status,
        "can_check_in": not missing and booking.status == Booking.Status.CONFIRMED,
        "missing_fields": missing,
        "unassigned_booking_rooms": unassigned,
        "non_vacant_room_numbers": non_vacant,
    }


def _payment_summary(booking):
    amount_due = max(booking.grand_total - booking.amount_paid, 0)
    return {
        "currency": booking.currency,
        "room_total": booking.room_total,
        "add_on_total": booking.add_on_total,
        "tax_total": booking.tax_total,
        "discount_total": booking.discount_total,
        "grand_total": booking.grand_total,
        "amount_paid": booking.amount_paid,
        "amount_due": amount_due,
        "payment_status": (
            "paid" if amount_due == 0
            else "partially_paid" if booking.amount_paid > 0
            else "unpaid"
        ),
        "transaction_url": f"/api/v1/admin/bookings/{booking.id}/payment/",
        "payment_types": ["deposit", "balance", "full_payment"],
    }


class PublicAvailabilityView(APIView):
    permission_classes = [AllowAny]

    def get(self, request, core_business_id):
        hotel = Hotel.objects.filter(core_business_id=core_business_id, is_active=True).first()
        if not hotel:
            raise NotFound("Hotel is not available in the booking engine.")
        check_in = parse_date(request.query_params.get("check_in", ""))
        check_out = parse_date(request.query_params.get("check_out", ""))
        if not check_in or not check_out:
            raise ValidationError({"dates": "check_in and check_out are required in YYYY-MM-DD format."})
        results = availability_for_hotel_with_display(
            hotel,
            check_in,
            check_out,
            int(request.query_params.get("adults", 1)),
            int(request.query_params.get("children", 0)),
            request.query_params.get("guest_market", "local"),
            request.query_params.get("display_currency") or None,
        )
        return success({"hotel": PublicHotelSerializer(hotel).data, "room_types": results})


class PublicOTARoomTypeCatalogView(APIView):
    """List OTA-selected room types without calculating date availability."""

    permission_classes = [AllowAny]

    def get(self, request, core_business_id):
        query = PublicOTARoomTypeCatalogQuerySerializer(data=request.query_params)
        query.is_valid(raise_exception=True)
        hotel = Hotel.objects.filter(
            core_business_id=core_business_id,
            is_active=True,
            package__in=[Hotel.Package.OTA, Hotel.Package.OTA_PMS],
        ).first()
        if not hotel:
            raise NotFound("OTA hotel is not available in the booking engine.")

        room_types = list(
            RoomType.objects.filter(
                hotel=hotel,
                booking_enabled=True,
                core_active=True,
                physical_rooms__is_active=True,
                physical_rooms__ota_enabled=True,
                physical_rooms__ota_sale_open=True,
                rate_plans__is_active=True,
            ).annotate(
                ota_enabled_room_count=Count(
                    "physical_rooms",
                    filter=Q(
                        physical_rooms__is_active=True,
                        physical_rooms__ota_enabled=True,
                        physical_rooms__ota_sale_open=True,
                    ),
                    distinct=True,
                ),
                ota_open_room_count=Count(
                    "physical_rooms",
                    filter=Q(
                        physical_rooms__is_active=True,
                        physical_rooms__ota_enabled=True,
                        physical_rooms__ota_sale_open=True,
                    ),
                    distinct=True,
                ),
            ).select_related("hotel").prefetch_related(
                "meal_plan_links", "meal_plan_links__meal_plan",
                Prefetch(
                    "rate_plans",
                    queryset=RatePlan.objects.filter(is_active=True).order_by("guest_market", "code", "id"),
                    to_attr="ota_catalog_rate_plans",
                ),
            ).distinct().order_by("name", "id")
        )
        context = {
            "request": request,
            "guest_market": query.validated_data.get("guest_market"),
            "display_currency": query.validated_data.get("display_currency"),
        }
        return success({
            "hotel": PublicHotelSerializer(hotel).data,
            "room_types": PublicOTARoomTypeCatalogSerializer(
                room_types,
                many=True,
                context=context,
            ).data,
            "availability_calculated": False,
            "availability_url": f"/api/v1/public/hotels/{core_business_id}/availability/",
        })


class PublicHotelRoomTypeCatalogView(APIView):
    """List every active room type and identify whether it is currently OTA-bookable."""

    permission_classes = [AllowAny]

    def get(self, request, core_business_id=None):
        if core_business_id is None:
            try:
                core_business_id = int(request.query_params.get("business_id", ""))
            except (TypeError, ValueError):
                raise ValidationError({"business_id": "A valid business_id is required."})
        query = PublicOTARoomTypeCatalogQuerySerializer(data=request.query_params)
        query.is_valid(raise_exception=True)
        hotel = Hotel.objects.filter(core_business_id=core_business_id, is_active=True).first()
        if not hotel:
            raise NotFound("Hotel is not available in the booking engine.")

        room_types = list(
            RoomType.objects.filter(hotel=hotel, core_active=True)
            .annotate(
                ota_enabled_room_count=Count(
                    "physical_rooms",
                    filter=Q(
                        physical_rooms__is_active=True,
                        physical_rooms__ota_enabled=True,
                    ),
                    distinct=True,
                ),
                ota_open_room_count=Count(
                    "physical_rooms",
                    filter=Q(
                        physical_rooms__is_active=True,
                        physical_rooms__ota_enabled=True,
                        physical_rooms__ota_sale_open=True,
                    ),
                    distinct=True,
                ),
            )
            .select_related("hotel")
            .prefetch_related(
                "meal_plan_links", "meal_plan_links__meal_plan",
                Prefetch(
                    "rate_plans",
                    queryset=RatePlan.objects.filter(is_active=True).order_by("guest_market", "code", "id"),
                    to_attr="ota_catalog_rate_plans",
                ),
            )
            .order_by("name", "id")
        )
        context = {
            "request": request,
            "guest_market": query.validated_data.get("guest_market"),
            "display_currency": query.validated_data.get("display_currency"),
        }
        return success({
            "hotel": PublicHotelSerializer(hotel).data,
            "room_types": PublicOTARoomTypeCatalogSerializer(room_types, many=True, context=context).data,
            "availability_calculated": False,
            "availability_url": f"/api/v1/public/hotels/{core_business_id}/availability/",
        })


class PublicGlobalAvailabilityView(APIView):
    permission_classes = [AllowAny]

    def get(self, request):
        serializer = AvailabilitySearchQuerySerializer(data=request.query_params)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        hotels = Hotel.objects.filter(
            is_active=True,
            room_types__booking_enabled=True,
            room_types__core_active=True,
            room_types__rate_plans__is_active=True,
            room_types__rate_plans__guest_market__in=[data["guest_market"], RatePlan.GuestMarket.ALL],
        ).distinct().order_by("id")
        query = data.get("q", "")
        if query:
            hotels = hotels.filter(Q(name__icontains=query) | Q(slug__icontains=query) | Q(address__icontains=query))
        hotels = list(hotels)
        availability = availability_for_hotels(
            hotels,
            data["check_in"],
            data["check_out"],
            data["adults"],
            data["children"],
            data["guest_market"],
            data.get("display_currency"),
        )
        available_hotels = [hotel for hotel in hotels if availability.get(hotel.id)]
        total = len(available_hotels)
        start = (data["page"] - 1) * data["page_size"]
        end = start + data["page_size"]
        page_hotels = available_hotels[start:end]
        results = [
            {
                "hotel": PublicHotelSerializer(hotel).data,
                "room_types": availability[hotel.id],
            }
            for hotel in page_hotels
        ]
        return success({
            "results": results,
            "pagination": {
                "page": data["page"],
                "page_size": data["page_size"],
                "total": total,
                "total_pages": (total + data["page_size"] - 1) // data["page_size"],
                "has_next": end < total,
                "has_previous": data["page"] > 1,
            },
        })


class PublicBookingCreateView(APIView):
    permission_classes = [AllowAny]

    @transaction.atomic
    def post(self, request):
        data = request.data.dict() if hasattr(request.data, "dict") else request.data.copy()
        nested_guests = {}
        nested_rooms = {}
        identity_photos = {}
        guest_field_pattern = re.compile(
            r"^guests?\[(\d+)\]\[(?:['\"])?([a-zA-Z_][a-zA-Z0-9_]*)(?:['\"])?\]$"
        )
        room_field_pattern = re.compile(
            r"^rooms?\[(\d+)\]\[(?:['\"])?([a-zA-Z_][a-zA-Z0-9_]*)(?:['\"])?\]$"
        )
        room_preference_field_pattern = re.compile(
            r"^rooms?\[(\d+)\]\[(?:['\"])?preferences(?:['\"])?\]"
            r"\[(?:['\"])?([a-zA-Z_][a-zA-Z0-9_]*)(?:['\"])?\]$"
        )
        for key, value in request.data.items():
            match = guest_field_pattern.match(key)
            if match:
                index = int(match.group(1))
                field = match.group(2)
                if field in {"photo", "identity_photo"}:
                    if key in request.FILES:
                        identity_photos[index] = request.FILES[key]
                    continue
                nested_guests.setdefault(index, {})[field] = value
                continue

            room_preference_match = room_preference_field_pattern.match(key)
            if room_preference_match:
                index = int(room_preference_match.group(1))
                field = room_preference_match.group(2)
                nested_rooms.setdefault(index, {}).setdefault("preferences", {})[field] = value
                continue

            room_match = room_field_pattern.match(key)
            if room_match:
                index = int(room_match.group(1))
                field = room_match.group(2)
                if field == "preferences" and isinstance(value, str):
                    try:
                        value = json.loads(value)
                    except (TypeError, ValueError):
                        raise ValidationError({key: "Must be valid JSON."})
                nested_rooms.setdefault(index, {})[field] = value

        if nested_guests:
            indexes = sorted(nested_guests)
            if indexes != list(range(len(indexes))):
                raise ValidationError({"guests": "Guest indexes must start at 0 and be consecutive."})
            data["guests"] = [nested_guests[index] for index in indexes]
        if nested_rooms:
            indexes = sorted(nested_rooms)
            if indexes != list(range(len(indexes))):
                raise ValidationError({"rooms": "Room indexes must start at 0 and be consecutive."})
            data["rooms"] = [nested_rooms[index] for index in indexes]

        for field in ("rooms", "guests", "add_ons"):
            raw_value = data.get(field)
            if isinstance(raw_value, str):
                try:
                    data[field] = json.loads(raw_value)
                except (TypeError, ValueError):
                    raise ValidationError({field: "Must be valid JSON when using multipart/form-data."})

        serializer = BookingCreateSerializer(data=data)
        serializer.is_valid(raise_exception=True)
        try:
            booking, created = create_booking(serializer.validated_data, request.headers.get("Idempotency-Key"))
        except Hotel.DoesNotExist:
            raise NotFound("Hotel is not available in the booking engine.")
        if created and identity_photos:
            created_guests = list(booking.guests.order_by("id"))
            guests_data = serializer.validated_data.get("guests") or []
            for index, file in identity_photos.items():
                if index >= len(created_guests):
                    raise ValidationError({f"guest[{index}][photo]": "Guest index does not exist."})
                guest_data = guests_data[index]
                GuestIdentityDocument.objects.create(
                    guest=created_guests[index],
                    document_type=GuestIdentityDocument.DocumentType.IDENTITY_PHOTO,
                    document_number=(
                        guest_data.get("identity_number")
                        or guest_data.get("nrc_number")
                        or guest_data.get("passport_number")
                        or ""
                    ),
                    file=file,
                )
        response_status = status.HTTP_201_CREATED if created else status.HTTP_200_OK
        return success(
            BookingSerializer(booking).data,
            extra_dict={"created": created},
            status_code=response_status,
            status=response_status,
        )


class PublicBookingEstimateView(APIView):
    permission_classes = [AllowAny]

    def post(self, request):
        serializer = BookingEstimateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            return success(estimate_booking(serializer.validated_data))
        except Hotel.DoesNotExist:
            raise NotFound("Hotel is not available in the booking engine.")


class PublicBookingDetailView(APIView):
    permission_classes = [AllowAny]

    def get(self, request, public_token):
        booking = Booking.objects.filter(public_token=public_token).select_related("hotel").prefetch_related("rooms__nights", "guests", "add_ons", "payments").first()
        if not booking:
            raise NotFound("Booking not found.")
        return success(BookingSerializer(booking).data)


class PublicStayBillView(APIView):
    permission_classes = [AllowAny]

    def get(self, request, public_token):
        booking = Booking.objects.filter(public_token=public_token).select_related("hotel").prefetch_related(
            "invoices__lines", "invoices__receipts",
        ).first()
        if not booking:
            raise NotFound("Booking not found.")
        return success({
            "booking_id": str(booking.id),
            "booking_reference": booking.reference,
            "stay_status": booking.status,
            "currency": booking.currency,
            "invoices": InvoiceSerializer(booking.invoices.all(), many=True).data,
        })


class PublicDemoPaymentView(APIView):
    permission_classes = [AllowAny]

    def post(self, request, public_token):
        if not settings.DEMO_PAYMENT_ENABLED:
            raise NotFound("Demo payment is not available.")
        booking = Booking.objects.filter(public_token=public_token).select_related("hotel").first()
        if not booking:
            raise NotFound("Booking not found.")

        existing = booking.payments.filter(provider=Payment.Provider.DEMO, status=Payment.Status.PAID).order_by("-created_at").first()
        if existing and booking.status == Booking.Status.CONFIRMED:
            return success(
                {"booking": BookingSerializer(booking).data, "payment": PaymentSerializer(existing).data},
                extra_dict={"duplicate": True},
            )
        if booking.status != Booking.Status.PENDING_PAYMENT:
            raise ValidationError(f"A booking in status '{booking.status}' cannot be demo-paid.")

        amount_due = booking.grand_total - booking.amount_paid
        payment = record_payment(booking, {
            "provider": Payment.Provider.DEMO,
            "provider_reference": f"DEMO-{booking.reference}",
            "status": Payment.Status.PAID,
            "amount": amount_due,
            "metadata": {"demo": True},
        })
        booking.refresh_from_db()
        return success(
            {"booking": BookingSerializer(booking).data, "payment": PaymentSerializer(payment).data},
            extra_dict={"duplicate": False},
            status_code=status.HTTP_201_CREATED,
            status=status.HTTP_201_CREATED,
        )


class PublicAYAPaymentView(APIView):
    permission_classes = [AllowAny]

    def post(self, request, public_token):
        booking = Booking.objects.filter(public_token=public_token).select_related("hotel").first()
        if not booking:
            raise NotFound("Booking not found.")
        if booking.status != Booking.Status.PENDING_PAYMENT:
            raise ValidationError(f"A booking in status '{booking.status}' cannot start AYA payment.")

        amount_due = booking.grand_total - booking.amount_paid
        if amount_due <= 0:
            raise ValidationError("This booking has no outstanding payment amount.")

        checkout = CoreClient().post("direct-booking/hotel-bookings/aya-checkout/", {
            "business_id": booking.hotel.core_business_id,
            "booking_id": str(booking.id),
            "booking_public_token": str(booking.public_token),
            "amount": int(amount_due),
            "currency": booking.currency,
            "description": f"Visit77 hotel booking payment for {booking.hotel.name}",
            "customer_name": booking.contact_name,
            "customer_phone": booking.contact_phone,
            "customer_email": booking.contact_email,
            "channel": request.data.get("channel") or "",
            "method": request.data.get("method") or "",
        })
        return success({
            "booking": BookingSerializer(booking).data,
            "checkout": checkout,
        })


class CorePaymentSuccessView(APIView):
    permission_classes = [HasBookingAdminKey]

    def post(self, request):
        serializer = CorePaymentSuccessSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        query = Q()
        if data.get("booking_id"):
            query |= Q(id=data["booking_id"])
        if data.get("booking_public_token"):
            query |= Q(public_token=data["booking_public_token"])

        booking = Booking.objects.filter(query).select_related("hotel").first()
        if not booking:
            raise NotFound("Booking not found.")
        if booking.hotel.core_business_id != data["business_id"]:
            raise ValidationError("Booking does not belong to the supplied business.")

        existing = booking.payments.filter(
            provider=Payment.Provider.AYA,
            provider_reference=data["payment_reference"],
            status=Payment.Status.PAID,
        ).order_by("-created_at").first()
        if existing:
            booking.refresh_from_db()
            return success(
                {"booking": BookingSerializer(booking).data, "payment": PaymentSerializer(existing).data},
                extra_dict={"duplicate": True},
            )

        if booking.status != Booking.Status.PENDING_PAYMENT:
            raise ValidationError(f"A booking in status '{booking.status}' cannot be marked paid by Core.")

        payment_payload = data["payment"] or {}
        payment = record_payment(booking, {
            "provider": Payment.Provider.AYA,
            "provider_reference": data["payment_reference"],
            "status": Payment.Status.PAID,
            "amount": data["amount"],
            "metadata": {
                "source": "visit77_core_aya_webhook",
                "core_payment": payment_payload,
                "aya": data.get("aya") or {},
            },
        })
        booking.refresh_from_db()
        return success(
            {"booking": BookingSerializer(booking).data, "payment": PaymentSerializer(payment).data},
            extra_dict={"duplicate": False},
            status_code=status.HTTP_201_CREATED,
            status=status.HTTP_201_CREATED,
        )


class PublicHotelAddOnsView(APIView):
    permission_classes = [AllowAny]

    def get(self, request, core_business_id):
        hotel = Hotel.objects.filter(core_business_id=core_business_id, is_active=True).first()
        if not hotel:
            raise NotFound("Hotel is not available in the booking engine.")
        add_ons = AddOn.objects.filter(hotel=hotel, is_active=True).order_by("name", "id")
        return success(AddOnSerializer(add_ons, many=True).data)


class AddOnTemplateView(APIView):
    permission_classes = [HasBookingAdminKey]
    business_scoped = True

    def get(self, request):
        templates = AddOnTemplate.objects.filter(status=AddOnTemplate.Status.PUBLISHED).order_by("code", "-version", "-id")
        latest = {}
        for template in templates:
            latest.setdefault(template.code, template)
        return success(AddOnTemplateSerializer(latest.values(), many=True).data)


class PMSAvailableRoomSearchView(APIView):
    """Search assignable PMS rooms and group them for the multi-room form."""

    permission_classes = [HasBookingAdminKey]
    business_scoped = True

    @staticmethod
    def _natural_room_number(value):
        return tuple(
            int(part) if part.isdigit() else part.lower()
            for part in re.split(r"(\d+)", value or "")
        )

    @staticmethod
    def _room_details(room):
        snapshot = room.core_snapshot or {}
        room_type_snapshot = room.room_type.core_snapshot or {}
        beds = snapshot.get("beds") or []
        bed_types = [
            bed.get("bed_type") for bed in beds
            if isinstance(bed, dict) and bed.get("bed_type")
        ]
        room_view = snapshot.get("room_view") or snapshot.get("view")
        room_standard = snapshot.get("room_standard") or room_type_snapshot.get("room_standard")
        return {
            "id": room.id,
            "physical_room_id": room.id,
            "core_physical_room_id": room.core_physical_room_id,
            "room_number": room.room_number,
            "building_id": room.core_building_id,
            "building": room.building,
            "floor_id": room.core_floor_id,
            "floor": room.floor,
            "operational_status": room.status,
            # Room standard is owned by the room type in Core, but repeating it
            # here keeps each selectable physical-room card self-contained.
            "room_standard": room_standard,
            "room_standard_id": (
                room_standard.get("id") if isinstance(room_standard, dict) else None
            ),
            "extra_bed_available": bool(snapshot.get("extra_bed_available", False)),
            "extra_bed_quantity": int(snapshot.get("extra_bed_quantity") or 0),
            "beds": beds,
            "bed_type": snapshot.get("bed_type") or (bed_types[0] if bed_types else None),
            "bed_types": bed_types,
            "room_view": room_view,
            "room_views": snapshot.get("room_views") or ([room_view] if room_view else []),
            "bath_type": snapshot.get("bath_type"),
            "bath_types": snapshot.get("bath_types") or ([snapshot.get("bath_type")] if snapshot.get("bath_type") else []),
            "room_area": snapshot.get("room_area"),
            "area_unit": snapshot.get("area_unit"),
            "size_sqft": snapshot.get("size_sqft") or (
                snapshot.get("room_area") if snapshot.get("area_unit") == "sqft" else None
            ),
        }

    @staticmethod
    def _effective_plan_price(plan, dates):
        overrides = {
            row.stay_date: row
            for row in DailyRate.objects.filter(rate_plan=plan, stay_date__in=dates)
        }
        periods = list(RatePeriod.objects.filter(
            rate_plan=plan,
            is_active=True,
            start_date__lte=dates[-1],
        ).filter(Q(end_date__isnull=True) | Q(end_date__gte=dates[0])).order_by("-start_date", "-id"))
        nightly = []
        for day in dates:
            rule = overrides.get(day) or next((
                period for period in periods
                if period.start_date <= day and (period.end_date is None or period.end_date >= day)
            ), None)
            nightly.append({
                "date": day.isoformat(),
                "price": rule.base_price if rule else plan.base_price,
            })
        return nightly

    @staticmethod
    def _plan_for(room_type, guest_market):
        plans = RatePlan.objects.filter(
            room_type=room_type,
            is_active=True,
            guest_market__in=[guest_market, RatePlan.GuestMarket.ALL],
        )
        return (
            plans.filter(guest_market=guest_market, is_default=True).order_by("id").first()
            or plans.filter(guest_market=RatePlan.GuestMarket.ALL, is_default=True).order_by("id").first()
            or plans.filter(guest_market=guest_market).order_by("id").first()
            or plans.filter(guest_market=RatePlan.GuestMarket.ALL).order_by("id").first()
        )

    def get(self, request):
        serializer = PMSAvailableRoomSearchSerializer(data=request.query_params)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        core_business_id = getattr(request, "booking_core_business_id", None)
        if not core_business_id:
            raise ValidationError({"core_business_id": "X-Booking-Business-ID is required."})
        hotel = Hotel.objects.filter(core_business_id=core_business_id, is_active=True).first()
        if not hotel:
            raise NotFound("Hotel is not synced in the booking engine.")
        if hotel.package not in [Hotel.Package.FREE, Hotel.Package.PMS, Hotel.Package.OTA_PMS]:
            raise PermissionDenied("Available-room search requires a PMS or OTA + PMS package.")

        check_in, check_out = data["check_in"], data["check_out"]
        selected_ids = set(data["selected_room_ids"])
        current_room = None
        if data.get("current_room_id"):
            current_room = PhysicalRoom.objects.select_related("room_type").filter(
                id=data["current_room_id"], hotel=hotel, is_active=True,
            ).first()
            if not current_room:
                raise ValidationError({"current_room_id": "Current room does not belong to this hotel."})
            selected_ids.add(current_room.id)

        overlapping_ids = RoomAssignment.objects.filter(
            released_at__isnull=True,
            booking_room__booking__status__in=[
                Booking.Status.PENDING_PAYMENT,
                Booking.Status.CONFIRMED,
                Booking.Status.CHECKED_IN,
            ],
            booking_room__booking__check_in__lt=check_out,
            booking_room__booking__check_out__gt=check_in,
        ).values_list("physical_room_id", flat=True)
        blocked_ids = PhysicalRoomBlock.objects.filter(
            is_active=True,
            start_date__lt=check_out,
            end_date__gte=check_in,
        ).values_list("physical_room_id", flat=True)
        rooms = PhysicalRoom.objects.filter(
            hotel=hotel,
            is_active=True,
            room_type__booking_enabled=True,
            room_type__core_active=True,
        ).exclude(
            status=PhysicalRoom.Status.OUT_OF_SERVICE,
        ).exclude(
            id__in=selected_ids,
        ).exclude(
            id__in=overlapping_ids,
        ).exclude(
            id__in=blocked_ids,
        ).select_related("room_type")
        if data["workflow"] == "check_in":
            rooms = rooms.filter(status=PhysicalRoom.Status.VACANT)

        adults, children = data["adults"], data["children"]
        rooms = list(rooms.filter(
            room_type__max_adults__gte=adults,
            room_type__max_children__gte=children,
            room_type__max_occupancy__gte=adults + children,
        ))
        dates = [check_in + timedelta(days=offset) for offset in range((check_out - check_in).days)]

        current_plan = None
        if data.get("current_rate_plan_id"):
            current_plan = RatePlan.objects.filter(
                id=data["current_rate_plan_id"], room_type__hotel=hotel, is_active=True,
            ).first()
            if not current_plan:
                raise ValidationError({"current_rate_plan_id": "Current rate plan does not belong to this hotel."})
            if current_room and current_plan.room_type_id != current_room.room_type_id:
                raise ValidationError({
                    "current_rate_plan_id": "Current rate plan must belong to the current room type."
                })
        elif current_room:
            current_plan = self._plan_for(current_room.room_type, data["guest_market"])
        current_total = None
        if current_plan:
            current_total = sum(
                (item["price"] for item in self._effective_plan_price(current_plan, dates)),
                Decimal("0"),
            )

        grouped = {}
        for room in rooms:
            plan = self._plan_for(room.room_type, data["guest_market"])
            if not plan:
                continue
            nightly = self._effective_plan_price(plan, dates)
            total = sum((item["price"] for item in nightly), Decimal("0"))
            same_type = bool(current_room and room.room_type_id == current_room.room_type_id)
            same_price = current_total is not None and total == current_total
            priority = 0 if same_type and same_price else 1
            key = (priority, room.room_type_id, plan.id)
            group = grouped.setdefault(key, {
                "priority": "same_type_same_price" if priority == 0 else "other",
                "room_type": {
                    "id": room.room_type_id,
                    "core_room_type_id": room.room_type.core_room_type_id,
                    "name": room.room_type.name,
                    "cover_image_url": room.room_type.cover_image_url,
                    "max_adults": room.room_type.max_adults,
                    "max_children": room.room_type.max_children,
                    "max_occupancy": room.room_type.max_occupancy,
                    "breakfast": RoomTypeSerializer().get_breakfast(room.room_type),
                },
                "rate_plan": {
                    "id": plan.id,
                    "name": plan.name,
                    "guest_market": plan.guest_market,
                    "currency": plan.currency,
                },
                "nightly_prices": nightly,
                "total_price": total,
                "rooms": [],
            })
            group["rooms"].append(self._room_details(room))

        groups = list(grouped.values())
        for group in groups:
            group["rooms"].sort(key=lambda room: (
                room["building"], room["floor"], self._natural_room_number(room["room_number"]), room["id"],
            ))
            group["room_count"] = len(group["rooms"])
        groups.sort(key=lambda group: (
            0 if group["priority"] == "same_type_same_price" else 1,
            group["room_type"]["name"].lower(),
            group["total_price"],
        ))
        return success({
            "criteria": {
                "check_in": check_in,
                "check_out": check_out,
                "adults": adults,
                "children": children,
                "guest_market": data["guest_market"],
                "workflow": data["workflow"],
                "current_room_id": data.get("current_room_id"),
                "current_rate_plan_id": current_plan.id if current_plan else None,
                "excluded_selected_room_ids": sorted(selected_ids),
            },
            "total_rooms": sum(group["room_count"] for group in groups),
            "groups": groups,
        })


class OTARoomSelectionView(APIView):
    permission_classes = [HasBookingAdminKey]
    business_scoped = True

    @staticmethod
    def _hotel(request):
        core_business_id = getattr(request, "booking_core_business_id", None)
        if not core_business_id:
            raise ValidationError({"core_business_id": "X-Booking-Business-ID is required."})
        hotel = Hotel.objects.filter(core_business_id=core_business_id, is_active=True).first()
        if not hotel:
            raise NotFound("Hotel is not synced in the booking engine.")
        if hotel.package not in [Hotel.Package.OTA, Hotel.Package.OTA_PMS]:
            raise PermissionDenied("OTA room selection is only available for OTA or OTA + PMS packages.")
        return hotel

    @staticmethod
    def _room_card_details(room):
        snapshot = room.core_snapshot or {}
        room_type_snapshot = room.room_type.core_snapshot or {}
        beds = snapshot.get("beds") or room_type_snapshot.get("beds") or []
        bed_types = [
            bed.get("bed_type")
            for bed in beds
            if isinstance(bed, dict) and bed.get("bed_type")
        ]
        room_views = snapshot.get("room_views") or []
        room_view = (
            snapshot.get("room_view")
            or snapshot.get("view")
            or (room_views[0] if room_views else None)
        )
        if not room_view:
            type_room_views = room_type_snapshot.get("room_views") or []
            room_view = (
                room_type_snapshot.get("room_view")
                or room_type_snapshot.get("view")
                or (type_room_views[0] if type_room_views else None)
            )
            room_views = type_room_views
        if not room_views and room_view:
            room_views = [room_view]
        room_area = snapshot.get("room_area")
        if room_area is None:
            room_area = snapshot.get("size_sqft")
        if room_area is None:
            room_area = room_type_snapshot.get("room_area") or room_type_snapshot.get("size_sqft")
        area_unit = snapshot.get("area_unit") or room_type_snapshot.get("area_unit")
        room_standard = snapshot.get("room_standard") or room_type_snapshot.get("room_standard")
        return {
            "room_type": {
                "id": room.room_type_id,
                "core_room_type_id": room.room_type.core_room_type_id,
                "name": room.room_type.name,
            },
            "room_type_name": room.room_type.name,
            "room_standard": room_standard,
            "room_standard_id": (
                room_standard.get("id") if isinstance(room_standard, dict) else None
            ),
            "beds": beds,
            "bed_type": snapshot.get("bed_type") or (bed_types[0] if bed_types else None),
            "bed_types": bed_types,
            "room_view": room_view,
            "room_views": room_views,
            "room_area": room_area,
            "area_unit": area_unit,
            "size_sqft": (
                snapshot.get("size_sqft")
                or room_type_snapshot.get("size_sqft")
                or (room_area if area_unit == "sqft" else None)
            ),
            "room_area_text": (
                f"{room_area} {area_unit}"
                if room_area is not None and area_unit
                else None
            ),
        }

    @staticmethod
    def _payload(hotel, timeline_status="all"):
        today = timezone.localdate()
        rooms = list(PhysicalRoom.objects.filter(
            hotel=hotel, is_active=True,
        ).select_related("room_type").order_by("room_type__name", "building", "floor", "room_number", "id"))
        room_ids = [room.id for room in rooms]
        assignments = list(RoomAssignment.objects.filter(
            physical_room_id__in=room_ids,
            booking_room__booking__source__in=[Booking.Source.OTA, Booking.Source.DIRECT],
        ).select_related(
            "booking_room__booking",
        ).prefetch_related(
            "booking_room__booking__invoices",
        ))
        records_by_room = defaultdict(list)
        active_booking_counts = defaultdict(int)
        for assignment in assignments:
            booking_room = assignment.booking_room
            booking = booking_room.booking
            is_live_status = booking.status in [
                Booking.Status.PENDING_PAYMENT,
                Booking.Status.CONFIRMED,
                Booking.Status.CHECKED_IN,
            ]
            if is_live_status and booking.check_in <= today < booking.check_out:
                record_timeline_status = "active_today"
                color = "blue"
                sort_key = (0, booking.check_in, booking.check_out, str(booking.id))
            elif is_live_status and booking.check_in > today:
                record_timeline_status = "upcoming"
                color = "orange"
                sort_key = (1, booking.check_in, booking.check_out, str(booking.id))
            else:
                record_timeline_status = "past"
                color = "grey"
                # Closest past record first.
                sort_key = (2, -booking.check_out.toordinal(), -booking.check_in.toordinal(), str(booking.id))
            if record_timeline_status in ["active_today", "upcoming"] and assignment.released_at is None:
                active_booking_counts[assignment.physical_room_id] += 1
            invoices = [
                {
                    "id": str(invoice.id),
                    "invoice_number": invoice.invoice_number,
                    "status": invoice.status,
                    "total": invoice.total,
                    "currency": invoice.currency,
                }
                for invoice in booking.invoices.all()
            ]
            records_by_room[assignment.physical_room_id].append((sort_key, {
                "assignment_id": assignment.id,
                "booking_id": str(booking.id),
                "booking_reference": booking.reference,
                "booking_status": booking.status,
                "source": booking.source,
                "timeline_status": record_timeline_status,
                "color": color,
                "check_in": str(booking.check_in),
                "check_out": str(booking.check_out),
                "nights": booking.nights,
                "adults": booking_room.adults,
                "children": booking_room.children,
                "contact_name": booking.contact_name,
                "contact_phone": booking.contact_phone,
                "amount": booking_room.total,
                "currency": booking.currency,
                "invoice_count": len(invoices),
                "invoices": invoices,
                "stay_bill_url": f"/api/v1/admin/bookings/{booking.id}/stay-bill/",
            }))
        for room_id, records in records_by_room.items():
            records_by_room[room_id] = [payload for _key, payload in sorted(records, key=lambda item: item[0])]

        active_booking_counts_from_query = dict(RoomAssignment.objects.filter(
            physical_room__hotel=hotel,
            physical_room__is_active=True,
            released_at__isnull=True,
            booking_room__booking__source__in=[Booking.Source.OTA, Booking.Source.DIRECT],
            booking_room__booking__status__in=[Booking.Status.PENDING_PAYMENT, Booking.Status.CONFIRMED],
            booking_room__booking__check_out__gt=timezone.localdate(),
        ).values("physical_room_id").annotate(total=Count("id")).values_list("physical_room_id", "total"))
        grouped = {}
        for room in rooms:
            group = grouped.setdefault(room.room_type_id, {
                "room_type_id": room.room_type_id,
                "core_room_type_id": room.room_type.core_room_type_id,
                "room_type_name": room.room_type.name,
                "total_rooms": 0,
                "selected_count": 0,
                "rooms": [],
            })
            group["total_rooms"] += 1
            group["selected_count"] += int(room.ota_enabled)
            group.setdefault("open_count", 0)
            group["open_count"] += int(room.ota_enabled and room.ota_sale_open)
            all_ota_records = records_by_room.get(room.id, [])
            record_summary = {
                "all": len(all_ota_records),
                "active_today": sum(item["timeline_status"] == "active_today" for item in all_ota_records),
                "upcoming": sum(item["timeline_status"] == "upcoming" for item in all_ota_records),
                "past": sum(item["timeline_status"] == "past" for item in all_ota_records),
            }
            ota_records = (
                all_ota_records
                if timeline_status == "all"
                else [item for item in all_ota_records if item["timeline_status"] == timeline_status]
            )
            group["rooms"].append({
                "physical_room_id": room.id,
                "core_physical_room_id": room.core_physical_room_id,
                "room_number": room.room_number,
                "building_id": room.core_building_id,
                "building": room.building,
                "floor_id": room.core_floor_id,
                "floor": room.floor,
                **OTARoomSelectionView._room_card_details(room),
                "is_ota_selected": room.ota_enabled,
                "ota_sale_open": room.ota_sale_open,
                "ota_sale_status": (
                    "not_selected" if not room.ota_enabled
                    else "open" if room.ota_sale_open
                    else "closed"
                ),
                "active_ota_bookings": active_booking_counts_from_query.get(room.id, active_booking_counts.get(room.id, 0)),
                "ota_record_count": len(ota_records),
                "ota_record_total": len(all_ota_records),
                "ota_record_summary": record_summary,
                "applied_timeline_status": timeline_status,
                "ota_records": ota_records,
                "history_url": f"/api/v1/admin/physical-rooms/{room.core_physical_room_id or room.id}/history/",
                "sale_status_url": f"/api/v1/admin/ota-rooms/{room.id}/sale-status/",
            })
        return {
            "direct_booking_package": hotel.package,
            "total_rooms": len(rooms),
            "total_ota_rooms": sum(int(room.ota_enabled) for room in rooms),
            "total_open_ota_rooms": sum(int(room.ota_enabled and room.ota_sale_open) for room in rooms),
            "selected_room_ids": [room.id for room in rooms if room.ota_enabled],
            "deselected_room_ids": [room.id for room in rooms if not room.ota_enabled],
            "applied_timeline_status": timeline_status,
            "room_types": list(grouped.values()),
        }

    def get(self, request):
        query = OTARoomTimelineQuerySerializer(data=request.query_params)
        query.is_valid(raise_exception=True)
        return success(self._payload(
            self._hotel(request),
            timeline_status=query.validated_data["timeline_status"],
        ))

    @transaction.atomic
    def put(self, request):
        hotel = self._hotel(request)
        serializer = OTARoomSelectionUpdateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        selected_ids = serializer.validated_data["selected_room_ids"]
        deselected_ids = serializer.validated_data["deselected_room_ids"]
        requested_ids = set(selected_ids + deselected_ids)
        rooms = list(PhysicalRoom.objects.select_for_update().filter(
            hotel=hotel, is_active=True, id__in=requested_ids,
        ).select_related("room_type"))
        if len(rooms) != len(requested_ids):
            found_ids = {room.id for room in rooms}
            raise ValidationError({
                "room_ids": f"Rooms are inactive, missing, or belong to another business: {sorted(requested_ids - found_ids)}."
            })

        affected_room_types = {room.room_type_id: room.room_type for room in rooms}
        selected_set = set(selected_ids)
        deselected_set = set(deselected_ids)
        conflicts = []
        today = timezone.localdate()
        for room_type_id, room_type in affected_room_types.items():
            final_selected_ids = set(PhysicalRoom.objects.filter(
                room_type_id=room_type_id, is_active=True, ota_enabled=True, ota_sale_open=True,
            ).values_list("id", flat=True))
            final_selected_ids.update(
                room.id for room in rooms
                if room.room_type_id == room_type_id
                and room.id in selected_set
                and (room.ota_sale_open or not room.ota_enabled)
            )
            final_selected_ids.difference_update(room.id for room in rooms if room.room_type_id == room_type_id and room.id in deselected_set)
            for inventory in DailyInventory.objects.select_for_update().filter(
                room_type_id=room_type_id, stay_date__gte=today,
            ).order_by("stay_date"):
                blocked = PhysicalRoomBlock.objects.filter(
                    physical_room_id__in=final_selected_ids,
                    is_active=True,
                    start_date__lte=inventory.stay_date,
                    end_date__gte=inventory.stay_date,
                ).values("physical_room_id").distinct().count()
                capacity = max(len(final_selected_ids) - blocked, 0)
                committed = inventory.held_rooms + inventory.reserved_rooms
                if committed > capacity:
                    conflicts.append({
                        "room_type_id": room_type_id,
                        "room_type_name": room_type.name,
                        "date": str(inventory.stay_date),
                        "ota_capacity_after_change": capacity,
                        "committed_rooms": committed,
                    })
        if conflicts:
            raise ValidationError({
                "selection": "OTA room capacity would be lower than existing booking commitments.",
                "conflict_dates": conflicts,
            })

        previous_states = {room.id: room.ota_enabled for room in rooms}
        newly_selected_ids = [room.id for room in rooms if room.id in selected_set and not room.ota_enabled]
        PhysicalRoom.objects.filter(id__in=selected_set).update(ota_enabled=True)
        PhysicalRoom.objects.filter(id__in=newly_selected_ids).update(ota_sale_open=True)
        PhysicalRoom.objects.filter(id__in=deselected_set).update(ota_enabled=False)
        actor_id = _core_user_id(request)
        PhysicalRoomActionHistory.objects.bulk_create([
            PhysicalRoomActionHistory(
                physical_room=room,
                action=PhysicalRoomActionHistory.Action.STATUS_CHANGED,
                actor_type=PhysicalRoomActionHistory.ActorType.HOTEL_ADMIN,
                actor_core_user_id=actor_id,
                note="Added to OTA room pool" if room.id in selected_set else "Removed from OTA room pool",
                metadata={
                    "ota_selection_changed": True,
                    "previous_ota_enabled": previous_states[room.id],
                    "ota_enabled": room.id in selected_set,
                },
            )
            for room in rooms
            if previous_states[room.id] != (room.id in selected_set)
        ])
        for room_type in affected_room_types.values():
            ensure_daily_inventory_for_room_type(room_type)
        return success(self._payload(hotel))


class OTARoomSaleStatusView(APIView):
    permission_classes = [HasBookingAdminKey]
    business_scoped = True

    @transaction.atomic
    def post(self, request, physical_room_id):
        hotel = OTARoomSelectionView._hotel(request)
        serializer = OTARoomSaleStatusSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        room = PhysicalRoom.objects.select_for_update().select_related("room_type").filter(
            id=physical_room_id,
            hotel=hotel,
            is_active=True,
        ).first()
        if not room:
            raise NotFound("Physical room is not available for this business.")
        if not room.ota_enabled:
            raise ValidationError({"physical_room_id": "Select this room for OTA before opening or closing OTA sales."})

        action = serializer.validated_data["action"]
        should_open = action == "open"
        if room.ota_sale_open == should_open:
            return success(OTARoomSelectionView._payload(hotel))

        if not should_open:
            today = timezone.localdate()
            conflicts = RoomAssignment.objects.filter(
                physical_room=room,
                released_at__isnull=True,
                booking_room__booking__source__in=[Booking.Source.OTA, Booking.Source.DIRECT],
                booking_room__booking__status__in=[
                    Booking.Status.PENDING_PAYMENT,
                    Booking.Status.CONFIRMED,
                    Booking.Status.CHECKED_IN,
                ],
                booking_room__booking__check_out__gt=today,
            ).select_related("booking_room__booking").order_by(
                "booking_room__booking__check_in", "booking_room__booking__check_out",
            )
            conflict_bookings = [
                {
                    "booking_id": str(item.booking_room.booking.id),
                    "booking_reference": item.booking_room.booking.reference,
                    "status": item.booking_room.booking.status,
                    "check_in": str(item.booking_room.booking.check_in),
                    "check_out": str(item.booking_room.booking.check_out),
                    "contact_name": item.booking_room.booking.contact_name,
                }
                for item in conflicts
            ]
            if conflict_bookings:
                raise ValidationError({
                    "ota_sale_status": "Room cannot be closed while it has active or upcoming OTA bookings.",
                    "conflict_bookings": conflict_bookings,
                })

            inventory_conflicts = []
            open_room_ids = set(PhysicalRoom.objects.filter(
                room_type=room.room_type,
                is_active=True,
                ota_enabled=True,
                ota_sale_open=True,
            ).exclude(id=room.id).values_list("id", flat=True))
            for inventory in DailyInventory.objects.select_for_update().filter(
                room_type=room.room_type,
                stay_date__gte=today,
            ).order_by("stay_date"):
                blocked = PhysicalRoomBlock.objects.filter(
                    physical_room_id__in=open_room_ids,
                    is_active=True,
                    start_date__lte=inventory.stay_date,
                    end_date__gte=inventory.stay_date,
                ).values("physical_room_id").distinct().count()
                capacity = max(len(open_room_ids) - blocked, 0)
                committed = inventory.held_rooms + inventory.reserved_rooms
                if committed > capacity:
                    inventory_conflicts.append({
                        "date": str(inventory.stay_date),
                        "ota_capacity_after_close": capacity,
                        "committed_rooms": committed,
                    })
            if inventory_conflicts:
                raise ValidationError({
                    "ota_sale_status": "Closing this room would reduce OTA capacity below existing commitments.",
                    "conflict_dates": inventory_conflicts,
                })

        previous = room.ota_sale_open
        room.ota_sale_open = should_open
        room.save(update_fields=["ota_sale_open"])
        _record_room_history(
            room,
            (
                PhysicalRoomActionHistory.Action.OTA_SALE_OPENED
                if should_open else PhysicalRoomActionHistory.Action.OTA_SALE_CLOSED
            ),
            request=request,
            note=serializer.validated_data.get("note", ""),
            metadata={
                "previous_ota_sale_open": previous,
                "ota_sale_open": should_open,
            },
        )
        ensure_daily_inventory_for_room_type(room.room_type)
        return success(OTARoomSelectionView._payload(hotel))


class RoomBoardView(APIView):
    permission_classes = [HasBookingAdminKey]
    business_scoped = True

    def get(self, request):
        serializer = RoomBoardQuerySerializer(data=request.query_params)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        scoped_business_id = getattr(request, "booking_core_business_id", None)
        requested_business_id = data.get("core_business_id")
        if scoped_business_id and requested_business_id and scoped_business_id != requested_business_id:
            raise ValidationError("core_business_id does not match X-Booking-Business-ID.")
        core_business_id = scoped_business_id or requested_business_id
        if not core_business_id:
            raise ValidationError({"core_business_id": "Required when X-Booking-Business-ID is not supplied."})
        hotel = Hotel.objects.filter(core_business_id=core_business_id).first()
        if not hotel:
            raise NotFound("Hotel is not synced in the booking engine.")

        target_date = data.get("date") or timezone.localdate()
        rooms = PhysicalRoom.objects.filter(hotel=hotel, is_active=True).select_related("room_type").prefetch_related(
            Prefetch(
                "room_type__rate_plans",
                queryset=RatePlan.objects.filter(is_active=True).order_by("-is_default", "guest_market", "id"),
                to_attr="room_board_rate_plans",
            )
        )
        if data.get("building_id"):
            rooms = rooms.filter(core_building_id=data["building_id"])
        elif data.get("building"):
            rooms = rooms.filter(building=data["building"])
        if data.get("floor_id"):
            rooms = rooms.filter(core_floor_id=data["floor_id"])
        elif data.get("floor"):
            rooms = rooms.filter(floor=data["floor"])
        rooms = list(rooms.order_by("building", "floor", "room_number", "id"))
        room_ids = [room.id for room in rooms]
        latest_status_events = {}
        for event in PhysicalRoomActionHistory.objects.filter(
            physical_room_id__in=room_ids,
            action__in=[
                PhysicalRoomActionHistory.Action.CHECKED_OUT,
                PhysicalRoomActionHistory.Action.CLEANING_STARTED,
                PhysicalRoomActionHistory.Action.CLEANING_COMPLETED,
                PhysicalRoomActionHistory.Action.OUT_OF_SERVICE_STARTED,
                PhysicalRoomActionHistory.Action.OUT_OF_SERVICE_ENDED,
                PhysicalRoomActionHistory.Action.STATUS_CHANGED,
            ],
        ).order_by("-created_at", "-id"):
            latest_status_events.setdefault((event.physical_room_id, event.action), event)

        active_assignments = RoomAssignment.objects.filter(
            physical_room_id__in=room_ids,
            released_at__isnull=True,
            booking_room__booking__status__in=[Booking.Status.CONFIRMED, Booking.Status.CHECKED_IN],
            booking_room__booking__check_in__lte=target_date,
            booking_room__booking__check_out__gt=target_date,
        ).select_related(
            "physical_room",
            "booking_room__room_type",
            "booking_room__rate_plan",
            "booking_room__meal_plan_link",
            "booking_room__meal_plan_link__meal_plan",
            "booking_room__booking",
        ).prefetch_related(
            "booking_room__nights",
            "booking_room__booking__payments",
            "booking_room__booking__guests__identity_documents",
        ).order_by("assigned_at", "id")
        assignment_by_room = {assignment.physical_room_id: assignment for assignment in active_assignments}
        checked_in_assignments = RoomAssignment.objects.filter(
            physical_room_id__in=room_ids,
            released_at__isnull=True,
            booking_room__booking__status=Booking.Status.CHECKED_IN,
        ).select_related(
            "physical_room", "booking_room__room_type", "booking_room__rate_plan",
            "booking_room__meal_plan_link", "booking_room__meal_plan_link__meal_plan",
            "booking_room__booking",
        ).prefetch_related(
            "booking_room__nights", "booking_room__booking__payments",
            "booking_room__booking__guests__identity_documents",
        ).order_by("assigned_at", "id")
        # An actual active stay has precedence over a date-based reservation.
        for checked_in_assignment in checked_in_assignments:
            assignment_by_room[checked_in_assignment.physical_room_id] = checked_in_assignment

        future_assignments = RoomAssignment.objects.filter(
            physical_room_id__in=room_ids,
            released_at__isnull=True,
            booking_room__booking__status=Booking.Status.CONFIRMED,
            booking_room__booking__check_in__gte=target_date,
        ).select_related(
            "booking_room__rate_plan",
            "booking_room__room_type",
            "booking_room__booking",
        ).prefetch_related(
            "booking_room__nights",
            "booking_room__booking__rooms",
            "booking_room__booking__guests",
            "booking_room__booking__payments",
            "booking_room__booking__invoices",
        ).order_by("booking_room__booking__check_in", "assigned_at", "id")
        next_assignments_by_room = defaultdict(list)
        for future_assignment in future_assignments:
            next_assignments_by_room[future_assignment.physical_room_id].append(future_assignment)

        released_assignments = RoomAssignment.objects.filter(
            physical_room_id__in=room_ids,
            released_at__isnull=False,
            booking_room__booking__status=Booking.Status.CHECKED_OUT,
            booking_room__booking__check_out=target_date,
        ).select_related(
            "booking_room__room_type",
            "booking_room__booking",
        ).order_by("-released_at", "-id")
        last_checkout_assignment_by_room = {}
        for released_assignment in released_assignments:
            last_checkout_assignment_by_room.setdefault(released_assignment.physical_room_id, released_assignment)

        block_by_room = {}
        upcoming_blocks_by_room = defaultdict(list)
        room_blocks = PhysicalRoomBlock.objects.filter(
            physical_room_id__in=room_ids,
            is_active=True,
            end_date__gte=target_date,
        ).order_by("start_date", "end_date", "id")
        for block in room_blocks:
            if block.start_date <= target_date <= block.end_date:
                block_by_room.setdefault(block.physical_room_id, block)
            elif block.start_date > target_date:
                upcoming_blocks_by_room[block.physical_room_id].append(block)

        counts = {status_name: 0 for status_name in ["available", "reserved", "occupied", "cleaning", "out_of_service", "blocked"]}
        floors = {}
        room_rows = []
        for room in rooms:
            assignment = assignment_by_room.get(room.id)
            if room.status == PhysicalRoom.Status.OUT_OF_SERVICE:
                display_status = "out_of_service"
            elif assignment and assignment.booking_room.booking.status == Booking.Status.CHECKED_IN:
                display_status = "occupied"
            elif room.id in block_by_room:
                display_status = "blocked"
            elif room.status == PhysicalRoom.Status.CLEANING:
                display_status = "cleaning"
            elif assignment:
                display_status = "reserved"
            elif room.status == PhysicalRoom.Status.OCCUPIED:
                display_status = "occupied"
            else:
                display_status = "available"
            counts[display_status] += 1
            if display_status == "cleaning":
                status_event = (
                    latest_status_events.get((room.id, PhysicalRoomActionHistory.Action.CLEANING_STARTED))
                    or latest_status_events.get((room.id, PhysicalRoomActionHistory.Action.CHECKED_OUT))
                )
            elif display_status == "out_of_service":
                status_event = latest_status_events.get(
                    (room.id, PhysicalRoomActionHistory.Action.OUT_OF_SERVICE_STARTED)
                )
            else:
                status_event = None
            timeline = self.build_room_timeline(
                display_status=display_status,
                target_date=target_date,
                assignment=assignment,
                next_assignments=next_assignments_by_room.get(room.id, []),
                checkout_assignment=last_checkout_assignment_by_room.get(room.id),
                status_event=status_event,
                current_block=block_by_room.get(room.id),
            )
            floor_key = (
                room.core_building_id or room.building or "Unspecified",
                room.core_floor_id or room.floor or "Unspecified",
            )
            floor_summary = floors.setdefault(floor_key, {
                "building_id": room.core_building_id,
                "building": room.building,
                "floor_id": room.core_floor_id,
                "floor": room.floor,
                "total_rooms": 0,
                "counts": {name: 0 for name in counts},
                "rooms": [],
            })
            floor_summary["total_rooms"] += 1
            floor_summary["counts"][display_status] += 1

            assignment_data = None
            if assignment:
                booking_room = assignment.booking_room
                booking = booking_room.booking
                night = next((item for item in booking_room.nights.all() if item.stay_date == target_date), None)
                if booking.amount_paid <= 0:
                    payment_status = "unpaid"
                elif booking.amount_paid >= booking.grand_total:
                    payment_status = "paid"
                else:
                    payment_status = "partially_paid"
                assignment_data = {
                    "assignment_id": assignment.id,
                    "booking_id": booking.id,
                    "booking_reference": booking.reference,
                    "booking_status": booking.status,
                    "booking_room_id": booking_room.id,
                    "contact_name": booking.contact_name,
                    "contact_phone": booking.contact_phone,
                    "check_in": booking.check_in,
                    "check_out": booking.check_out,
                    "nights": booking.nights,
                    "currency": booking.currency,
                    "unit_price": night.unit_price if night else None,
                    "room_total": booking_room.total,
                    "payment_status": payment_status,
                    "amount_paid": booking.amount_paid,
                    "grand_total": booking.grand_total,
                    "special_request": booking.special_request,
                }
            room_data = {
                "id": room.id,
                "core_physical_room_id": room.core_physical_room_id,
                "booking_id": assignment.booking_room.booking_id if assignment else None,
                "building_id": room.core_building_id,
                "floor_id": room.core_floor_id,
                "room_number": room.room_number,
                "building": room.building,
                "floor": room.floor,
                **self.serialize_physical_room_details(room),
                # "core_snapshot": room.core_snapshot,
                "operational_status": room.status,
                "display_status": display_status,
                "block": PhysicalRoomBlockSerializer(block_by_room[room.id]).data if room.id in block_by_room else None,
                **self.serialize_room_block_state(
                    target_date=target_date,
                    current_block=block_by_room.get(room.id),
                    upcoming_blocks=upcoming_blocks_by_room.get(room.id, []),
                ),
                "status_note": room.note,
                "oos_note": room.note if room.status == PhysicalRoom.Status.OUT_OF_SERVICE else None,
                "timeline": timeline,
                "timeline_text": timeline["text"],
                "next_reservations": self.serialize_next_reservations(
                    next_assignments_by_room.get(room.id, [])
                ),
                "room_type": self.serialize_room_board_room_type(room.room_type),
                "assignment": assignment_data,
                "current_booking": self.serialize_room_board_current_booking(assignment) if assignment else None,
            }
            floor_summary["rooms"].append(room_data)
            room_rows.append(room_data)

        room_type_ids = {room.room_type_id for room in rooms}
        confirmed_rooms = BookingRoom.objects.filter(
            booking__hotel=hotel,
            booking__status=Booking.Status.CONFIRMED,
            booking__check_in__lte=target_date,
            booking__check_out__gt=target_date,
            room_type_id__in=room_type_ids,
        ).select_related("booking", "room_type").prefetch_related(
            Prefetch("assignments", queryset=RoomAssignment.objects.filter(released_at__isnull=True))
        )
        unassigned = []
        for booking_room in confirmed_rooms:
            remaining = booking_room.quantity - len(booking_room.assignments.all())
            if remaining > 0:
                unassigned.append({
                    "booking_id": booking_room.booking_id,
                    "booking_reference": booking_room.booking.reference,
                    "booking_room_id": booking_room.id,
                    "room_type_id": booking_room.room_type_id,
                    "room_type_name": booking_room.room_type.name,
                    "quantity_unassigned": remaining,
                    "check_in": booking_room.booking.check_in,
                    "check_out": booking_room.booking.check_out,
                    "contact_name": booking_room.booking.contact_name,
                    "preference_snapshot": booking_room.preference_snapshot,
                })

        return success({
            "date": target_date,
            "hotel": PublicHotelSerializer(hotel).data,
            "summary": {
                "buildings": len({room.core_building_id or room.building or "Unspecified" for room in rooms}),
                "floors": len(floors),
                "total_rooms": len(rooms),
                **counts,
                "unassigned_bookings": sum(item["quantity_unassigned"] for item in unassigned),
            },
            "floors": list(floors.values()),
            "rooms": room_rows,
            "unassigned": unassigned,
        })

    def serialize_physical_room_details(self, room):
        snapshot = room.core_snapshot or {}
        room_type_snapshot = snapshot.get("room_type") or {}
        beds = snapshot.get("beds") or []
        bed_types = [
            bed.get("bed_type")
            for bed in beds
            if isinstance(bed, dict) and bed.get("bed_type")
        ]
        room_view = snapshot.get("room_view") or snapshot.get("view")
        room_views = snapshot.get("room_views") or ([room_view] if room_view else [])
        bath_type = snapshot.get("bath_type")
        bath_types = snapshot.get("bath_types") or ([bath_type] if bath_type else [])
        room_standard = snapshot.get("room_standard") or room_type_snapshot.get("room_standard")
        extra_bed_quantity = (
            snapshot.get("extra_bed_quantity")
            if snapshot.get("extra_bed_quantity") is not None
            else snapshot.get("extra_bed_count", 0)
        )
        return {
            "room_standard": room_standard,
            "room_standard_id": (
                room_standard.get("id") if isinstance(room_standard, dict) else None
            ),
            "room_view": room_view,
            "view": room_view,
            "room_views": room_views,
            "bath_type": bath_type,
            "bath_types": bath_types,
            "beds": beds,
            "bed_type": snapshot.get("bed_type") or (bed_types[0] if bed_types else None),
            "bed_types": bed_types,
            "room_area": snapshot.get("room_area"),
            "area_unit": snapshot.get("area_unit"),
            "size_sqft": snapshot.get("size_sqft") or (
                snapshot.get("room_area") if snapshot.get("area_unit") == "sqft" else None
            ),
            "extra_bed_available": bool(snapshot.get("extra_bed_available", False)),
            "extra_bed_quantity": int(extra_bed_quantity or 0),
        }

    def serialize_room_block_state(self, *, target_date, current_block=None, upcoming_blocks=None):
        upcoming_blocks = upcoming_blocks or []
        upcoming_block = upcoming_blocks[0] if upcoming_blocks else None
        current_data = PhysicalRoomBlockSerializer(current_block).data if current_block else None
        upcoming_data = PhysicalRoomBlockSerializer(upcoming_block).data if upcoming_block else None
        upcoming_list = PhysicalRoomBlockSerializer(upcoming_blocks, many=True).data
        if current_block:
            return {
                "block_status": "currently_blocked",
                "current_block": current_data,
                "upcoming_block": upcoming_data,
                "upcoming_blocks": upcoming_list,
                "block_timeline": {
                    "text": (
                        f"Blocked: {current_block.start_date.isoformat()} "
                        f"to {current_block.end_date.isoformat()}"
                    ),
                    "start_date": current_block.start_date,
                    "end_date": current_block.end_date,
                    "days_until_block": 0,
                    "blocked_days": (current_block.end_date - current_block.start_date).days + 1,
                },
            }
        if upcoming_block:
            days_until_block = (upcoming_block.start_date - target_date).days
            return {
                "block_status": "upcoming_block",
                "current_block": None,
                "upcoming_block": upcoming_data,
                "upcoming_blocks": upcoming_list,
                "block_timeline": {
                    "text": (
                        f"Block starts {upcoming_block.start_date.isoformat()} "
                        f"and ends {upcoming_block.end_date.isoformat()}"
                    ),
                    "start_date": upcoming_block.start_date,
                    "end_date": upcoming_block.end_date,
                    "days_until_block": days_until_block,
                    "blocked_days": (upcoming_block.end_date - upcoming_block.start_date).days + 1,
                },
            }
        return {
            "block_status": "none",
            "current_block": None,
            "upcoming_block": None,
            "upcoming_blocks": [],
            "block_timeline": None,
        }

    def serialize_next_reservations(self, assignments):
        reservations = []
        seen_booking_ids = set()
        for assignment in assignments:
            booking = assignment.booking_room.booking
            if booking.id in seen_booking_ids:
                continue
            seen_booking_ids.add(booking.id)
            booking_room = assignment.booking_room
            guests = list(booking.guests.all())
            primary_guest = next((guest for guest in guests if guest.is_primary), guests[0] if guests else None)
            booking_rooms = list(booking.rooms.all())
            amount_due = max(booking.grand_total - booking.amount_paid, Decimal("0"))
            first_night = next(iter(booking_room.nights.all()), None)
            reservations.append({
                "assignment_id": assignment.id,
                "booking_id": booking.id,
                "booking_reference": booking.reference,
                "booking_status": booking.status,
                "contact_name": booking.contact_name,
                "contact_phone": booking.contact_phone,
                "guest_name": primary_guest.name if primary_guest else booking.contact_name,
                "guest_phone": primary_guest.phone if primary_guest and primary_guest.phone else booking.contact_phone,
                "primary_guest": {
                    "id": primary_guest.id,
                    "name": primary_guest.name,
                    "phone": primary_guest.phone,
                } if primary_guest else None,
                "check_in": booking.check_in,
                "check_out": booking.check_out,
                "nights": booking.nights,
                "adults": booking_room.adults,
                "children": booking_room.children,
                "extra_beds": booking_room.extra_beds,
                "guest_count": booking_room.adults + booking_room.children,
                "room_quantity": booking_room.quantity,
                "booking_room_id": booking_room.id,
                "room_type_id": booking_room.room_type_id,
                "room_type_name": booking_room.room_type.name,
                "rate_plan_id": booking_room.rate_plan_id,
                "rate_plan_name": booking_room.rate_plan.name,
                "nightly_price": first_night.unit_price if first_night else None,
                "room_total": booking_room.total,
                "currency": booking.currency,
                "grand_total": booking.grand_total,
                "formatted_grand_total": format_money(booking.grand_total, booking.currency),
                "amount_paid": booking.amount_paid,
                "amount_due": amount_due,
                "payment_status": (
                    "paid" if amount_due == 0
                    else "partially_paid" if booking.amount_paid > 0
                    else "unpaid"
                ),
                "invoice_count": len(booking.invoices.all()),
                "receipt_count": len([
                    payment for payment in booking.payments.all()
                    if payment.receipt_number
                ]),
                "total_rooms": sum(room.quantity for room in booking_rooms),
            })
        return reservations

    def build_room_timeline(
        self, *, display_status, target_date, assignment=None,
        next_assignments=None, checkout_assignment=None, status_event=None,
        current_block=None,
    ):
        next_assignments = next_assignments or []
        next_reservations = self.serialize_next_reservations(next_assignments)
        base = {
            "type": display_status,
            "text": "",
            "stay_nights": None,
            "reserved_nights": None,
            "vacant_days": None,
            "next_reserved": None,
            "next_reservations": next_reservations,
            "checkout": None,
        }

        if display_status == "occupied" and assignment:
            booking = assignment.booking_room.booking
            stay_nights = max(booking.nights, 0)
            base.update({
                "text": f"Stay: {stay_nights} {_pluralize_night_label(stay_nights)}",
                "stay_nights": stay_nights,
                "checkout": {
                    "date": booking.check_out,
                    "label": "Check-out",
                },
            })
            return base

        if display_status == "reserved" and assignment:
            booking = assignment.booking_room.booking
            reserved_nights = max(booking.nights, 0)
            base.update({
                "text": f"Reserved: {reserved_nights} {_pluralize_night_label(reserved_nights)}",
                "reserved_nights": reserved_nights,
                "next_reserved": {
                    "booking_id": booking.id,
                    "booking_reference": booking.reference,
                    "check_in": booking.check_in,
                    "check_out": booking.check_out,
                    "nights": reserved_nights,
                },
            })
            return base

        if display_status == "cleaning":
            checkout_booking = checkout_assignment.booking_room.booking if checkout_assignment else None
            checkout_data = None
            if checkout_assignment:
                checkout_data = {
                    "booking_id": checkout_booking.id,
                    "booking_reference": checkout_booking.reference,
                    "check_out": checkout_booking.check_out,
                    "released_at": checkout_assignment.released_at,
                }
            checked_out_at = None
            if status_event:
                checked_out_at = (status_event.metadata or {}).get("checked_out_at")
                checked_out_at = checked_out_at or status_event.created_at.isoformat()
            base.update({
                "text": f"Cleaning | Checked out: {checked_out_at}" if checked_out_at else "Cleaning",
                "checkout": checkout_data,
                "status_since": status_event.created_at if status_event else None,
            })
            return base

        if display_status == "available":
            if next_assignments:
                next_booking = next_assignments[0].booking_room.booking
                vacant_days = max((next_booking.check_in - target_date).days, 0)
                reserved_nights = max(next_booking.nights, 0)
                base.update({
                    "text": f"Vacant: {vacant_days} {_pluralize_day_label(vacant_days)} | Reserved: {reserved_nights} {_pluralize_night_label(reserved_nights)}",
                    "vacant_days": vacant_days,
                    "reserved_nights": reserved_nights,
                    "next_reserved": {
                        "booking_id": next_booking.id,
                        "booking_reference": next_booking.reference,
                        "check_in": next_booking.check_in,
                        "check_out": next_booking.check_out,
                        "nights": reserved_nights,
                    },
                })
            else:
                base["text"] = "Vacant"
            return base

        if display_status == "out_of_service":
            since = status_event.created_at if status_event else None
            base["text"] = f"Out of service since {since.isoformat()}" if since else "Out of service"
            base["status_since"] = since
            return base

        if display_status == "blocked":
            if current_block:
                base.update({
                    "text": (
                        f"Blocked: {current_block.start_date.isoformat()} "
                        f"to {current_block.end_date.isoformat()}"
                    ),
                    "block": {
                        "id": current_block.id,
                        "start_date": current_block.start_date.isoformat(),
                        "end_date": current_block.end_date.isoformat(),
                        "note": current_block.note,
                    },
                })
            else:
                base["text"] = "Blocked"
            return base

        base["text"] = display_status.replace("_", " ").title()
        return base

    def serialize_room_board_room_type(self, room_type):
        rate_plans = list(getattr(room_type, "room_board_rate_plans", None) or room_type.rate_plans.filter(is_active=True).order_by("-is_default", "guest_market", "id"))
        primary_rate_plan = next((rate_plan for rate_plan in rate_plans if rate_plan.is_default), None) or (rate_plans[0] if rate_plans else None)
        breakfast_cache = getattr(self, "_room_board_breakfast_cache", None)
        if breakfast_cache is None:
            breakfast_cache = self._room_board_breakfast_cache = {}
        if room_type.id not in breakfast_cache:
            breakfast_cache[room_type.id] = RoomTypeSerializer().get_breakfast(room_type)
        snapshot = room_type.core_snapshot or {}
        photos = snapshot.get("photos") or []
        beds = snapshot.get("beds") or []
        bed_types = [
            bed.get("bed_type")
            for bed in beds
            if isinstance(bed, dict) and bed.get("bed_type")
        ]
        bed_type = snapshot.get("bed_type") or (bed_types[0] if bed_types else None)

        return {
            "id": room_type.id,
            "core_room_type_id": room_type.core_room_type_id,
            "name": room_type.name,
            "room_type_name": room_type.name,
            "description": room_type.description,
            "cover_image_url": room_type.cover_image_url,
            "photos": photos,
            "beds": beds,
            "bed_type": bed_type,
            "bed_types": bed_types,
            "room_area": snapshot.get("room_area"),
            "room_area_from": snapshot.get("room_area_from"),
            "room_area_to": snapshot.get("room_area_to"),
            "area_unit": snapshot.get("area_unit"),
            "size_sqft": snapshot.get("size_sqft"),
            "max_adults": room_type.max_adults,
            "max_children": room_type.max_children,
            "max_occupancy": room_type.max_occupancy,
            "extra_bed_quantity": (
                snapshot.get("extra_bed_quantity")
                if snapshot.get("extra_bed_quantity") is not None
                else snapshot.get("extra_bed_count", 0)
            ),
            "default_inventory": room_type.default_inventory,
            "booking_enabled": room_type.booking_enabled,
            "breakfast": breakfast_cache[room_type.id],
            "price": self.serialize_room_board_rate_plan(primary_rate_plan),
            "rate_plans": [self.serialize_room_board_rate_plan(rate_plan) for rate_plan in rate_plans],
        }

    def serialize_room_board_current_booking(self, assignment):
        booking_room = assignment.booking_room
        booking = booking_room.booking
        meal_plan_link = booking_room.meal_plan_link
        meal_plan = meal_plan_link.meal_plan if meal_plan_link else None

        if booking.amount_paid <= 0:
            payment_status = "unpaid"
        elif booking.amount_paid >= booking.grand_total:
            payment_status = "paid"
        else:
            payment_status = "partially_paid"
        guests = list(booking.guests.all())
        primary_guest = next((guest for guest in guests if guest.is_primary), guests[0] if guests else None)
        payments = list(booking.payments.all())

        def serialize_guest(guest):
            return {
                "id": guest.id,
                "name": guest.name,
                "phone": guest.phone,
                "email": guest.email,
                "nationality": guest.nationality,
                "nrc_number": guest.nrc_number,
                "passport_number": guest.passport_number,
                "identity_type": guest.identity_type,
                "identity_number": guest.identity_number,
                "is_primary": guest.is_primary,
                "documents": [
                    {
                        "id": document.id,
                        "document_type": document.document_type,
                        "document_number": document.document_number,
                        "file_url": document.file.url if document.file else None,
                        "is_verified": document.is_verified,
                        "verified_at": document.verified_at,
                        "uploaded_at": document.uploaded_at,
                    }
                    for document in guest.identity_documents.all()
                ],
            }

        return {
            "id": booking.id,
            "reference": booking.reference,
            "public_token": booking.public_token,
            "status": booking.status,
            "payment_status": payment_status,
            "check_in": booking.check_in,
            "check_out": booking.check_out,
            "nights": booking.nights,
            "guest_market": booking.guest_market,
            "contact": {
                "name": booking.contact_name,
                "phone": booking.contact_phone,
                "email": booking.contact_email,
            },
            "primary_guest": serialize_guest(primary_guest) if primary_guest else None,
            "guests": [serialize_guest(guest) for guest in guests],
            "guest_count": {
                "adults": booking_room.adults,
                "children": booking_room.children,
                "total": booking_room.adults + booking_room.children,
            },
            "booking_room": {
                "id": booking_room.id,
                "quantity": booking_room.quantity,
                "adults": booking_room.adults,
                "children": booking_room.children,
                "extra_beds": booking_room.extra_beds,
                "preference_snapshot": booking_room.preference_snapshot,
                "room_type": {
                    "id": booking_room.room_type_id,
                    "core_room_type_id": booking_room.room_type.core_room_type_id,
                    "name": booking_room.room_type.name,
                },
                "rate_plan": {
                    "id": booking_room.rate_plan_id,
                    "code": booking_room.rate_plan.code,
                    "name": booking_room.rate_plan.name,
                    "guest_market": booking_room.rate_plan.guest_market,
                },
                "meal_plan": {
                    "id": meal_plan.id,
                    "name": meal_plan.name,
                    "is_included": meal_plan_link.is_included,
                    "is_default": meal_plan_link.is_default,
                } if meal_plan else None,
                "breakfast": booking_room.breakfast_snapshot,
                "breakfast_total": booking_room.breakfast_total,
            },
            "amount": {
                "currency": booking.currency,
                "room_total": booking.room_total,
                "add_on_total": booking.add_on_total,
                "tax_total": booking.tax_total,
                "discount_total": booking.discount_total,
                "grand_total": booking.grand_total,
                "amount_paid": booking.amount_paid,
                "amount_due": max(booking.grand_total - booking.amount_paid, 0),
            },
            "payments": [
                {
                    "id": payment.id,
                    "provider": payment.provider,
                    "status": payment.status,
                    "amount": payment.amount,
                    "refunded_amount": payment.refunded_amount,
                    "paid_at": payment.paid_at,
                }
                for payment in payments
            ],
            "special_request": booking.special_request,
            "assignment_id": assignment.id,
            "assigned_at": assignment.assigned_at,
        }

    def serialize_room_board_rate_plan(self, rate_plan):
        if not rate_plan:
            return None
        return {
            "id": rate_plan.id,
            "code": rate_plan.code,
            "name": rate_plan.name,
            "guest_market": rate_plan.guest_market,
            "currency": rate_plan.currency,
            "base_price": rate_plan.base_price,
            "usd_display_price": rate_plan.usd_display_price,
            "default_price": rate_plan.default_price,
            "extra_bed_base_price": rate_plan.extra_bed_base_price,
            "extra_bed_usd_display_price": rate_plan.extra_bed_usd_display_price,
            "breakfast_included": rate_plan.breakfast_included,
            "refundable": rate_plan.refundable,
            "is_default": rate_plan.is_default,
        }


class IntegrationStatusView(APIView):
    permission_classes = [HasBookingAdminKey]
    business_scoped = True

    def get(self, request):
        scoped_business_id = getattr(request, "booking_core_business_id", None)
        raw_business_id = request.query_params.get("core_business_id")
        try:
            requested_business_id = int(raw_business_id) if raw_business_id else None
        except (TypeError, ValueError):
            raise ValidationError({"core_business_id": "Must be a positive integer."})
        if scoped_business_id and requested_business_id and scoped_business_id != requested_business_id:
            raise ValidationError("core_business_id does not match X-Booking-Business-ID.")
        core_business_id = scoped_business_id or requested_business_id
        if not core_business_id:
            raise ValidationError({"core_business_id": "Required when X-Booking-Business-ID is not supplied."})
        hotel = Hotel.objects.filter(core_business_id=core_business_id).first()
        latest_event = CoreIntegrationEvent.objects.filter(core_business_id=core_business_id).order_by("-processed_at").first()
        if not hotel:
            return success({
                "core_business_id": core_business_id,
                "status": "not_synced",
                "hotel": None,
                "latest_event": None,
            })
        return success({
            "core_business_id": core_business_id,
            "status": "active" if hotel.is_active else "disabled",
            "last_synced_at": hotel.synced_at,
            "hotel": PublicHotelSerializer(hotel).data,
            "counts": {
                "room_types": hotel.room_types.filter(core_active=True).count(),
                "physical_rooms": hotel.physical_rooms.filter(is_active=True).count(),
                "active_rate_plans": RatePlan.objects.filter(room_type__hotel=hotel, is_active=True).count(),
            },
            "latest_event": {
                "event_id": latest_event.event_id,
                "event_type": latest_event.event_type,
                "processed_at": latest_event.processed_at,
            } if latest_event else None,
        })


class CoreSyncView(APIView):
    permission_classes = [HasBookingAdminKey]

    def post(self, request, core_business_id):
        return success(sync_business_from_core(core_business_id))


class CoreEventView(APIView):
    permission_classes = [HasBookingAdminKey]

    def post(self, request):
        serializer = CoreEventSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        # Claim the inbox event before processing it. get_or_create relies on
        # the unique event_id constraint, so concurrent deliveries of the same
        # Core outbox event cannot both pass an exists-then-create race. Keeping
        # the claim and processing in one transaction also means a failed sync
        # rolls the claim back and Core can safely retry the event.
        with transaction.atomic():
            _, claimed = CoreIntegrationEvent.objects.get_or_create(
                event_id=data["event_id"],
                defaults={
                    "event_type": data["event_type"],
                    "core_business_id": data["business_id"],
                    "payload": data.get("payload", {}),
                },
            )
            if not claimed:
                return success([], extra_dict={"duplicate": True})

            if data["event_type"] in ["direct_booking.revoked", "direct_booking.expired"]:
                deprovision_hotel(data["business_id"])
            else:
                sync_business_from_core(data["business_id"])
        return success([])


class BusinessScopedQuerysetMixin:
    business_scoped = True
    business_lookup = None

    def scope_queryset(self, queryset):
        core_business_id = getattr(self.request, "booking_core_business_id", None)
        if core_business_id and self.business_lookup:
            return queryset.filter(**{self.business_lookup: core_business_id})
        return queryset

    def get_queryset(self):
        print(self.scope_queryset(super().get_queryset()))
        return self.scope_queryset(super().get_queryset())


class FormattedResponseMixin:
    """Wrap responses produced by DRF's standard CRUD mixins."""

    @staticmethod
    def _formatted_response(response):
        if isinstance(response.data, dict) and {
            "count", "message", "status_code", "data", "error"
        }.issubset(response.data):
            return response
        formatted = success(
            response.data if response.data is not None else [],
            status_code=response.status_code,
            status=response.status_code,
        )
        for header, value in response.items():
            formatted[header] = value
        return formatted

    def list(self, request, *args, **kwargs):
        return self._formatted_response(super().list(request, *args, **kwargs))

    def retrieve(self, request, *args, **kwargs):
        return self._formatted_response(super().retrieve(request, *args, **kwargs))

    def create(self, request, *args, **kwargs):
        return self._formatted_response(super().create(request, *args, **kwargs))

    def update(self, request, *args, **kwargs):
        return self._formatted_response(super().update(request, *args, **kwargs))

    def partial_update(self, request, *args, **kwargs):
        return self._formatted_response(super().partial_update(request, *args, **kwargs))

    def destroy(self, request, *args, **kwargs):
        return self._formatted_response(super().destroy(request, *args, **kwargs))


class AdminModelViewSet(BusinessScopedQuerysetMixin, FormattedResponseMixin, viewsets.ModelViewSet):
    permission_classes = [HasBookingAdminKey]


class HotelViewSet(BusinessScopedQuerysetMixin, FormattedResponseMixin, mixins.ListModelMixin, mixins.RetrieveModelMixin, mixins.UpdateModelMixin, viewsets.GenericViewSet):
    permission_classes = [HasBookingAdminKey]
    queryset = Hotel.objects.all()
    serializer_class = HotelSerializer
    filterset_fields = ["core_business_id", "is_active"]
    business_lookup = "core_business_id"


class RoomTypeViewSet(AdminModelViewSet):
    queryset = RoomType.objects.select_related("hotel")
    serializer_class = RoomTypeSerializer
    filterset_fields = ["hotel", "core_room_type_id", "booking_enabled", "core_active"]
    http_method_names = ["get", "patch", "head", "options"]
    business_lookup = "hotel__core_business_id"

    def get_queryset(self):
        queryset = super().get_queryset()
        ota_enabled = str(self.request.query_params.get("ota_enabled", "all")).strip().lower()
        ota_only = self.request.query_params.get("ota_only")
        if ota_only is not None:
            ota_only = str(ota_only).strip().lower()
            if ota_only not in ["true", "false"]:
                raise ValidationError({"ota_only": "Use true or false."})
            if ota_only == "true":
                ota_enabled = "true"
        ota_sort = str(self.request.query_params.get("ota_sort", "enabled_first")).strip().lower()
        if ota_enabled not in ["all", "true", "false"]:
            raise ValidationError({"ota_enabled": "Use all, true, or false."})
        if ota_sort not in ["enabled_first", "disabled_first"]:
            raise ValidationError({"ota_sort": "Use enabled_first or disabled_first."})

        room_queryset = PhysicalRoom.objects.filter(is_active=True)
        if ota_enabled != "all":
            filter_enabled = ota_enabled == "true"
            room_queryset = room_queryset.filter(ota_enabled=filter_enabled)
            queryset = queryset.filter(
                physical_rooms__is_active=True,
                physical_rooms__ota_enabled=filter_enabled,
            ).distinct()
        ota_order = "-ota_enabled" if ota_sort == "enabled_first" else "ota_enabled"
        room_queryset = room_queryset.annotate(
            active_ota_bookings=Count(
                "assignments",
                filter=Q(
                    assignments__released_at__isnull=True,
                    assignments__booking_room__booking__source__in=[Booking.Source.OTA, Booking.Source.DIRECT],
                    assignments__booking_room__booking__status__in=[
                        Booking.Status.PENDING_PAYMENT,
                        Booking.Status.CONFIRMED,
                        Booking.Status.CHECKED_IN,
                    ],
                    assignments__booking_room__booking__check_out__gt=timezone.localdate(),
                ),
                distinct=True,
            ),
        ).order_by(ota_order, "building", "floor", "room_number", "id")
        return queryset.prefetch_related(
            "meal_plan_links", "meal_plan_links__meal_plan",
            Prefetch(
                "physical_rooms",
                queryset=room_queryset,
                to_attr="room_type_room_list",
            ),
        )


class MealPlanViewSet(AdminModelViewSet):
    queryset = MealPlan.objects.select_related("hotel")
    serializer_class = MealPlanSerializer
    filterset_fields = ["hotel", "core_meal_plan_id", "availability", "core_active"]
    http_method_names = ["get", "head", "options"]
    business_lookup = "hotel__core_business_id"


class RoomTypeMealPlanViewSet(AdminModelViewSet):
    queryset = RoomTypeMealPlan.objects.select_related("room_type", "room_type__hotel", "meal_plan")
    serializer_class = RoomTypeMealPlanSerializer
    filterset_fields = ["room_type", "meal_plan", "is_included", "is_default", "is_guest_selectable"]
    http_method_names = ["get", "head", "options"]
    business_lookup = "room_type__hotel__core_business_id"


class PhysicalRoomViewSet(AdminModelViewSet):
    queryset = PhysicalRoom.objects.select_related("hotel", "room_type")
    serializer_class = PhysicalRoomSerializer
    filterset_fields = [
        "hotel", "room_type", "floor", "building", "core_building_id", "core_floor_id", "status", "is_active",
    ]
    http_method_names = ["get", "patch", "head", "options"]
    business_lookup = "hotel__core_business_id"

    def perform_update(self, serializer):
        previous_status = serializer.instance.status
        previous_note = serializer.instance.note
        room = serializer.save()
        if room.status != previous_status:
            from booking.services import ensure_daily_inventory_for_room_type
            ensure_daily_inventory_for_room_type(room.room_type)
            if room.status == PhysicalRoom.Status.OUT_OF_SERVICE:
                action_name = PhysicalRoomActionHistory.Action.OUT_OF_SERVICE_STARTED
            elif previous_status == PhysicalRoom.Status.OUT_OF_SERVICE:
                action_name = PhysicalRoomActionHistory.Action.OUT_OF_SERVICE_ENDED
            elif previous_status == PhysicalRoom.Status.CLEANING and room.status == PhysicalRoom.Status.VACANT:
                action_name = PhysicalRoomActionHistory.Action.CLEANING_COMPLETED
            else:
                action_name = PhysicalRoomActionHistory.Action.STATUS_CHANGED
            _record_room_history(
                room,
                action_name,
                request=self.request,
                old_status=previous_status,
                new_status=room.status,
                note=room.note or previous_note,
            )
        elif room.note != previous_note:
            _record_room_history(
                room,
                PhysicalRoomActionHistory.Action.STATUS_CHANGED,
                request=self.request,
                old_status=room.status,
                new_status=room.status,
                note=room.note,
                metadata={"note_changed": True, "previous_note": previous_note},
            )

    @action(detail=True, methods=["get"], url_path="history")
    def history(self, request, pk=None):
        room = self.filter_queryset(self.get_queryset()).filter(
            core_physical_room_id=pk,
        ).first()
        if room is None:
            raise NotFound("Physical room was not found for the supplied core physical room ID.")
        queryset = room.action_history.select_related("booking", "block").prefetch_related(
            "booking__payments",
        )
        include_system_events = str(
            request.query_params.get("include_system_events", "")
        ).strip().lower() in {"1", "true", "yes"}
        if not include_system_events:
            queryset = queryset.exclude(action__in=[
                PhysicalRoomActionHistory.Action.CLEANING_STARTED,
                PhysicalRoomActionHistory.Action.STATUS_CHANGED,
            ])
        date_from = parse_date(request.query_params.get("date_from", ""))
        date_to = parse_date(request.query_params.get("date_to", ""))
        if request.query_params.get("date_from") and not date_from:
            raise ValidationError({"date_from": "Use YYYY-MM-DD format."})
        if request.query_params.get("date_to") and not date_to:
            raise ValidationError({"date_to": "Use YYYY-MM-DD format."})
        if date_from:
            queryset = queryset.filter(created_at__date__gte=date_from)
        if date_to:
            queryset = queryset.filter(created_at__date__lte=date_to)
        if date_from and date_to and date_to < date_from:
            raise ValidationError({"date_to": "Must be on or after date_from."})
        return success(PhysicalRoomActionHistorySerializer(queryset, many=True).data)

    def retrieve(self, request, *args, **kwargs):
        room = self.get_object()
        query = RoomBoardQuerySerializer(data=request.query_params)
        query.is_valid(raise_exception=True)
        target_date = query.validated_data.get("date") or timezone.localdate()
        assignment = RoomAssignment.objects.filter(
            physical_room=room,
            released_at__isnull=True,
            booking_room__booking__status__in=[Booking.Status.CONFIRMED, Booking.Status.CHECKED_IN],
            booking_room__booking__check_in__lte=target_date,
            booking_room__booking__check_out__gt=target_date,
        ).select_related(
            "booking_room__room_type",
            "booking_room__rate_plan",
            "booking_room__meal_plan_link",
            "booking_room__meal_plan_link__meal_plan",
            "booking_room__booking",
        ).prefetch_related(
            "booking_room__nights",
            "booking_room__booking__payments",
            "booking_room__booking__guests__identity_documents",
        ).order_by("assigned_at", "id").first()
        checked_in_assignment = RoomAssignment.objects.filter(
            physical_room=room,
            released_at__isnull=True,
            booking_room__booking__status=Booking.Status.CHECKED_IN,
        ).select_related(
            "booking_room__room_type", "booking_room__rate_plan",
            "booking_room__meal_plan_link", "booking_room__meal_plan_link__meal_plan",
            "booking_room__booking",
        ).prefetch_related(
            "booking_room__nights", "booking_room__booking__payments",
            "booking_room__booking__guests__identity_documents",
        ).order_by("assigned_at", "id").first()
        if checked_in_assignment:
            assignment = checked_in_assignment

        next_assignments = list(RoomAssignment.objects.filter(
            physical_room=room,
            released_at__isnull=True,
            booking_room__booking__status=Booking.Status.CONFIRMED,
            booking_room__booking__check_in__gte=target_date,
        ).select_related(
            "booking_room__room_type", "booking_room__rate_plan", "booking_room__booking",
        ).prefetch_related(
            "booking_room__nights",
            "booking_room__booking__rooms",
            "booking_room__booking__guests",
            "booking_room__booking__payments",
            "booking_room__booking__invoices",
        ).order_by(
            "booking_room__booking__check_in", "assigned_at", "id",
        ))

        active_block = PhysicalRoomBlock.objects.filter(
            physical_room=room,
            is_active=True,
            start_date__lte=target_date,
            end_date__gte=target_date,
        ).order_by("start_date", "id").first()
        upcoming_blocks = list(PhysicalRoomBlock.objects.filter(
            physical_room=room,
            is_active=True,
            start_date__gt=target_date,
        ).order_by("start_date", "end_date", "id"))

        if room.status == PhysicalRoom.Status.OUT_OF_SERVICE:
            display_status = "out_of_service"
        elif assignment and assignment.booking_room.booking.status == Booking.Status.CHECKED_IN:
            display_status = "occupied"
        elif active_block:
            display_status = "blocked"
        elif room.status == PhysicalRoom.Status.CLEANING:
            display_status = "cleaning"
        elif assignment:
            display_status = "reserved"
        elif room.status == PhysicalRoom.Status.OCCUPIED:
            display_status = "occupied"
        else:
            display_status = "available"

        board = RoomBoardView()
        data = PhysicalRoomSerializer(room, context={"request": request}).data
        data.update({
            "date": target_date,
            **board.serialize_physical_room_details(room),
            "display_status": display_status,
            "status_note": room.note,
            "oos_note": room.note if room.status == PhysicalRoom.Status.OUT_OF_SERVICE else None,
            "block": PhysicalRoomBlockSerializer(active_block).data if active_block else None,
            **board.serialize_room_block_state(
                target_date=target_date,
                current_block=active_block,
                upcoming_blocks=upcoming_blocks,
            ),
            "room_type": board.serialize_room_board_room_type(room.room_type),
            "next_reservations": board.serialize_next_reservations(next_assignments),
            "current_booking": board.serialize_room_board_current_booking(assignment) if assignment else None,
        })
        return success(data)


class PhysicalRoomBlockViewSet(AdminModelViewSet):
    queryset = PhysicalRoomBlock.objects.select_related("physical_room", "physical_room__hotel", "physical_room__room_type")
    serializer_class = PhysicalRoomBlockSerializer
    filterset_fields = ["physical_room", "start_date", "end_date", "is_active"]
    business_lookup = "physical_room__hotel__core_business_id"

    def _reconcile_inventory(self, room):
        from booking.services import ensure_daily_inventory_for_room_type
        ensure_daily_inventory_for_room_type(room.room_type)

    def perform_create(self, serializer):
        block = serializer.save()
        _record_room_history(
            block.physical_room,
            PhysicalRoomActionHistory.Action.BLOCK_CREATED,
            request=self.request,
            block=block,
            note=block.note,
            metadata={"start_date": str(block.start_date), "end_date": str(block.end_date)},
        )
        self._reconcile_inventory(block.physical_room)

    def perform_update(self, serializer):
        old_room = serializer.instance.physical_room
        block = serializer.save()
        _record_room_history(
            block.physical_room,
            PhysicalRoomActionHistory.Action.BLOCK_UPDATED,
            request=self.request,
            block=block,
            note=block.note,
            metadata={"start_date": str(block.start_date), "end_date": str(block.end_date)},
        )
        self._reconcile_inventory(old_room)
        if block.physical_room_id != old_room.id:
            self._reconcile_inventory(block.physical_room)

    def perform_destroy(self, instance):
        room = instance.physical_room
        _record_room_history(
            room,
            PhysicalRoomActionHistory.Action.UNBLOCKED,
            request=self.request,
            block=instance,
            note=instance.note,
            metadata={"start_date": str(instance.start_date), "end_date": str(instance.end_date), "deleted": True},
        )
        instance.delete()
        self._reconcile_inventory(room)

    @action(detail=True, methods=["post"], url_path="unblock")
    @transaction.atomic
    def unblock(self, request, pk=None):
        block = PhysicalRoomBlock.objects.select_for_update().select_related(
            "physical_room",
            "physical_room__hotel",
            "physical_room__room_type",
        ).get(pk=self.get_object().pk)
        if block.is_active:
            block.is_active = False
            block.save(update_fields=["is_active", "updated_at"])
            _record_room_history(
                block.physical_room,
                PhysicalRoomActionHistory.Action.UNBLOCKED,
                request=request,
                block=block,
                note=block.note,
                metadata={"start_date": str(block.start_date), "end_date": str(block.end_date)},
            )
            self._reconcile_inventory(block.physical_room)
        return success(PhysicalRoomBlockSerializer(block, context={"request": request}).data)


class RatePlanViewSet(AdminModelViewSet):
    queryset = RatePlan.objects.select_related("room_type", "room_type__hotel")
    serializer_class = RatePlanSerializer
    filterset_fields = ["room_type", "guest_market", "source", "is_default", "is_active"]
    business_lookup = "room_type__hotel__core_business_id"

    def perform_destroy(self, instance):
        if instance.source == RatePlan.Source.CORE:
            raise ValidationError("Core-generated default RatePlans cannot be deleted.")
        instance.is_active = False
        instance.save(update_fields=["is_active"])


class DailyInventoryViewSet(AdminModelViewSet):
    queryset = DailyInventory.objects.select_related("room_type", "room_type__hotel")
    serializer_class = DailyInventorySerializer
    filterset_fields = ["room_type", "stay_date", "stop_sell"]
    business_lookup = "room_type__hotel__core_business_id"

    @action(detail=False, methods=["post"], url_path="bulk-upsert")
    @transaction.atomic
    def bulk_upsert(self, request):
        serializer = DailyInventoryBulkUpsertSerializer(data=request.data, context={"request": request})
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        current_date = data["start_date"]
        created_count = 0
        updated_count = 0
        row_ids = []
        while current_date <= data["end_date"]:
            row, created = DailyInventory.objects.select_for_update().get_or_create(
                room_type=data["room_type"],
                stay_date=current_date,
                defaults={"total_rooms": data["total_rooms"], "stop_sell": data["stop_sell"]},
            )
            if not created:
                committed = row.held_rooms + row.reserved_rooms
                if data["total_rooms"] < committed:
                    raise ValidationError({
                        "total_rooms": f"Cannot set {current_date} below {committed} held/reserved rooms."
                    })
                row.total_rooms = data["total_rooms"]
                row.stop_sell = data["stop_sell"]
                row.save(update_fields=["total_rooms", "stop_sell"])
            row_ids.append(row.id)
            created_count += int(created)
            updated_count += int(not created)
            current_date += timedelta(days=1)
        rows = DailyInventory.objects.filter(id__in=row_ids).order_by("stay_date")
        return success({
            "room_type_id": data["room_type"].id,
            "start_date": data["start_date"],
            "end_date": data["end_date"],
            "created": created_count,
            "updated": updated_count,
            "inventory": DailyInventorySerializer(rows, many=True).data,
        })


class DailyRateViewSet(AdminModelViewSet):
    queryset = DailyRate.objects.select_related("rate_plan", "rate_plan__room_type")
    serializer_class = DailyRateSerializer
    filterset_fields = ["rate_plan", "stay_date"]
    business_lookup = "rate_plan__room_type__hotel__core_business_id"

    @action(detail=False, methods=["post"], url_path="bulk-upsert")
    @transaction.atomic
    def bulk_upsert(self, request):
        serializer = DailyRateBulkUpsertSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        current_date = data["start_date"]
        created_count = 0
        updated_count = 0
        rate_ids = []
        defaults = {
            "base_price": data["base_price"],
            "usd_display_price": data.get("usd_display_price"),
            "price": data["price"],
            "min_stay": data["min_stay"],
            "closed_to_arrival": data["closed_to_arrival"],
            "closed_to_departure": data["closed_to_departure"],
        }
        while current_date <= data["end_date"]:
            daily_rate, created = DailyRate.objects.update_or_create(
                rate_plan=data["rate_plan"],
                stay_date=current_date,
                defaults=defaults,
            )
            rate_ids.append(daily_rate.id)
            created_count += int(created)
            updated_count += int(not created)
            current_date += timedelta(days=1)

        rates = DailyRate.objects.filter(id__in=rate_ids).order_by("stay_date")
        return success({
            "rate_plan_id": data["rate_plan"].id,
            "start_date": data["start_date"],
            "end_date": data["end_date"],
            "created": created_count,
            "updated": updated_count,
            "rates": DailyRateSerializer(rates, many=True).data,
        })


class RatePeriodViewSet(AdminModelViewSet):
    queryset = RatePeriod.objects.select_related("rate_plan", "rate_plan__room_type", "rate_plan__room_type__hotel")
    serializer_class = RatePeriodSerializer
    filterset_fields = ["rate_plan", "start_date", "end_date", "is_active"]
    business_lookup = "rate_plan__room_type__hotel__core_business_id"

    @transaction.atomic
    def create(self, request, *args, **kwargs):
        rate_plan_id = request.data.get("rate_plan")
        if rate_plan_id:
            RatePlan.objects.select_for_update().filter(id=rate_plan_id).exists()
        return super().create(request, *args, **kwargs)

    @transaction.atomic
    def update(self, request, *args, **kwargs):
        instance = self.get_object()
        rate_plan_id = request.data.get("rate_plan", instance.rate_plan_id)
        RatePlan.objects.select_for_update().filter(id=rate_plan_id).exists()
        return super().update(request, *args, **kwargs)


class AddOnViewSet(AdminModelViewSet):
    queryset = AddOn.objects.select_related("hotel", "template")
    serializer_class = AddOnSerializer
    filterset_fields = ["hotel", "service_type", "template", "pricing_unit", "currency", "is_active"]
    business_lookup = "hotel__core_business_id"


def _core_user_id(request):
    if request.user and request.user.is_authenticated:
        return getattr(request.user, "id", None)
    try:
        value = int(request.headers.get("X-Core-User-ID", ""))
        return value if value > 0 else None
    except (TypeError, ValueError):
        return None


class AddOnTemplateRequestViewSet(BusinessScopedQuerysetMixin, FormattedResponseMixin, mixins.CreateModelMixin, mixins.ListModelMixin, mixins.RetrieveModelMixin, viewsets.GenericViewSet):
    permission_classes = [HasBookingAdminKey]
    serializer_class = AddOnTemplateRequestSerializer
    queryset = AddOnTemplateRequest.objects.select_related("hotel", "approved_template")
    filterset_fields = ["status"]
    business_scoped = True
    business_lookup = "hotel__core_business_id"

    def perform_create(self, serializer):
        core_business_id = getattr(self.request, "booking_core_business_id", None)
        hotel = Hotel.objects.filter(core_business_id=core_business_id, is_active=True).first()
        if not hotel:
            raise NotFound("The subscribed hotel is not synced in the booking engine.")
        serializer.save(hotel=hotel, requested_by_core_user_id=_core_user_id(self.request))


class SuperAdminAddOnTemplateViewSet(FormattedResponseMixin, viewsets.ModelViewSet):
    authentication_classes = [CoreJWTAuthentication]
    permission_classes = [IsCoreSuperAdmin]
    serializer_class = AddOnTemplateSerializer
    queryset = AddOnTemplate.objects.all()
    filterset_fields = ["code", "status", "version"]
    http_method_names = ["get", "post", "patch", "head", "options"]

    @transaction.atomic
    def create(self, request, *args, **kwargs):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        code = serializer.validated_data["code"]
        AddOnTemplate.objects.select_for_update().filter(code=code).exists()
        version = (AddOnTemplate.objects.filter(code=code).aggregate(value=Max("version"))["value"] or 0) + 1
        template = serializer.save(
            version=version,
            status=AddOnTemplate.Status.DRAFT,
            created_by_core_user_id=_core_user_id(request),
        )
        return success(
            self.get_serializer(template).data,
            status_code=status.HTTP_201_CREATED,
            status=status.HTTP_201_CREATED,
        )

    def update(self, request, *args, **kwargs):
        if self.get_object().status != AddOnTemplate.Status.DRAFT:
            raise ValidationError("Only a draft template version can be edited.")
        return super().update(request, *args, **kwargs)

    @action(detail=True, methods=["post"])
    @transaction.atomic
    def publish(self, request, pk=None):
        template = AddOnTemplate.objects.select_for_update().get(pk=self.get_object().pk)
        if template.status == AddOnTemplate.Status.ARCHIVED:
            raise ValidationError("An archived template cannot be published.")
        AddOnTemplate.objects.filter(
            code=template.code,
            status=AddOnTemplate.Status.PUBLISHED,
        ).exclude(pk=template.pk).update(status=AddOnTemplate.Status.ARCHIVED)
        template.status = AddOnTemplate.Status.PUBLISHED
        template.published_at = timezone.now()
        template.save(update_fields=["status", "published_at", "updated_at"])
        return success(self.get_serializer(template).data)

    @action(detail=True, methods=["post"])
    def archive(self, request, pk=None):
        template = self.get_object()
        template.status = AddOnTemplate.Status.ARCHIVED
        template.save(update_fields=["status", "updated_at"])
        return success(self.get_serializer(template).data)

    @action(detail=True, methods=["post"], url_path="new-version")
    @transaction.atomic
    def new_version(self, request, pk=None):
        source = AddOnTemplate.objects.select_for_update().get(pk=self.get_object().pk)
        version = (AddOnTemplate.objects.filter(code=source.code).aggregate(value=Max("version"))["value"] or 0) + 1
        template = AddOnTemplate.objects.create(
            code=source.code,
            version=version,
            name=source.name,
            description=source.description,
            allowed_pricing_units=source.allowed_pricing_units,
            configuration_schema=source.configuration_schema,
            status=AddOnTemplate.Status.DRAFT,
            created_by_core_user_id=_core_user_id(request),
        )
        return success(
            self.get_serializer(template).data,
            status_code=status.HTTP_201_CREATED,
            status=status.HTTP_201_CREATED,
        )


class SuperAdminAddOnTemplateRequestViewSet(FormattedResponseMixin, mixins.ListModelMixin, mixins.RetrieveModelMixin, viewsets.GenericViewSet):
    authentication_classes = [CoreJWTAuthentication]
    permission_classes = [IsCoreSuperAdmin]
    serializer_class = AddOnTemplateRequestSerializer
    queryset = AddOnTemplateRequest.objects.select_related("hotel", "approved_template")
    filterset_fields = ["status", "hotel"]

    @action(detail=True, methods=["post"])
    def reviewing(self, request, pk=None):
        template_request = self.get_object()
        if template_request.status != AddOnTemplateRequest.Status.PENDING:
            raise ValidationError("Only a pending request can move to reviewing.")
        template_request.status = AddOnTemplateRequest.Status.REVIEWING
        template_request.reviewed_by_core_user_id = _core_user_id(request)
        template_request.save(update_fields=["status", "reviewed_by_core_user_id", "updated_at"])
        return success(self.get_serializer(template_request).data)

    @action(detail=True, methods=["post"])
    @transaction.atomic
    def approve(self, request, pk=None):
        template_request = AddOnTemplateRequest.objects.select_for_update().select_related("hotel").get(pk=self.get_object().pk)
        if template_request.status not in [AddOnTemplateRequest.Status.PENDING, AddOnTemplateRequest.Status.REVIEWING]:
            raise ValidationError("Only a pending or reviewing request can be approved.")
        approval = AddOnTemplateApprovalSerializer(data=request.data)
        approval.is_valid(raise_exception=True)
        data = approval.validated_data
        code = data["code"] or slugify(template_request.requested_name)
        allowed_pricing_units = data.get("allowed_pricing_units") or template_request.suggested_pricing_units or [AddOn.PricingUnit.PER_BOOKING]
        configuration_schema = data.get("configuration_schema") or template_request.suggested_schema or {"version": 1, "fields": []}
        template_serializer = AddOnTemplateSerializer(data={
            "code": code,
            "name": data.get("name", template_request.requested_name),
            "description": data.get("description", template_request.description),
            "allowed_pricing_units": allowed_pricing_units,
            "configuration_schema": configuration_schema,
        })
        template_serializer.is_valid(raise_exception=True)
        AddOnTemplate.objects.select_for_update().filter(code=code).exists()
        version = (AddOnTemplate.objects.filter(code=code).aggregate(value=Max("version"))["value"] or 0) + 1
        AddOnTemplate.objects.filter(code=code, status=AddOnTemplate.Status.PUBLISHED).update(status=AddOnTemplate.Status.ARCHIVED)
        template = template_serializer.save(
            version=version,
            status=AddOnTemplate.Status.PUBLISHED,
            created_by_core_user_id=_core_user_id(request),
            published_at=timezone.now(),
        )
        template_request.status = AddOnTemplateRequest.Status.APPROVED
        template_request.reviewed_by_core_user_id = _core_user_id(request)
        template_request.reviewed_at = timezone.now()
        template_request.admin_note = data.get("admin_note", "")
        template_request.approved_template = template
        template_request.save(update_fields=[
            "status", "reviewed_by_core_user_id", "reviewed_at", "admin_note", "approved_template", "updated_at",
        ])
        return success(self.get_serializer(template_request).data)

    @action(detail=True, methods=["post"])
    def reject(self, request, pk=None):
        template_request = self.get_object()
        if template_request.status not in [AddOnTemplateRequest.Status.PENDING, AddOnTemplateRequest.Status.REVIEWING]:
            raise ValidationError("Only a pending or reviewing request can be rejected.")
        rejection = AddOnTemplateRejectionSerializer(data=request.data)
        rejection.is_valid(raise_exception=True)
        template_request.status = AddOnTemplateRequest.Status.REJECTED
        template_request.reviewed_by_core_user_id = _core_user_id(request)
        template_request.reviewed_at = timezone.now()
        template_request.admin_note = rejection.validated_data["admin_note"]
        template_request.save(update_fields=[
            "status", "reviewed_by_core_user_id", "reviewed_at", "admin_note", "updated_at",
        ])
        return success(self.get_serializer(template_request).data)


class BookingViewSet(BusinessScopedQuerysetMixin, FormattedResponseMixin, mixins.ListModelMixin, mixins.RetrieveModelMixin, viewsets.GenericViewSet):
    permission_classes = [HasBookingAdminKey]
    serializer_class = BookingSerializer
    filterset_fields = ["hotel", "status", "check_in", "check_out", "reference"]
    business_scoped = True
    business_lookup = "hotel__core_business_id"

    def get_queryset(self):
        return self.scope_queryset(
            Booking.objects.select_related("hotel").prefetch_related(
                "rooms__nights", "rooms__assignments", "guests__identity_documents", "add_ons", "payments",
                "invoices__lines", "invoices__receipts",
            )
        )

    @action(detail=True, methods=["get"], url_path="stay-bill")
    def stay_bill(self, request, pk=None):
        booking = self.get_object()
        return success({
            "booking_id": str(booking.id),
            "booking_reference": booking.reference,
            "stay_status": booking.status,
            "currency": booking.currency,
            "invoices": InvoiceSerializer(booking.invoices.all(), many=True).data,
        })

    @action(detail=True, methods=["post"], url_path="invoices")
    @transaction.atomic
    def create_booking_invoice(self, request, pk=None):
        booking = Booking.objects.select_for_update().get(pk=self.get_object().pk)
        serializer = InvoiceCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        invoice = create_invoice(
            booking,
            data["invoice_type"],
            data["lines"],
            tax_total=data["tax_total"],
            discount_total=data["discount_total"],
            note=data["note"],
            add_to_booking_total=True,
        )
        return success(
            InvoiceSerializer(invoice).data,
            status_code=status.HTTP_201_CREATED,
            status=status.HTTP_201_CREATED,
        )

    @action(detail=True, methods=["get", "patch"], url_path="check-in-form")
    @transaction.atomic
    def check_in_form(self, request, pk=None):
        booking = Booking.objects.select_for_update().get(pk=self.get_object().pk)
        if booking.status not in [Booking.Status.CONFIRMED, Booking.Status.PENDING_PAYMENT]:
            raise ValidationError("Only pending or confirmed reservations can use the check-in form.")
        if request.method == "PATCH":
            request_data = request.data.dict() if hasattr(request.data, "dict") else request.data.copy()
            nested_guests = {}
            nested_rooms = {}
            nested_add_ons = {}
            nested_payment = {}
            identity_photos = {}
            guest_field_pattern = re.compile(
                r"^guests?\[(\d+)\]\[(?:['\"])?([a-zA-Z_][a-zA-Z0-9_]*)(?:['\"])?\]$"
            )
            room_field_pattern = re.compile(
                r"^rooms?\[(\d+)\]\[(?:['\"])?([a-zA-Z_][a-zA-Z0-9_]*)(?:['\"])?\]$"
            )
            add_on_field_pattern = re.compile(
                r"^add_ons?\[(\d+)\]\[(?:['\"])?([a-zA-Z_][a-zA-Z0-9_]*)(?:['\"])?\]$"
            )
            payment_field_pattern = re.compile(
                r"^payment\[(?:['\"])?([a-zA-Z_][a-zA-Z0-9_]*)(?:['\"])?\]$"
            )
            for key, value in request.data.items():
                match = guest_field_pattern.match(key)
                if match:
                    index = int(match.group(1))
                    field = match.group(2)
                    if field in {"photo", "identity_photo"}:
                        if key in request.FILES:
                            identity_photos[index] = request.FILES[key]
                        continue
                    nested_guests.setdefault(index, {})[field] = value
                    continue
                room_match = room_field_pattern.match(key)
                if room_match:
                    nested_rooms.setdefault(int(room_match.group(1)), {})[room_match.group(2)] = value
                    continue
                add_on_match = add_on_field_pattern.match(key)
                if add_on_match:
                    index = int(add_on_match.group(1))
                    field = add_on_match.group(2)
                    if field == "configuration" and isinstance(value, str):
                        try:
                            value = json.loads(value)
                        except (TypeError, ValueError):
                            raise ValidationError({key: "Must be valid JSON."})
                    nested_add_ons.setdefault(index, {})[field] = value
                    continue
                payment_match = payment_field_pattern.match(key)
                if payment_match:
                    nested_payment[payment_match.group(1)] = value
            if nested_guests:
                indexes = sorted(nested_guests)
                if indexes != list(range(len(indexes))):
                    raise ValidationError({"guests": "Guest indexes must start at 0 and be consecutive."})
                request_data["guests"] = [nested_guests[index] for index in indexes]
            elif isinstance(request_data.get("guests"), str):
                try:
                    request_data["guests"] = json.loads(request_data["guests"])
                except (TypeError, ValueError):
                    raise ValidationError({"guests": "Must be valid JSON when using multipart/form-data."})
            if nested_rooms:
                indexes = sorted(nested_rooms)
                if indexes != list(range(len(indexes))):
                    raise ValidationError({"rooms": "Room indexes must start at 0 and be consecutive."})
                normalized_rooms = []
                for index in indexes:
                    room_data = nested_rooms[index]
                    physical_room_id = room_data.pop("physical_room_id", None)
                    if physical_room_id is not None:
                        physical_room = PhysicalRoom.objects.select_related("room_type").filter(
                            id=physical_room_id,
                            hotel=booking.hotel,
                            is_active=True,
                        ).first()
                        if not physical_room:
                            raise ValidationError({
                                f"rooms[{index}][physical_room_id]": "Selected physical room is unavailable."
                            })
                        room_data["core_room_type_id"] = physical_room.room_type.core_room_type_id
                        room_data["physical_room_ids"] = [physical_room.id]
                        room_data.setdefault("quantity", 1)
                    normalized_rooms.append(room_data)
                request_data["rooms"] = normalized_rooms
            elif isinstance(request_data.get("rooms"), str):
                try:
                    request_data["rooms"] = json.loads(request_data["rooms"])
                except (TypeError, ValueError):
                    raise ValidationError({"rooms": "Must be valid JSON when using multipart/form-data."})
            if nested_add_ons:
                indexes = sorted(nested_add_ons)
                if indexes != list(range(len(indexes))):
                    raise ValidationError({"add_ons": "Add-on indexes must start at 0 and be consecutive."})
                request_data["add_ons"] = [nested_add_ons[index] for index in indexes]
            elif isinstance(request_data.get("add_ons"), str):
                try:
                    request_data["add_ons"] = json.loads(request_data["add_ons"])
                except (TypeError, ValueError):
                    raise ValidationError({"add_ons": "Must be valid JSON when using multipart/form-data."})
            if nested_payment:
                request_data["payment"] = nested_payment
            elif isinstance(request_data.get("payment"), str):
                try:
                    request_data["payment"] = json.loads(request_data["payment"])
                except (TypeError, ValueError):
                    raise ValidationError({"payment": "Must be valid JSON when using multipart/form-data."})
            serializer = CheckInFormUpdateSerializer(
                data=request_data, partial=True, context={"booking": booking},
            )
            serializer.is_valid(raise_exception=True)
            data = serializer.validated_data
            guest_updates = data.pop("guests", [])
            payment_data = data.pop("payment", None)
            data.pop("workflow", None)
            booking = update_reservation_for_check_in(booking, data)
            existing_guests = list(booking.guests.order_by("id"))
            for guest_index, guest_data in enumerate(guest_updates):
                guest_id = guest_data.pop("id", None)
                if guest_data.get("is_primary"):
                    booking.guests.update(is_primary=False)
                if not guest_id and guest_index < len(existing_guests):
                    guest_id = existing_guests[guest_index].id
                if guest_id:
                    guest = booking.guests.filter(id=guest_id).first()
                    if not guest:
                        raise ValidationError({"guests": f"Guest {guest_id} does not belong to this booking."})
                    for field, value in guest_data.items():
                        setattr(guest, field, value)
                    guest.save()
                else:
                    Guest.objects.create(booking=booking, **guest_data)
            if identity_photos:
                current_guests = list(booking.guests.order_by("id"))
                for guest_index, file in identity_photos.items():
                    if guest_index >= len(current_guests):
                        raise ValidationError({f"guest[{guest_index}][photo]": "Guest index does not exist."})
                    guest = current_guests[guest_index]
                    GuestIdentityDocument.objects.update_or_create(
                        guest=guest,
                        document_type=GuestIdentityDocument.DocumentType.IDENTITY_PHOTO,
                        defaults={
                            "document_number": (
                                guest.identity_number or guest.nrc_number or guest.passport_number or ""
                            ),
                            "file": file,
                            "is_verified": False,
                            "verified_at": None,
                            "verified_by_core_user_id": None,
                        },
                    )
            if payment_data:
                payment_data = dict(payment_data)
                if "amount" not in payment_data:
                    payment_data["amount"] = max(booking.grand_total - booking.amount_paid, Decimal("0"))
                if payment_data["amount"] <= 0:
                    raise ValidationError({"payment": "This booking has no outstanding balance."})
                record_payment(booking, payment_data, auto_assign=False)
        booking = self.get_queryset().prefetch_related("guests__identity_documents").get(pk=booking.pk)
        return success({
            "booking": BookingSerializer(booking, context={"request": request}).data,
            "verification": _check_in_readiness(booking),
            "payment_summary": _payment_summary(booking),
        })

    @action(detail=True, methods=["post"], url_path="guest-identity-document")
    @transaction.atomic
    def guest_identity_document(self, request, pk=None):
        booking = self.get_object()
        if booking.status not in [Booking.Status.PENDING_PAYMENT, Booking.Status.CONFIRMED]:
            raise ValidationError("Identity documents can only be updated before check-in.")
        serializer = GuestIdentityDocumentUploadSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        guest = booking.guests.filter(id=data["guest_id"]).first()
        if not guest:
            raise ValidationError({"guest_id": "Guest does not belong to this booking."})
        document, _created = GuestIdentityDocument.objects.update_or_create(
            guest=guest,
            document_type=data["document_type"],
            defaults={
                "document_number": data.get("document_number", ""),
                "file": data["file"],
                "is_verified": False,
                "verified_at": None,
                "verified_by_core_user_id": None,
            },
        )
        return success(
            GuestIdentityDocumentSerializer(document, context={"request": request}).data,
            status_code=status.HTTP_201_CREATED,
            status=status.HTTP_201_CREATED,
        )

    @action(
        detail=True,
        methods=["get"],
        url_path=r"identity-documents/(?P<document_id>[^/.]+)/download",
    )
    def download_identity_document(self, request, pk=None, document_id=None):
        booking = self.get_object()
        document = GuestIdentityDocument.objects.filter(
            id=document_id,
            guest__booking=booking,
        ).first()
        if not document or not document.file:
            raise NotFound("Identity document not found.")
        return redirect(document.file.url)

    @action(detail=True, methods=["post"])
    def payment(self, request, pk=None):
        booking = self.get_object()
        serializer = PaymentCreateSerializer(data=request.data, context={"booking": booking})
        serializer.is_valid(raise_exception=True)
        payment = record_payment(booking, serializer.validated_data)
        return success(
            PaymentSerializer(payment).data,
            status_code=status.HTTP_201_CREATED,
            status=status.HTTP_201_CREATED,
        )

    @action(detail=True, methods=["post"])
    def cancel(self, request, pk=None):
        return success(BookingSerializer(cancel_booking(self.get_object())).data)

    @action(detail=True, methods=["post"])
    def refund(self, request, pk=None):
        serializer = RefundCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        payment = Payment.objects.filter(id=serializer.validated_data["payment_id"], booking=self.get_object()).first()
        if not payment:
            raise NotFound("Payment not found for this booking.")
        payment = refund_payment(payment, serializer.validated_data["amount"], serializer.validated_data.get("provider_reference", ""))
        return success(PaymentSerializer(payment).data)

    @action(detail=True, methods=["get"], url_path="refund-quote")
    def refund_quote(self, request, pk=None):
        return success(calculate_refund_quote(self.get_object()))

    @action(detail=True, methods=["post"], url_path="assign-room")
    @transaction.atomic
    def assign_room(self, request, pk=None):
        serializer = RoomAssignmentCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        booking = Booking.objects.select_for_update().get(pk=self.get_object().pk)
        if booking.status != Booking.Status.CONFIRMED:
            raise ValidationError("Only a confirmed booking can be assigned a physical room.")
        booking_room = booking.rooms.filter(id=serializer.validated_data["booking_room_id"]).first()
        room = PhysicalRoom.objects.select_for_update().filter(id=serializer.validated_data["physical_room_id"], hotel=booking.hotel, is_active=True).first()
        if not booking_room or not room or room.room_type_id != booking_room.room_type_id:
            raise ValidationError("The physical room does not match this booking room.")
        if room.status != PhysicalRoom.Status.VACANT:
            raise ValidationError("Only a vacant physical room can be assigned.")
        validate_assignment_preferences(booking_room, room)
        if booking_room.assignments.filter(released_at__isnull=True).count() >= booking_room.quantity:
            raise ValidationError("All requested rooms have already been assigned.")
        overlapping_statuses = [Booking.Status.PENDING_PAYMENT, Booking.Status.CONFIRMED, Booking.Status.CHECKED_IN]
        overlap = RoomAssignment.objects.filter(
            physical_room=room,
            released_at__isnull=True,
            booking_room__booking__status__in=overlapping_statuses,
            booking_room__booking__check_in__lt=booking.check_out,
            booking_room__booking__check_out__gt=booking.check_in,
        ).exists()
        if overlap:
            raise ValidationError("This physical room is assigned to an overlapping booking.")
        if PhysicalRoomBlock.objects.filter(
            physical_room=room, is_active=True,
            start_date__lt=booking.check_out, end_date__gte=booking.check_in,
        ).exists():
            raise ValidationError("This physical room is blocked for one or more stay dates.")
        assignment = RoomAssignment.objects.create(booking_room=booking_room, physical_room=room)
        _record_room_history(
            room,
            PhysicalRoomActionHistory.Action.ROOM_ASSIGNED,
            request=request,
            booking=booking,
            new_status=room.status,
            note=booking.special_request,
            metadata={"assignment_id": assignment.id},
        )
        return success(
            RoomAssignmentSerializer(assignment).data,
            status_code=status.HTTP_201_CREATED,
            status=status.HTTP_201_CREATED,
        )

    @action(detail=True, methods=["post"], url_path="unassign-room")
    @transaction.atomic
    def unassign_room(self, request, pk=None):
        serializer = RoomUnassignmentSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        booking = self.get_object()
        if booking.status != Booking.Status.CONFIRMED:
            raise ValidationError("Only a confirmed, not-yet-checked-in booking can be unassigned.")
        assignment = RoomAssignment.objects.select_for_update().filter(
            id=serializer.validated_data["assignment_id"],
            booking_room__booking=booking,
            released_at__isnull=True,
        ).first()
        if not assignment:
            raise NotFound("Active room assignment not found for this booking.")
        assignment.released_at = timezone.now()
        assignment.save(update_fields=["released_at"])
        _record_room_history(
            assignment.physical_room,
            PhysicalRoomActionHistory.Action.ROOM_UNASSIGNED,
            request=request,
            booking=booking,
            old_status=assignment.physical_room.status,
            note=booking.special_request,
            metadata={"assignment_id": assignment.id},
        )
        return success(RoomAssignmentSerializer(assignment).data)

    @action(detail=True, methods=["post"], url_path="change-room")
    @transaction.atomic
    def change_room(self, request, pk=None):
        serializer = RoomChangeSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        booking = self.get_object()
        if booking.status not in [Booking.Status.CONFIRMED, Booking.Status.CHECKED_IN]:
            raise ValidationError("Only confirmed or checked-in bookings can change rooms.")
        assignment = RoomAssignment.objects.select_for_update().select_related("physical_room", "booking_room").filter(
            id=serializer.validated_data["assignment_id"],
            booking_room__booking=booking,
            released_at__isnull=True,
        ).first()
        if not assignment:
            raise NotFound("Active room assignment not found for this booking.")
        new_room = PhysicalRoom.objects.select_for_update().filter(
            id=serializer.validated_data["physical_room_id"],
            hotel=booking.hotel,
            room_type_id=assignment.booking_room.room_type_id,
            is_active=True,
            status=PhysicalRoom.Status.VACANT,
        ).first()
        if not new_room:
            raise ValidationError("The new room must be active, vacant, and have the same room type.")
        validate_assignment_preferences(assignment.booking_room, new_room)
        overlap = RoomAssignment.objects.filter(
            physical_room=new_room,
            released_at__isnull=True,
            booking_room__booking__status__in=[Booking.Status.CONFIRMED, Booking.Status.CHECKED_IN],
            booking_room__booking__check_in__lt=booking.check_out,
            booking_room__booking__check_out__gt=booking.check_in,
        ).exists()
        if overlap:
            raise ValidationError("The new room is assigned to an overlapping booking.")
        if PhysicalRoomBlock.objects.filter(
            physical_room=new_room, is_active=True,
            start_date__lt=booking.check_out, end_date__gte=booking.check_in,
        ).exists():
            raise ValidationError("The new room is blocked for one or more stay dates.")
        old_room = assignment.physical_room
        assignment.released_at = timezone.now()
        assignment.save(update_fields=["released_at"])
        new_assignment = RoomAssignment.objects.create(
            booking_room=assignment.booking_room,
            physical_room=new_room,
        )
        if booking.status == Booking.Status.CHECKED_IN:
            old_room.status = PhysicalRoom.Status.CLEANING
            old_room.save(update_fields=["status"])
            new_room.status = PhysicalRoom.Status.OCCUPIED
            new_room.save(update_fields=["status"])
        _record_room_history(
            old_room,
            PhysicalRoomActionHistory.Action.ROOM_CHANGED,
            request=request,
            booking=booking,
            old_status=PhysicalRoom.Status.OCCUPIED if booking.status == Booking.Status.CHECKED_IN else old_room.status,
            new_status=old_room.status,
            note=booking.special_request,
            metadata={"direction": "from", "new_physical_room_id": new_room.id},
        )
        _record_room_history(
            new_room,
            PhysicalRoomActionHistory.Action.ROOM_CHANGED,
            request=request,
            booking=booking,
            old_status=PhysicalRoom.Status.VACANT,
            new_status=new_room.status,
            note=booking.special_request,
            metadata={"direction": "to", "old_physical_room_id": old_room.id, "assignment_id": new_assignment.id},
        )
        return success(RoomAssignmentSerializer(new_assignment).data)

    @action(detail=True, methods=["post"], url_path="check-in")
    @transaction.atomic
    def check_in(self, request, pk=None):
        serializer = CheckInConfirmSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        if not serializer.validated_data["verification_confirmed"]:
            raise ValidationError({"verification_confirmed": "Hotel admin verification is required."})
        booking = Booking.objects.select_for_update().get(pk=self.get_object().pk)
        if booking.status != Booking.Status.CONFIRMED:
            raise ValidationError("Only a confirmed booking can check in.")
        assigned_room_ids = RoomAssignment.objects.filter(
            booking_room__booking=booking,
            released_at__isnull=True,
        ).values_list("physical_room_id", flat=True)
        assigned_rooms = PhysicalRoom.objects.select_for_update().filter(id__in=assigned_room_ids)
        for physical_room in assigned_rooms:
            if physical_room.status != PhysicalRoom.Status.OCCUPIED:
                continue
            has_other_checked_in_stay = RoomAssignment.objects.filter(
                physical_room=physical_room,
                released_at__isnull=True,
                booking_room__booking__status=Booking.Status.CHECKED_IN,
            ).exclude(booking_room__booking=booking).exists()
            if not has_other_checked_in_stay:
                # Repair legacy state: reserved/confirmed rooms must remain vacant
                # until this final check-in transaction succeeds.
                physical_room.status = PhysicalRoom.Status.VACANT
                physical_room.save(update_fields=["status"])
        readiness = _check_in_readiness(booking)
        if not readiness["can_check_in"]:
            raise ValidationError({
                "check_in": "Required guest documents or room assignments are incomplete.",
                "missing_fields": readiness["missing_fields"],
            })
        assignments = RoomAssignment.objects.filter(booking_room__booking=booking, released_at__isnull=True).select_related("physical_room")
        if assignments.count() < sum(booking.rooms.values_list("quantity", flat=True)):
            raise ValidationError("Assign all physical rooms before check-in.")
        if any(assignment.physical_room.status != PhysicalRoom.Status.VACANT for assignment in assignments):
            raise ValidationError("All assigned physical rooms must be vacant before check-in.")
        checked_in_rooms = [assignment.physical_room for assignment in assignments]
        PhysicalRoom.objects.filter(id__in=[room.id for room in checked_in_rooms]).update(status=PhysicalRoom.Status.OCCUPIED)
        booking.status = Booking.Status.CHECKED_IN
        booking.checked_in_at = timezone.now()
        booking.checked_in_by_core_user_id = _core_user_id(request)
        booking.check_in_verification_note = serializer.validated_data.get("verification_note", "")
        GuestIdentityDocument.objects.filter(guest__booking=booking).update(
            is_verified=True,
            verified_at=booking.checked_in_at,
            verified_by_core_user_id=booking.checked_in_by_core_user_id,
        )
        booking.save(update_fields=[
            "status", "checked_in_at", "checked_in_by_core_user_id",
            "check_in_verification_note", "updated_at",
        ])
        for room in checked_in_rooms:
            _record_room_history(
                room,
                PhysicalRoomActionHistory.Action.CHECKED_IN,
                request=request,
                booking=booking,
                old_status=PhysicalRoom.Status.VACANT,
                new_status=PhysicalRoom.Status.OCCUPIED,
                note=booking.check_in_verification_note,
                metadata={"checked_in_at": booking.checked_in_at.isoformat()},
            )
        return success(BookingSerializer(booking).data)


    @action(detail=True, methods=["post"], url_path="check-out")
    @transaction.atomic
    def check_out(self, request, pk=None):
        booking = Booking.objects.select_for_update().get(pk=self.get_object().pk)
        if booking.status != Booking.Status.CHECKED_IN:
            raise ValidationError("Only a checked-in booking can check out.")
        outstanding = [
            {
                "invoice_id": str(invoice.id),
                "invoice_number": invoice.invoice_number,
                "balance": str(invoice.balance),
                "currency": invoice.currency,
            }
            for invoice in booking.invoices.prefetch_related("receipts").exclude(status=Invoice.Status.VOID)
            if invoice.balance > 0
        ]
        if outstanding:
            raise ValidationError({
                "invoices": "Every invoice must be fully paid before checkout.",
                "outstanding_invoices": outstanding,
            })
        # A checked-in booking remains counted in reserved_rooms until checkout.
        # Release that commitment together with its physical-room assignment so
        # the cleaned room can be sold again (including an early checkout).
        release_checked_in_booking_inventory(booking)
        assignments = list(RoomAssignment.objects.filter(
            booking_room__booking=booking, released_at__isnull=True,
        ).select_related("physical_room"))
        checked_out_at = timezone.now()
        PhysicalRoom.objects.filter(id__in=[item.physical_room_id for item in assignments]).update(
            status=PhysicalRoom.Status.CLEANING,
        )
        RoomAssignment.objects.filter(id__in=[item.id for item in assignments]).update(released_at=checked_out_at)
        booking.status = Booking.Status.CHECKED_OUT
        booking.save(update_fields=["status", "updated_at"])
        for assignment in assignments:
            _record_room_history(
                assignment.physical_room,
                PhysicalRoomActionHistory.Action.CHECKED_OUT,
                request=request,
                booking=booking,
                old_status=PhysicalRoom.Status.OCCUPIED,
                new_status=PhysicalRoom.Status.CLEANING,
                note=booking.special_request,
                metadata={"checked_out_at": checked_out_at.isoformat()},
            )
            _record_room_history(
                assignment.physical_room,
                PhysicalRoomActionHistory.Action.CLEANING_STARTED,
                request=request,
                booking=booking,
                old_status=PhysicalRoom.Status.OCCUPIED,
                new_status=PhysicalRoom.Status.CLEANING,
                metadata={"checked_out_at": checked_out_at.isoformat()},
            )
        return success(BookingSerializer(booking).data)


class WalkInBookingView(APIView):
    permission_classes = [HasBookingAdminKey]
    business_scoped = True

    def post(self, request):
        serializer = WalkInBookingCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        booking = create_walk_in_booking(
            serializer.validated_data,
            idempotency_key=request.headers.get("Idempotency-Key"),
            core_business_id=getattr(request, "booking_core_business_id", None),
            check_in_immediately=True,
        )
        _record_booking_room_assignments(
            booking, request, PhysicalRoomActionHistory.Action.CHECKED_IN,
        )
        return success(
            BookingSerializer(booking, context={"request": request}).data,
            status_code=status.HTTP_201_CREATED,
            status=status.HTTP_201_CREATED,
        )


class WalkInBookingV2View(APIView):
    permission_classes = [HasBookingAdminKey]
    business_scoped = True

    @transaction.atomic
    def post(self, request):
        data = request.data.dict() if hasattr(request.data, "dict") else request.data.copy()
        nested_guests = {}
        nested_identity_photos = {}
        nested_payment = {}
        nested_add_ons = {}
        nested_rooms = {}
        guest_field_pattern = re.compile(
            r"^guests?\[(\d+)\]\[(?:['\"])?([a-zA-Z_][a-zA-Z0-9_]*)(?:['\"])?\]$"
        )
        payment_field_pattern = re.compile(
            r"^payment\[(?:['\"])?([a-zA-Z_][a-zA-Z0-9_]*)(?:['\"])?\]$"
        )
        add_on_field_pattern = re.compile(
            r"^add_ons?\[(\d+)\]\[(?:['\"])?([a-zA-Z_][a-zA-Z0-9_]*)(?:['\"])?\]$"
        )
        room_field_pattern = re.compile(
            r"^rooms?\[(\d+)\]\[(?:['\"])?([a-zA-Z_][a-zA-Z0-9_]*)(?:['\"])?\]$"
        )
        room_preference_field_pattern = re.compile(
            r"^rooms?\[(\d+)\]\[(?:['\"])?preferences(?:['\"])?\]"
            r"\[(?:['\"])?([a-zA-Z_][a-zA-Z0-9_]*)(?:['\"])?\]$"
        )
        for key, value in request.data.items():
            match = guest_field_pattern.match(key)
            if match:
                index = int(match.group(1))
                field = match.group(2)
                if field in {"photo", "identity_photo"}:
                    if key in request.FILES:
                        nested_identity_photos[index] = request.FILES[key]
                    continue
                nested_guests.setdefault(index, {})[field] = value
                continue

            payment_match = payment_field_pattern.match(key)
            if payment_match:
                nested_payment[payment_match.group(1)] = value
                continue
            add_on_match = add_on_field_pattern.match(key)
            if add_on_match:
                index = int(add_on_match.group(1))
                field = add_on_match.group(2)
                if field == "configuration" and isinstance(value, str):
                    try:
                        value = json.loads(value)
                    except (TypeError, ValueError):
                        raise ValidationError({key: "Must be valid JSON."})
                nested_add_ons.setdefault(index, {})[field] = value
                continue
            room_preference_match = room_preference_field_pattern.match(key)
            if room_preference_match:
                index = int(room_preference_match.group(1))
                field = room_preference_match.group(2)
                nested_rooms.setdefault(index, {}).setdefault("preferences", {})[field] = value
                continue
            room_match = room_field_pattern.match(key)
            if room_match:
                index = int(room_match.group(1))
                field = room_match.group(2)
                if field == "preferences" and isinstance(value, str):
                    try:
                        value = json.loads(value)
                    except (TypeError, ValueError):
                        raise ValidationError({key: "Must be valid JSON."})
                nested_rooms.setdefault(index, {})[field] = value

        if nested_guests:
            indexes = sorted(nested_guests)
            if indexes != list(range(len(indexes))):
                raise ValidationError({"guests": "Guest indexes must start at 0 and be consecutive."})
            data["guests"] = [nested_guests[index] for index in indexes]
        if nested_payment:
            data["payment"] = nested_payment
        if nested_add_ons:
            indexes = sorted(nested_add_ons)
            if indexes != list(range(len(indexes))):
                raise ValidationError({"add_ons": "Add-on indexes must start at 0 and be consecutive."})
            data["add_ons"] = [nested_add_ons[index] for index in indexes]
        if nested_rooms:
            indexes = sorted(nested_rooms)
            if indexes != list(range(len(indexes))):
                raise ValidationError({"rooms": "Room indexes must start at 0 and be consecutive."})
            data["rooms"] = [nested_rooms[index] for index in indexes]

        for field in ("guests", "payment", "preferences", "add_ons", "rooms"):
            raw_value = data.get(field)
            if isinstance(raw_value, str):
                try:
                    data[field] = json.loads(raw_value)
                except (TypeError, ValueError):
                    raise ValidationError({field: "Must be valid JSON when using multipart/form-data."})

        serializer = WalkInBookingCreateSerializer(data=data)
        serializer.is_valid(raise_exception=True)
        guests_data = serializer.validated_data["guests"]
        identity_photos = []
        for index, guest_data in enumerate(guests_data):
            file = (
                nested_identity_photos.get(index)
                or request.FILES.get(f"guest_identity_photo_{index}")
                or request.FILES.get(f"guests[{index}][identity_photo]")
            )
            if file:
                identity_photos.append((index, file, guest_data))

        booking = create_walk_in_booking(
            serializer.validated_data,
            idempotency_key=request.headers.get("Idempotency-Key"),
            core_business_id=getattr(request, "booking_core_business_id", None),
            check_in_immediately=False,
        )
        if serializer.validated_data["workflow"] == "reservation":
            _record_booking_room_assignments(
                booking, request, PhysicalRoomActionHistory.Action.RESERVED,
            )
        created_guests = list(booking.guests.order_by("id"))
        for index, file, guest_data in identity_photos:
            if index >= len(created_guests):
                raise ValidationError({f"guest_identity_photo_{index}": "Guest index does not exist."})
            GuestIdentityDocument.objects.create(
                guest=created_guests[index],
                document_type=GuestIdentityDocument.DocumentType.IDENTITY_PHOTO,
                document_number=(
                    guest_data.get("identity_number")
                    or guest_data.get("nrc_number")
                    or guest_data.get("passport_number")
                    or ""
                ),
                file=file,
            )
        return success(
            {
                "booking": BookingSerializer(booking, context={"request": request}).data,
                "verification": _check_in_readiness(booking),
                "payment_summary": _payment_summary(booking),
                "next_action": {
                    "type": "complete_check_in",
                    "url": f"/api/v1/admin/bookings/{booking.id}/check-in-form/",
                },
            },
            status_code=status.HTTP_201_CREATED,
            status=status.HTTP_201_CREATED,
        )


class AdminReservationView(APIView):
    permission_classes = [HasBookingAdminKey]
    business_scoped = True

    def post(self, request):
        serializer = AdminReservationCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        booking = create_admin_reservation(
            serializer.validated_data,
            idempotency_key=request.headers.get("Idempotency-Key"),
            core_business_id=getattr(request, "booking_core_business_id", None),
        )
        _record_booking_room_assignments(
            booking, request, PhysicalRoomActionHistory.Action.RESERVED,
        )
        return success(
            {
                "booking": BookingSerializer(booking, context={"request": request}).data,
                "verification": _check_in_readiness(booking),
                "payment_summary": _payment_summary(booking),
                "next_action": {
                    "type": "complete_check_in",
                    "url": f"/api/v1/admin/bookings/{booking.id}/check-in-form/",
                },
            },
            status_code=status.HTTP_201_CREATED,
            status=status.HTTP_201_CREATED,
        )
