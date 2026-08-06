from datetime import timedelta

from django.conf import settings
from django.db import transaction
from django.db.models import Max, Prefetch, Q
from django.utils import timezone
from django.utils.dateparse import parse_date
from django.utils.text import slugify
from rest_framework import mixins, status, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import NotFound, ValidationError
from rest_framework.permissions import AllowAny
from rest_framework.views import APIView

from booking.authentication import CoreJWTAuthentication
from booking.integrations.core import CoreClient, sync_business_from_core
from booking.models import AddOn, AddOnTemplate, AddOnTemplateRequest, Booking, BookingRoom, CoreIntegrationEvent, DailyInventory, DailyRate, Hotel, MealPlan, Payment, PhysicalRoom, RatePlan, RatePeriod, RoomAssignment, RoomType, RoomTypeMealPlan
from booking.permissions import HasBookingAdminKey, IsCoreSuperAdmin
from booking.serializers import (
    AddOnSerializer,
    AddOnTemplateApprovalSerializer,
    AddOnTemplateRejectionSerializer,
    AddOnTemplateRequestSerializer,
    AddOnTemplateSerializer,
    AvailabilitySearchQuerySerializer,
    BookingCreateSerializer,
    BookingEstimateSerializer,
    BookingSerializer,
    CorePaymentSuccessSerializer,
    CoreEventSerializer,
    DailyInventorySerializer,
    DailyInventoryBulkUpsertSerializer,
    DailyRateSerializer,
    DailyRateBulkUpsertSerializer,
    HotelSerializer,
    MealPlanSerializer,
    PaymentCreateSerializer,
    PaymentSerializer,
    PhysicalRoomSerializer,
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
from booking.services import availability_for_hotel_with_display, availability_for_hotels, cancel_booking, create_booking, create_walk_in_booking, deprovision_hotel, estimate_booking, record_payment, refund_payment, validate_assignment_preferences
from config.response_formatter import success


def _pluralize_day_label(days):
    return "Day" if days == 1 else "Days"


def _pluralize_night_label(nights):
    return "Night" if nights == 1 else "Nights"


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

    def post(self, request):
        serializer = BookingCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            booking, created = create_booking(serializer.validated_data, request.headers.get("Idempotency-Key"))
        except Hotel.DoesNotExist:
            raise NotFound("Hotel is not available in the booking engine.")
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
        view_mode = data["view"]
        include_flat_rooms = data["include_flat_rooms"]
        include_unassigned = data["include_unassigned"]
        rooms = PhysicalRoom.objects.filter(hotel=hotel, is_active=True).select_related("room_type")
        if view_mode == "detail":
            rooms = rooms.prefetch_related(
                Prefetch(
                    "room_type__rate_plans",
                    queryset=RatePlan.objects.filter(is_active=True, is_default=True).only(
                        "id", "room_type_id", "code", "name", "guest_market", "currency",
                        "base_price", "usd_display_price", "default_price", "is_default",
                    ).order_by("guest_market", "id"),
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

        active_assignments = RoomAssignment.objects.filter(
            physical_room_id__in=room_ids,
            released_at__isnull=True,
            booking_room__booking__status__in=[Booking.Status.CONFIRMED, Booking.Status.CHECKED_IN],
            booking_room__booking__check_in__lte=target_date,
            booking_room__booking__check_out__gt=target_date,
        ).select_related(
            "physical_room",
            "booking_room__booking",
        ).order_by("assigned_at", "id")
        if view_mode == "detail":
            active_assignments = active_assignments.prefetch_related("booking_room__booking__payments")
        assignment_by_room = {assignment.physical_room_id: assignment for assignment in active_assignments}

        next_assignment_by_room = {}
        last_checkout_assignment_by_room = {}
        if view_mode == "detail":
            future_assignments = RoomAssignment.objects.filter(
                physical_room_id__in=room_ids,
                released_at__isnull=True,
                booking_room__booking__status=Booking.Status.CONFIRMED,
                booking_room__booking__check_in__gt=target_date,
            ).select_related("booking_room__booking").order_by(
                "booking_room__booking__check_in", "assigned_at", "id"
            )
            for future_assignment in future_assignments:
                next_assignment_by_room.setdefault(future_assignment.physical_room_id, future_assignment)

            released_assignments = RoomAssignment.objects.filter(
                physical_room_id__in=room_ids,
                released_at__isnull=False,
                booking_room__booking__status=Booking.Status.CHECKED_OUT,
                booking_room__booking__check_out=target_date,
            ).select_related("booking_room__booking").order_by("-released_at", "-id")
            for released_assignment in released_assignments:
                last_checkout_assignment_by_room.setdefault(released_assignment.physical_room_id, released_assignment)

        counts = {status_name: 0 for status_name in ["available", "reserved", "occupied", "cleaning", "out_of_service"]}
        floors = {}
        room_rows = []
        for room in rooms:
            assignment = assignment_by_room.get(room.id)
            if room.status == PhysicalRoom.Status.OUT_OF_SERVICE:
                display_status = "out_of_service"
            elif room.status == PhysicalRoom.Status.CLEANING:
                display_status = "cleaning"
            elif assignment and assignment.booking_room.booking.status == Booking.Status.CHECKED_IN:
                display_status = "occupied"
            elif assignment:
                display_status = "reserved"
            elif room.status == PhysicalRoom.Status.OCCUPIED:
                display_status = "occupied"
            else:
                display_status = "available"
            counts[display_status] += 1
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

            room_data = {
                "id": room.id,
                "core_physical_room_id": room.core_physical_room_id,
                "room_number": room.room_number,
                "operational_status": room.status,
                "display_status": display_status,
            }
            if view_mode == "detail":
                timeline = self.build_room_timeline(
                    display_status=display_status,
                    target_date=target_date,
                    assignment=assignment,
                    next_assignment=next_assignment_by_room.get(room.id),
                    checkout_assignment=last_checkout_assignment_by_room.get(room.id),
                )
                assignment_data = None
                if assignment:
                    booking_room = assignment.booking_room
                    booking = booking_room.booking
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
                        "payment_status": payment_status,
                        "has_special_request": bool(booking.special_request),
                    }
                room_data.update({
                    "timeline": timeline,
                    "timeline_text": timeline["text"],
                    "room_type": self.serialize_room_board_room_type(room.room_type),
                    "assignment": assignment_data,
                })
            floor_summary["rooms"].append(room_data)
            if include_flat_rooms:
                room_rows.append({
                    **room_data,
                    "building_id": room.core_building_id,
                    "building": room.building,
                    "floor_id": room.core_floor_id,
                    "floor": room.floor,
                })

        unassigned = []
        if include_unassigned:
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
                    })

        response_data = {
            "date": target_date,
            "view": view_mode,
            "hotel": {
                "core_business_id": hotel.core_business_id,
                "name": hotel.name,
                "base_currency": hotel.base_currency,
            },
            "summary": {
                "buildings": len({room.core_building_id or room.building or "Unspecified" for room in rooms}),
                "floors": len(floors),
                "total_rooms": len(rooms),
                **counts,
            },
            "floors": list(floors.values()),
        }
        if include_flat_rooms:
            response_data["rooms"] = room_rows
        if include_unassigned:
            response_data["summary"]["unassigned_bookings"] = sum(item["quantity_unassigned"] for item in unassigned)
            response_data["unassigned"] = unassigned
        return success(response_data)

    def build_room_timeline(self, *, display_status, target_date, assignment=None, next_assignment=None, checkout_assignment=None):
        base = {
            "type": display_status,
            "text": "",
            "stay_nights": None,
            "reserved_nights": None,
            "vacant_days": None,
            "next_reserved": None,
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
            base.update({
                "text": "Check-out",
                "checkout": checkout_data,
            })
            return base

        if display_status == "available":
            if next_assignment:
                next_booking = next_assignment.booking_room.booking
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
            base["text"] = "Out of service"
            return base

        base["text"] = display_status.replace("_", " ").title()
        return base

    def serialize_room_board_room_type(self, room_type):
        rate_plans = list(getattr(room_type, "room_board_rate_plans", None) or room_type.rate_plans.filter(is_active=True).order_by("-is_default", "guest_market", "id"))
        primary_rate_plan = (
            next((rate_plan for rate_plan in rate_plans if rate_plan.guest_market == RatePlan.GuestMarket.LOCAL), None)
            or next((rate_plan for rate_plan in rate_plans if rate_plan.guest_market == RatePlan.GuestMarket.ALL), None)
            or (rate_plans[0] if rate_plans else None)
        )

        return {
            "id": room_type.id,
            "core_room_type_id": room_type.core_room_type_id,
            "name": room_type.name,
            "price": self.serialize_room_board_rate_plan(primary_rate_plan),
        }

    def serialize_room_board_rate_plan(self, rate_plan):
        if not rate_plan:
            return None
        return {
            "id": rate_plan.id,
            "currency": rate_plan.currency,
            "base_price": rate_plan.base_price,
            "usd_display_price": rate_plan.usd_display_price,
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
        existing = CoreIntegrationEvent.objects.filter(event_id=data["event_id"]).first()
        if existing:
            return success([], extra_dict={"duplicate": True})

        if data["event_type"] in ["direct_booking.revoked", "direct_booking.expired"]:
            deprovision_hotel(data["business_id"])
        else:
            sync_business_from_core(data["business_id"])
        CoreIntegrationEvent.objects.create(
            event_id=data["event_id"],
            event_type=data["event_type"],
            core_business_id=data["business_id"],
            payload=data.get("payload", {}),
        )
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
    queryset = RoomType.objects.select_related("hotel").prefetch_related("meal_plan_links", "meal_plan_links__meal_plan")
    serializer_class = RoomTypeSerializer
    filterset_fields = ["hotel", "core_room_type_id", "booking_enabled", "core_active"]
    http_method_names = ["get", "patch", "head", "options"]
    business_lookup = "hotel__core_business_id"


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
            Booking.objects.select_related("hotel").prefetch_related("rooms__nights", "rooms__assignments", "guests", "add_ons", "payments")
        )

    @action(detail=True, methods=["post"])
    def payment(self, request, pk=None):
        serializer = PaymentCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        payment = record_payment(self.get_object(), serializer.validated_data)
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
        assignment = RoomAssignment.objects.create(booking_room=booking_room, physical_room=room)
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
        return success(RoomAssignmentSerializer(new_assignment).data)

    @action(detail=True, methods=["post"], url_path="check-in")
    @transaction.atomic
    def check_in(self, request, pk=None):
        booking = Booking.objects.select_for_update().get(pk=self.get_object().pk)
        if booking.status != Booking.Status.CONFIRMED:
            raise ValidationError("Only a confirmed booking can check in.")
        assignments = RoomAssignment.objects.filter(booking_room__booking=booking, released_at__isnull=True).select_related("physical_room")
        if assignments.count() < sum(booking.rooms.values_list("quantity", flat=True)):
            raise ValidationError("Assign all physical rooms before check-in.")
        if any(assignment.physical_room.status != PhysicalRoom.Status.VACANT for assignment in assignments):
            raise ValidationError("All assigned physical rooms must be vacant before check-in.")
        PhysicalRoom.objects.filter(assignments__in=assignments).update(status=PhysicalRoom.Status.OCCUPIED)
        booking.status = Booking.Status.CHECKED_IN
        booking.save(update_fields=["status", "updated_at"])
        return success(BookingSerializer(booking).data)


    @action(detail=True, methods=["post"], url_path="check-out")
    @transaction.atomic
    def check_out(self, request, pk=None):
        booking = Booking.objects.select_for_update().get(pk=self.get_object().pk)
        if booking.status != Booking.Status.CHECKED_IN:
            raise ValidationError("Only a checked-in booking can check out.")
        assignments = RoomAssignment.objects.filter(booking_room__booking=booking, released_at__isnull=True)
        PhysicalRoom.objects.filter(assignments__in=assignments).update(status=PhysicalRoom.Status.CLEANING)
        assignments.update(released_at=timezone.now())
        booking.status = Booking.Status.CHECKED_OUT
        booking.save(update_fields=["status", "updated_at"])
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
        )
        return success(
            BookingSerializer(booking).data,
            status_code=status.HTTP_201_CREATED,
            status=status.HTTP_201_CREATED,
        )
