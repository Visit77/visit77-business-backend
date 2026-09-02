# booking/services/email.py

from django.conf import settings
from django.core.mail import send_mail


def booking_room_summary(booking):
    """Return one quantity-totalled line per room type for confirmations."""
    room_quantities = {}
    for booking_room in booking.rooms.all():
        room_type_name = booking_room.room_type.name
        room_quantities[room_type_name] = (
            room_quantities.get(room_type_name, 0) + booking_room.quantity
        )
    return "\n".join(
        f"Room: {room_type_name} x {quantity}"
        for room_type_name, quantity in room_quantities.items()
    )


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

    subject = "Booking Confirmation with VISIT 77"
    room_summary = booking_room_summary(booking)
    room_section = f"\n\n{room_summary}" if room_summary else ""

    message = f"""
Booking Confirmation with VISIT 77

Booking ID: {booking.booking_code}

Hotel: {booking.hotel.name}

Check-in: {booking.check_in}

Check-out: {booking.check_out}

Guest: {guest_name}
{room_section}

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
