from celery import shared_task
from django.conf import settings

from booking.services import (
    auto_cancel_no_show_reservations,
    ensure_rolling_daily_inventory,
    expire_pending_bookings,
)
import logging
from booking.models import Booking
from booking.booking_services.email import send_booking_confirmation_email
logger = logging.getLogger(__name__)


def _hotel_booking_notification_contact(booking, field):
    """Return the hotel booking contact provisioned from Core verification."""
    return str((booking.hotel.core_snapshot or {}).get(field) or "").strip()

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
def send_booking_confirmation_email_task(self, booking_id):
    booking = (
        Booking.objects
        .select_related("hotel")
        .prefetch_related(
            "guests", "rooms__room_type", "payments__invoice__lines",
            "payments__invoice__receipts",
        )
        .get(id=booking_id)
    )

    sent = send_booking_confirmation_email(booking)
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
        booking.source == Booking.Source.OTA
        and hotel_email
        and hotel_email.lower() not in guest_emails
    ):
        hotel_sent = send_booking_confirmation_email(
            booking, recipient_email=hotel_email
        )

    if not sent and not hotel_sent:
        logger.info(
            "Booking confirmation email skipped because booking %s has no recipient email.",
            booking.booking_code,
        )

    return bool(sent or hotel_sent)

@shared_task(
    autoretry_for=(Exception,),
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
    phone_no = (
        primary_guest.phone
        if primary_guest and primary_guest.phone
        else booking.contact_phone
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

    for recipient in recipients:
        send_custom_sms(phone_no=recipient, message=message)

    return bool(recipients)
