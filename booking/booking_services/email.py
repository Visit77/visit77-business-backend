# booking/services/email.py

from django.conf import settings
from django.core.mail import EmailMultiAlternatives
from django.template.loader import render_to_string
from urllib.parse import quote_plus
from decimal import Decimal

from booking.booking_services.receipt import ensure_receipt_pdf
from booking.models import Payment


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


def _email_money(value, currency):
    value = Decimal(str(value or 0))
    amount = f"{value:,.2f}" if value % 1 else f"{int(value):,}"
    return f"{currency} {amount}"


def build_booking_confirmation_context(booking, primary_guest):
    rooms = list(booking.rooms.select_related("room_type").all())
    invoice = booking.invoices.prefetch_related("lines").order_by("issued_at", "id").first()
    line_totals = {}
    if invoice:
        for line in invoice.lines.all():
            line_type = (line.metadata or {}).get("line_type", "other")
            line_totals[line_type] = line_totals.get(line_type, Decimal("0")) + line.total
    extra_bed_total = line_totals.get("extra_bed", Decimal("0"))
    room_details = " • ".join(
        f"{room.room_type.name} x {room.quantity}" for room in rooms
    ) or "-"
    occupancy = " • ".join(
        f"{room.adults} Adult(s), {room.children} Child(ren)" for room in rooms
    ) or "-"
    meals = []
    for room in rooms:
        for meal in room.meal_plan_snapshots or []:
            if meal.get("name"):
                meals.append(meal["name"])
        if (room.breakfast_snapshot or {}).get("selected"):
            meals.append("Breakfast")
    meals = list(dict.fromkeys(meals))
    hotel_snapshot = booking.hotel.core_snapshot or {}
    hotel_email = (
        hotel_snapshot.get("email")
        or hotel_snapshot.get("contact_email")
        or getattr(settings, "RECEIPT_ISSUER_EMAIL", settings.DEFAULT_FROM_EMAIL)
    )
    hotel_image = booking.hotel.cover_image_url or hotel_snapshot.get("cover_image_url") or ""
    booking_url = f"{settings.BOOKING_FRONTEND_URL.rstrip('/')}/bookings/{booking.public_token}"
    policy = booking.cancellation_policy_snapshot or {}
    policy_text = policy.get("description") or policy.get("name") or policy.get("type") or "Please contact the hotel for cancellation terms."
    return {
        "property_name": booking.hotel.name,
        "property_address": booking.hotel.address or "-",
        "hotel_img_url": hotel_image,
        "hotel_phone": booking.hotel.phone or "-",
        "hotel_email": hotel_email,
        "map_url": f"https://www.google.com/maps/search/?api=1&query={quote_plus(booking.hotel.address or booking.hotel.name)}",
        "check_in_date": booking.check_in.strftime("%d %b %Y, %A"),
        "check_in_time": f"After {booking.hotel.check_in_time.strftime('%I:%M %p')}" if booking.hotel.check_in_time else "Check with hotel",
        "check_out_date": booking.check_out.strftime("%d %b %Y, %A"),
        "check_out_time": f"Before {booking.hotel.check_out_time.strftime('%I:%M %p')}" if booking.hotel.check_out_time else "Check with hotel",
        "room_details": room_details,
        "occupancy": occupancy,
        "guest_name": primary_guest.name or booking.contact_name,
        "meal_info": ", ".join(meals) if meals else "No meal selected",
        "special_requests": booking.special_request or "None",
        "subtotal": _email_money(
            (invoice.subtotal - extra_bed_total) if invoice else booking.room_total,
            booking.currency,
        ),
        "extra_bed": _email_money(extra_bed_total, booking.currency),
        "taxes": _email_money(invoice.tax_total if invoice else booking.tax_total, booking.currency),
        "discount": _email_money(invoice.discount_total if invoice else booking.discount_total, booking.currency),
        "total_price": _email_money(invoice.total if invoice else booking.grand_total, booking.currency),
        "booking_code": booking.booking_code,
        "booking_url": booking_url,
        "cancellation_policy": policy_text.replace("_", " ").title(),
        "is_fully_paid": booking.amount_paid >= booking.grand_total,
    }


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

    booking_url = f"{settings.BOOKING_FRONTEND_URL.rstrip('/')}/bookings/{booking.public_token}"

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

    html_message = render_to_string(
        "emails/booking_confirmation.html",
        build_booking_confirmation_context(booking, primary_guest),
    )
    email = EmailMultiAlternatives(
        subject=subject,
        body=message,
        from_email=settings.DEFAULT_FROM_EMAIL,
        to=[recipient_email],
    )
    email.attach_alternative(html_message, "text/html")
    latest_receipt = booking.payments.filter(
        status__in=[Payment.Status.PAID, Payment.Status.PARTIALLY_REFUNDED],
        receipt_number__isnull=False,
    ).order_by("-paid_at", "-created_at").first()
    if latest_receipt:
        latest_receipt = ensure_receipt_pdf(latest_receipt)
        with latest_receipt.receipt_pdf.open("rb") as receipt_file:
            email.attach(
                f"{latest_receipt.receipt_number}.pdf",
                receipt_file.read(),
                "application/pdf",
            )
    email.send(fail_silently=False)
    return True
