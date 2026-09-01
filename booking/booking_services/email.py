# booking/services/email.py

from django.conf import settings
from django.core.mail import send_mail


def send_booking_confirmation_email(booking):
    primary_guest = booking.guests.filter(is_primary=True).first()

    if not primary_guest:
        primary_guest = booking.guests.order_by("id").first()

    if not primary_guest:
        return

    recipient_email = primary_guest.email or booking.contact_email

    if not recipient_email:
        return

    guest_name = primary_guest.name or booking.contact_name

    booking_url = (
        f"{settings.BOOKING_FRONTEND_URL}/booking/"
        f"{booking.public_token}"
    )

    subject = "Booking Confirmation with Visit77"

    message = f"""
Booking Confirmation with Visit77

Booking ID: {booking.booking_code}

Hotel: {booking.hotel.name}

Check-in: {booking.check_in}

Check-out: {booking.check_out}

Guest: {guest_name}

View booking: {booking_url}
""".strip()

    send_mail(
        subject=subject,
        message=message,
        from_email=settings.DEFAULT_FROM_EMAIL,
        recipient_list=[recipient_email],
        fail_silently=False,
    )
    return True
