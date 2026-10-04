from celery import shared_task
from django.conf import settings

from booking.services import (
    auto_cancel_no_show_reservations,
    ensure_rolling_daily_inventory,
    expire_pending_bookings,
)
import logging
from booking.models import Booking, Hotel, OTABookingNotification
from booking.booking_services.email import send_booking_confirmation_email
from booking.booking_services.sms import (
    PermanentSMSDeliveryError,
    SMS_RETRYABLE_EXCEPTIONS,
)
from booking.integrations.core import CoreClient
logger = logging.getLogger(__name__)


def ota_notification_payload(booking, stored_payload):
    """Add current PMS room assignments to OTA+PMS notification payloads."""
    payload = dict(stored_payload or {})
    if booking.hotel.inventory_mode != Hotel.InventoryMode.PHYSICAL_ROOM:
        return payload

    payload_rooms = [dict(item) for item in payload.get("rooms", [])]
    booking_rooms = list(booking.rooms.all())
    for index, booking_room in enumerate(booking_rooms):
        physical_rooms = [
            {
                "assignment_id": assignment.id,
                "id": assignment.physical_room_id,
                "core_physical_room_id": assignment.physical_room.core_physical_room_id,
                "room_number": assignment.physical_room.room_number,
                "floor": assignment.physical_room.floor,
                "building": assignment.physical_room.building,
                "status": assignment.physical_room.status,
                "assigned_at": assignment.assigned_at.isoformat(),
            }
            for assignment in booking_room.assignments.all()
            if assignment.released_at is None
        ]
        if index < len(payload_rooms):
            payload_rooms[index]["booking_room_id"] = booking_room.id
            payload_rooms[index]["physical_rooms"] = physical_rooms
        else:
            payload_rooms.append({
                "booking_room_id": booking_room.id,
                "room_type_id": booking_room.room_type_id,
                "room_type_name": booking_room.room_type.name,
                "quantity": booking_room.quantity,
                "physical_rooms": physical_rooms,
            })
    payload["rooms"] = payload_rooms
    return payload


def _hotel_booking_notification_contact(booking, field):
    """Return the hotel booking contact provisioned from Core verification."""
    return str((booking.hotel.core_snapshot or {}).get(field) or "").strip()


def queue_booking_confirmation_notifications(booking_id):
    """Publish independent guest/hotel email and SMS jobs."""
    ota_notification_result = create_and_queue_ota_booking_notification(
        booking_id, "new"
    )
    guest_email_result = send_booking_confirmation_email_task.delay(
        booking_id, "guest"
    )
    hotel_email_result = send_booking_confirmation_email_task.delay(
        booking_id, "hotel"
    )
    visit77_email_result = send_booking_confirmation_email_task.delay(
        booking_id, "visit77"
    )
    guest_sms_result = send_booking_confirmation_sms_task.delay(booking_id, "guest")
    hotel_sms_result = send_booking_confirmation_sms_task.delay(booking_id, "hotel")
    logger.info(
        "Queued booking confirmation notifications for booking %s: "
        "guest_email_task_id=%s hotel_email_task_id=%s visit77_email_task_id=%s "
        "guest_sms_task_id=%s hotel_sms_task_id=%s",
        booking_id,
        guest_email_result.id,
        hotel_email_result.id,
        visit77_email_result.id,
        guest_sms_result.id,
        hotel_sms_result.id,
    )
    return {
        "guest_email_task_id": guest_email_result.id,
        "hotel_email_task_id": hotel_email_result.id,
        "visit77_email_task_id": visit77_email_result.id,
        "guest_sms_task_id": guest_sms_result.id,
        "hotel_sms_task_id": hotel_sms_result.id,
        "ota_notification": ota_notification_result,
    }


