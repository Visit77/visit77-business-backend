from datetime import timedelta
import json
import re

from django.conf import settings
from django.http import FileResponse
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
from booking.models import AddOn, AddOnTemplate, AddOnTemplateRequest, Booking, BookingRoom, CoreIntegrationEvent, DailyInventory, DailyRate, Guest, GuestIdentityDocument, Hotel, MealPlan, Payment, PhysicalRoom, PhysicalRoomBlock, RatePlan, RatePeriod, RoomAssignment, RoomType, RoomTypeMealPlan
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
    GuestIdentityDocumentSerializer,
    GuestIdentityDocumentUploadSerializer,
    MealPlanSerializer,
    PaymentCreateSerializer,
    PaymentSerializer,
    PhysicalRoomSerializer,
    PhysicalRoomBlockSerializer,
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
from booking.services import availability_for_hotel_with_display, availability_for_hotels, cancel_booking, create_admin_reservation, create_booking, create_walk_in_booking, deprovision_hotel, estimate_booking, record_payment, refund_payment, refund_quote as calculate_refund_quote, validate_assignment_preferences
from config.response_formatter import success


def _pluralize_day_label(days):
    return "Day" if days == 1 else "Days"


def _pluralize_night_label(nights):
    return "Night" if nights == 1 else "Nights"


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
    else:
        document_types = {doc.document_type for doc in primary_guest.identity_documents.all()}
        if GuestIdentityDocument.DocumentType.IDENTITY_PHOTO not in document_types:
            missing.append(f"guests.{primary_guest.id}.identity_photo")

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
        "identity_documents_complete": not any("identity_photo" in item for item in missing),
        "all_rooms_assigned": not unassigned,
        "assigned_rooms_vacant": not non_vacant,
        "payment_status": payment_status,
        "can_check_in": not missing and booking.status == Booking.Status.CONFIRMED,
        "missing_fields": missing,
        "unassigned_booking_rooms": unassigned,
        "non_vacant_room_numbers": non_vacant,
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

        future_assignments = RoomAssignment.objects.filter(
            physical_room_id__in=room_ids,
            released_at__isnull=True,
            booking_room__booking__status=Booking.Status.CONFIRMED,
            booking_room__booking__check_in__gt=target_date,
        ).select_related(
            "booking_room__room_type",
            "booking_room__booking",
        ).order_by("booking_room__booking__check_in", "assigned_at", "id")
        next_assignment_by_room = {}
        for future_assignment in future_assignments:
            next_assignment_by_room.setdefault(future_assignment.physical_room_id, future_assignment)

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

        block_by_room = {
            block.physical_room_id: block
            for block in PhysicalRoomBlock.objects.filter(
                physical_room_id__in=room_ids,
                is_active=True,
                start_date__lte=target_date,
                end_date__gte=target_date,
            ).order_by("start_date", "id")
        }

        counts = {status_name: 0 for status_name in ["available", "reserved", "occupied", "cleaning", "out_of_service", "blocked"]}
        floors = {}
        room_rows = []
        for room in rooms:
            assignment = assignment_by_room.get(room.id)
            if room.status == PhysicalRoom.Status.OUT_OF_SERVICE:
                display_status = "out_of_service"
            elif room.status == PhysicalRoom.Status.CLEANING:
                display_status = "cleaning"
            elif room.id in block_by_room:
                display_status = "blocked"
            elif assignment and assignment.booking_room.booking.status == Booking.Status.CHECKED_IN:
                display_status = "occupied"
            elif assignment:
                display_status = "reserved"
            elif room.status == PhysicalRoom.Status.OCCUPIED:
                display_status = "occupied"
            else:
                display_status = "available"
            counts[display_status] += 1
            timeline = self.build_room_timeline(
                display_status=display_status,
                target_date=target_date,
                assignment=assignment,
                next_assignment=next_assignment_by_room.get(room.id),
                checkout_assignment=last_checkout_assignment_by_room.get(room.id),
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
                # "core_snapshot": room.core_snapshot,
                "operational_status": room.status,
                "display_status": display_status,
                "block": PhysicalRoomBlockSerializer(block_by_room[room.id]).data if room.id in block_by_room else None,
                "status_note": room.note,
                "timeline": timeline,
                "timeline_text": timeline["text"],
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

        if display_status == "blocked":
            base["text"] = "Blocked"
            return base

        base["text"] = display_status.replace("_", " ").title()
        return base

    def serialize_room_board_room_type(self, room_type):
        rate_plans = list(getattr(room_type, "room_board_rate_plans", None) or room_type.rate_plans.filter(is_active=True).order_by("-is_default", "guest_market", "id"))
        primary_rate_plan = next((rate_plan for rate_plan in rate_plans if rate_plan.is_default), None) or (rate_plans[0] if rate_plans else None)

        return {
            "id": room_type.id,
            "core_room_type_id": room_type.core_room_type_id,
            "name": room_type.name,
            "description": room_type.description,
            "cover_image_url": room_type.cover_image_url,
            "max_adults": room_type.max_adults,
            "max_children": room_type.max_children,
            "max_occupancy": room_type.max_occupancy,
            "default_inventory": room_type.default_inventory,
            "booking_enabled": room_type.booking_enabled,
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
                        "file_url": (
                            f"/api/v1/admin/bookings/{booking.id}/"
                            f"identity-documents/{document.id}/download/"
                        ),
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

    def perform_update(self, serializer):
        previous_status = serializer.instance.status
        room = serializer.save()
        if room.status != previous_status:
            from booking.services import ensure_daily_inventory_for_room_type
            ensure_daily_inventory_for_room_type(room.room_type)

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

        active_block = PhysicalRoomBlock.objects.filter(
            physical_room=room,
            is_active=True,
            start_date__lte=target_date,
            end_date__gte=target_date,
        ).order_by("start_date", "id").first()

        if room.status == PhysicalRoom.Status.OUT_OF_SERVICE:
            display_status = "out_of_service"
        elif room.status == PhysicalRoom.Status.CLEANING:
            display_status = "cleaning"
        elif active_block:
            display_status = "blocked"
        elif assignment and assignment.booking_room.booking.status == Booking.Status.CHECKED_IN:
            display_status = "occupied"
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
            "display_status": display_status,
            "block": PhysicalRoomBlockSerializer(active_block).data if active_block else None,
            "room_type": board.serialize_room_board_room_type(room.room_type),
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
        self._reconcile_inventory(block.physical_room)

    def perform_update(self, serializer):
        old_room = serializer.instance.physical_room
        block = serializer.save()
        self._reconcile_inventory(old_room)
        if block.physical_room_id != old_room.id:
            self._reconcile_inventory(block.physical_room)

    def perform_destroy(self, instance):
        room = instance.physical_room
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
                "rooms__nights", "rooms__assignments", "guests__identity_documents", "add_ons", "payments"
            )
        )

    @action(detail=True, methods=["get", "patch"], url_path="check-in-form")
    @transaction.atomic
    def check_in_form(self, request, pk=None):
        booking = Booking.objects.select_for_update().get(pk=self.get_object().pk)
        if booking.status not in [Booking.Status.CONFIRMED, Booking.Status.PENDING_PAYMENT]:
            raise ValidationError("Only pending or confirmed reservations can use the check-in form.")
        if request.method == "PATCH":
            serializer = CheckInFormUpdateSerializer(data=request.data, partial=True)
            serializer.is_valid(raise_exception=True)
            data = serializer.validated_data
            for field in ["contact_name", "contact_phone", "contact_email", "special_request"]:
                if field in data:
                    setattr(booking, field, data[field])
            booking.save(update_fields=[
                field for field in ["contact_name", "contact_phone", "contact_email", "special_request"]
                if field in data
            ] + ["updated_at"])
            for guest_data in data.get("guests", []):
                guest_id = guest_data.pop("id", None)
                if guest_data.get("is_primary"):
                    booking.guests.update(is_primary=False)
                if guest_id:
                    guest = booking.guests.filter(id=guest_id).first()
                    if not guest:
                        raise ValidationError({"guests": f"Guest {guest_id} does not belong to this booking."})
                    for field, value in guest_data.items():
                        setattr(guest, field, value)
                    guest.save()
                else:
                    Guest.objects.create(booking=booking, **guest_data)
        booking = self.get_queryset().prefetch_related("guests__identity_documents").get(pk=booking.pk)
        amount_due = max(booking.grand_total - booking.amount_paid, 0)
        return success({
            "booking": BookingSerializer(booking, context={"request": request}).data,
            "verification": _check_in_readiness(booking),
            "payment_summary": {
                "currency": booking.currency,
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
            },
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
        return FileResponse(
            document.file.open("rb"),
            as_attachment=False,
            filename=document.file.name.rsplit("/", 1)[-1],
        )

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
        PhysicalRoom.objects.filter(assignments__in=assignments).update(status=PhysicalRoom.Status.OCCUPIED)
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
            check_in_immediately=True,
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
        guest_field_pattern = re.compile(
            r"^guests?\[(\d+)\]\[(?:['\"])?([a-zA-Z_][a-zA-Z0-9_]*)(?:['\"])?\]$"
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
                        nested_identity_photos[index] = request.FILES[key]
                    continue
                nested_guests.setdefault(index, {})[field] = value
                continue

            payment_match = payment_field_pattern.match(key)
            if payment_match:
                nested_payment[payment_match.group(1)] = value

        if nested_guests:
            indexes = sorted(nested_guests)
            if indexes != list(range(len(indexes))):
                raise ValidationError({"guests": "Guest indexes must start at 0 and be consecutive."})
            data["guests"] = [nested_guests[index] for index in indexes]
        if nested_payment:
            data["payment"] = nested_payment

        for field in ("guests", "payment", "preferences"):
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
        return success(
            {
                "booking": BookingSerializer(booking, context={"request": request}).data,
                "verification": _check_in_readiness(booking),
                "next_action": {
                    "type": "complete_check_in",
                    "url": f"/api/v1/admin/bookings/{booking.id}/check-in-form/",
                },
            },
            status_code=status.HTTP_201_CREATED,
            status=status.HTTP_201_CREATED,
        )
