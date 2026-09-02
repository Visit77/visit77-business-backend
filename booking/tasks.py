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
        .prefetch_related("guests")
        .get(id=booking_id)
    )

    sent = send_booking_confirmation_email(booking)

    if not sent:
        logger.info(
            "Booking confirmation email skipped because booking %s has no email.",
            booking.booking_code,
        )

    return sent

@shared_task(
    autoretry_for=(Exception,),
    retry_backoff=True,
    retry_backoff_max=300,
    retry_jitter=True,
    max_retries=3,
)
def send_booking_confirmation_sms_task(booking_id):
    from booking.models import Booking
    from booking.booking_services.sms import send_custom_sms

    booking = (
        Booking.objects
        .select_related("hotel")
        .prefetch_related("guests")
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

    if not phone_no:
        return False

    booking_url = (
        f"{settings.BOOKING_FRONTEND_URL.rstrip('/')}"
        f"/bookings/{booking.public_token}"
    )

    message = (
        f"Your VISIT 77 booking is confirmed.\n"
        f"Booking ID: {booking.booking_code}\n"
        f"Hotel: {booking.hotel.name}\n"
        f"Check-in: {booking.check_in:%d %b %Y}\n"
        f"Check-out: {booking.check_out:%d %b %Y}\n"
        f"Guest: {guest_name}\n"
        f"View booking: {booking_url}"
    )

    send_custom_sms(
        phone_no=phone_no,
        message=message,
    )

    return True