def create_and_queue_ota_booking_notification(booking_id, event_type="new"):
    """Persist first; Firebase delivery must never control list visibility."""
    booking = Booking.objects.select_related("hotel").prefetch_related(
        "rooms__room_type",
        "rooms__assignments__physical_room",
    ).get(id=booking_id)
    if booking.source != Booking.Source.OTA:
        return {"skipped": True, "reason": "not_ota"}
    rooms = list(booking.rooms.all())
    payload = {
        "booking_id": str(booking.id),
        "booking_code": booking.booking_code,
        "reference": booking.reference,
        "status": booking.status,
        "guest_name": booking.contact_name,
        "guest_phone": booking.contact_phone,
        "check_in": booking.check_in.isoformat(),
        "check_out": booking.check_out.isoformat(),
        "nights": booking.nights,
        "adults": sum(room.adults * room.quantity for room in rooms),
        "children": sum(room.children * room.quantity for room in rooms),
        "rooms": [
            {
                "room_type_id": room.room_type_id,
                "room_type_name": room.room_type.name,
                "quantity": room.quantity,
            }
            for room in rooms
        ],
    }
    if booking.hotel.inventory_mode == Hotel.InventoryMode.PHYSICAL_ROOM:
        payload = ota_notification_payload(booking, payload)
    prefix = {"new": "New", "updated": "Updated", "cancelled": "Cancelled"}[event_type]
    body = f"{prefix}: {booking.booking_code}"
    notification = OTABookingNotification.objects.create(
        hotel=booking.hotel,
        booking=booking,
        event_type=event_type,
        body=body,
        payload=payload,
    )
    try:
        delivery_task = send_ota_booking_push_task.delay(notification.id)
        delivery_task_id = delivery_task.id
    except Exception:
        logger.exception(
            "OTA notification %s was stored but Firebase delivery could not be queued.",
            notification.id,
        )
        delivery_task_id = None
    return {
        "notification_id": notification.id,
        "delivery_task_id": delivery_task_id,
    }


@shared_task
def send_ota_booking_push_task(notification_id):
    notification = OTABookingNotification.objects.select_related(
        "hotel", "booking"
    ).get(id=notification_id)
    booking = notification.booking
    try:
        delivery = CoreClient().post(
            f"booking-integrations/businesses/{booking.hotel.core_business_id}/ota-notifications/",
            json={
                "event_type": notification.event_type,
                "body": notification.body,
                "notification_id": notification.id,
                "payload": notification.payload,
            },
        )
    except Exception:
        logger.exception("OTA notification %s was stored but Firebase delivery failed.", notification.id)
        delivery = {"sent_devices": 0, "delivery_failed": True}
    return {"notification_id": notification.id, "delivery": delivery}

@shared_task
def expire_booking_holds_task():
    count = expire_pending_bookings()
    return f"Expired {count} booking hold(s)."


@shared_task
def ensure_rolling_daily_inventory_task():
    summary = ensure_rolling_daily_inventory()
    return (
        f"Ensured inventory for {summary['room_types']} room type(s): "
        f"{summary['created']} created, {summary['updated']} updated."
    )


@shared_task
def auto_cancel_no_show_reservations_task():
    count = auto_cancel_no_show_reservations()
    return f"Auto-canceled {count} no-show reservation(s)."



@shared_task(
    bind=True,
    autoretry_for=(Exception,),
    retry_backoff=True,
    retry_backoff_max=300,
    retry_jitter=True,
    max_retries=3,
)
def send_booking_confirmation_email_task(self, booking_id, recipient_type="all"):
    booking = (
        Booking.objects
        .select_related("hotel")
        .prefetch_related(
            "guests", "rooms__room_type", "payments__invoice__lines",
            "payments__invoice__receipts",
        )
        .get(id=booking_id)
    )

    if recipient_type not in {"all", "guest", "hotel", "visit77"}:
        raise ValueError("recipient_type must be all, guest, hotel, or visit77.")

    guest_sent = False
    if recipient_type in {"all", "guest"}:
        guest_sent = send_booking_confirmation_email(booking)

    hotel_sent = False
    hotel_email = _hotel_booking_notification_contact(
        booking, "booking_notification_email"
    )
    guest_emails = {
        str(booking.contact_email or "").strip().lower(),
        *{
            str(email or "").strip().lower()
            for email in booking.guests.values_list("email", flat=True)
        },
    }
    if (
        recipient_type in {"all", "hotel"}
        and booking.source == Booking.Source.OTA
        and hotel_email
        and hotel_email.lower() not in guest_emails
    ):
        hotel_sent = send_booking_confirmation_email(
            booking, recipient_email=hotel_email
        )

    visit77_sent = False
    visit77_email = str(
        getattr(settings, "VISIT77_OTA_NOTIFICATION_EMAIL", "") or ""
    ).strip()
    if (
        recipient_type in {"all", "visit77"}
        and booking.source == Booking.Source.OTA
        and visit77_email
        and visit77_email.lower() not in guest_emails
        and visit77_email.lower() != hotel_email.lower()
    ):
        visit77_sent = send_booking_confirmation_email(
            booking, recipient_email=visit77_email
        )

    if not guest_sent and not hotel_sent and not visit77_sent:
        logger.info(
            "Booking confirmation %s email skipped because booking %s has no recipient email.",
            recipient_type,
            booking.booking_code,
        )

    return bool(guest_sent or hotel_sent or visit77_sent)

@shared_task(
    autoretry_for=SMS_RETRYABLE_EXCEPTIONS,
    retry_backoff=True,
    retry_backoff_max=300,
    retry_jitter=True,
    max_retries=3,
)
def send_booking_confirmation_sms_task(booking_id, recipient_type="all"):
    from booking.models import Booking
    from booking.booking_services.email import booking_room_summary
    from booking.booking_services.sms import send_custom_sms

    booking = (
        Booking.objects
        .select_related("hotel")
        .prefetch_related("guests", "rooms__room_type")
        .get(id=booking_id)
    )

    primary_guest = (
        booking.guests
        .filter(is_primary=True)
        .order_by("id")
        .first()
    )

    if not primary_guest:
        primary_guest = booking.guests.order_by("id").first()

    guest_name = primary_guest.name or booking.contact_name
    phone_no = booking.contact_phone or (
        primary_guest.phone if primary_guest else ""
    )

    booking_url = (
        f"{settings.BOOKING_FRONTEND_URL.rstrip('/')}"
        f"/bookings/{booking.public_token}"
    )
    room_summary = booking_room_summary(booking)
    room_section = f"{room_summary}\n" if room_summary else ""

    message = (
        f"Your VISIT 77 booking is confirmed.\n"
        f"Booking ID: {booking.booking_code}\n"
        f"Hotel: {booking.hotel.name}\n"
        f"Check-in: {booking.check_in:%d %b %Y}\n"
        f"Check-out: {booking.check_out:%d %b %Y}\n"
        f"Guest: {guest_name}\n"
        f"{room_section}"
        f"View booking: {booking_url}"
    )

    if recipient_type not in {"all", "guest", "hotel"}:
        raise ValueError("recipient_type must be all, guest, or hotel.")

    guest_phone = str(phone_no or "").strip()
    hotel_phone = ""
    if booking.source == Booking.Source.OTA:
        hotel_phone = _hotel_booking_notification_contact(
            booking, "booking_notification_phone_number"
        )

    recipients = []
    if recipient_type in {"all", "guest"} and guest_phone:
        recipients.append(guest_phone)
    if (
        recipient_type in {"all", "hotel"}
        and hotel_phone
        and hotel_phone != guest_phone
    ):
        recipients.append(hotel_phone)

    sent = False
    for recipient in recipients:
        try:
            send_custom_sms(phone_no=recipient, message=message)
            sent = True
        except PermanentSMSDeliveryError as exc:
            logger.warning(
                "Booking confirmation SMS permanently rejected for booking %s, "
                "recipient %s: %s",
                booking.booking_code,
                recipient,
                exc,
            )

    return sent
