from io import BytesIO
from decimal import Decimal

from django.conf import settings
from django.core.files.base import ContentFile
from django.db import transaction
from django.utils import timezone
from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER, TA_RIGHT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

from booking.models import Payment


def _money(value, currency):
    amount = Decimal(str(value or 0))
    return f"{currency} {amount:,.2f}" if amount % 1 else f"{currency} {int(amount):,}"


def build_receipt_snapshot(payment):
    """Capture immutable financial and booking data used by the receipt."""
    payment = Payment.objects.select_related("booking__hotel", "invoice").prefetch_related(
        "booking__guests", "booking__rooms__room_type", "invoice__lines", "invoice__receipts",
    ).get(pk=payment.pk)
    booking = payment.booking
    invoice = payment.invoice
    primary_guest = next(
        (guest for guest in booking.guests.all() if guest.is_primary),
        next(iter(booking.guests.all()), None),
    )
    room_charge = Decimal("0")
    extra_bed_charge = Decimal("0")
    additional_charge = Decimal("0")
    lines = []
    if invoice:
        for line in invoice.lines.all():
            line_type = (line.metadata or {}).get("line_type", "other")
            if line_type == "room":
                room_charge += line.total
            elif line_type == "extra_bed":
                extra_bed_charge += line.total
            elif line_type != "service_fee":
                additional_charge += line.total
            lines.append({
                "description": line.description,
                "quantity": str(line.quantity),
                "unit_price": str(line.unit_price),
                "total": str(line.total),
                "line_type": line_type,
            })
    paid_before = Decimal("0")
    if invoice:
        paid_before = sum(
            (
                item.amount - item.refunded_amount
                for item in invoice.receipts.all()
                if item.pk != payment.pk
                and item.status in {Payment.Status.PAID, Payment.Status.PARTIALLY_REFUNDED}
            ),
            Decimal("0"),
        )
    remaining = max((invoice.total if invoice else booking.grand_total) - paid_before - payment.amount, Decimal("0"))
    return {
        "version": 1,
        "receipt_number": payment.receipt_number,
        "invoice_number": invoice.invoice_number if invoice else payment.invoice_number,
        "payment_date": (payment.paid_at or payment.created_at or timezone.now()).isoformat(),
        "provider": payment.provider,
        "provider_reference": payment.provider_reference,
        "currency": payment.currency,
        "amount_paid": str(payment.amount),
        "refunded_amount": str(payment.refunded_amount),
        "remaining_balance": str(remaining),
        "booking": {
            "id": str(booking.id),
            "booking_code": booking.booking_code,
            "check_in": booking.check_in.isoformat(),
            "check_out": booking.check_out.isoformat(),
            "nights": booking.nights,
            "hotel_name": booking.hotel.name,
            "hotel_address": booking.hotel.address,
            "rooms": [
                {
                    "room_type": room.room_type.name,
                    "quantity": room.quantity,
                    "extra_beds": room.extra_beds,
                }
                for room in booking.rooms.all()
            ],
        },
        "guest": {
            "name": primary_guest.name if primary_guest else booking.contact_name,
            "phone": (primary_guest.phone if primary_guest else "") or booking.contact_phone,
            "email": (primary_guest.email if primary_guest else "") or booking.contact_email,
            "billing_address": "",
        },
        "invoice": {
            "lines": lines,
            "room_charge_total": str(room_charge),
            "extra_bed_total": str(extra_bed_charge),
            "additional_charge_total": str(additional_charge),
            "subtotal": str(invoice.subtotal if invoice else booking.grand_total),
            "tax_total": str(invoice.tax_total if invoice else booking.tax_total),
            "discount_total": str(invoice.discount_total if invoice else booking.discount_total),
            "invoice_total": str(invoice.total if invoice else booking.grand_total),
        },
        "issuer": {
            "name": getattr(settings, "RECEIPT_ISSUER_NAME", "Visit77 Co.,Ltd."),
            "address": getattr(
                settings,
                "RECEIPT_ISSUER_ADDRESS",
                "10-06, Panchan Tower, Bargayar St., Sanchaung Tsp., Yangon, Myanmar.",
            ),
            "email": getattr(settings, "RECEIPT_ISSUER_EMAIL", settings.DEFAULT_FROM_EMAIL),
            "phone": getattr(settings, "RECEIPT_ISSUER_PHONE", "(+95) 988 577 0011"),
        },
    }


def finalize_receipt_snapshot(payment):
    if not payment.receipt_number or payment.receipt_snapshot:
        return payment
    payment.receipt_snapshot = build_receipt_snapshot(payment)
    payment.save(update_fields=["receipt_snapshot"])
    return payment


def render_receipt_pdf(snapshot):
    buffer = BytesIO()
    doc = SimpleDocTemplate(
        buffer, pagesize=A4, rightMargin=18 * mm, leftMargin=18 * mm,
        topMargin=12 * mm, bottomMargin=10 * mm,
    )
    styles = getSampleStyleSheet()
    small = ParagraphStyle("ReceiptSmall", parent=styles["BodyText"], fontSize=9, leading=14)
    title = ParagraphStyle("ReceiptTitle", parent=styles["Heading1"], fontSize=18, leading=22)
    center = ParagraphStyle("ReceiptCenter", parent=small, alignment=TA_CENTER)
    right = ParagraphStyle("ReceiptRight", parent=small, alignment=TA_RIGHT)
    blue = colors.HexColor("#3039F5")
    border = colors.HexColor("#8190A8")
    pale = colors.HexColor("#F3F6FA")
    currency = snapshot["currency"]
    issuer = snapshot["issuer"]
    booking = snapshot["booking"]
    guest = snapshot["guest"]
    invoice = snapshot["invoice"]
    payment_date = timezone.datetime.fromisoformat(snapshot["payment_date"]).strftime("%d %b %Y")

    story = [Table([["", ""]], colWidths=[159 * mm, 0], rowHeights=[2 * mm], style=[("BACKGROUND", (0, 0), (-1, -1), blue)]), Spacer(1, 6 * mm)]
    story.append(Table([
        [Paragraph(f"<b>{issuer['name']}</b><br/>{issuer['address']}<br/>{issuer['email']}<br/>{issuer['phone']}", small),
         Paragraph("<font color='#3039F5' size='18'><b>Visit77</b></font>", right)],
    ], colWidths=[105 * mm, 54 * mm]))
    story.extend([Spacer(1, 3 * mm), Table([[""]], colWidths=[159 * mm], rowHeights=[0.4], style=[("BACKGROUND", (0, 0), (-1, -1), border)]), Spacer(1, 4 * mm)])
    story.append(Table([
        [Paragraph("<b>Receipt</b>", title), Paragraph(f"<b>Booking ID:</b> &nbsp; {booking['booking_code']}<br/><b>Payment Date:</b> &nbsp; {payment_date}", small)],
        [Paragraph(snapshot["receipt_number"], styles["BodyText"]), ""],
    ], colWidths=[105 * mm, 54 * mm]))
    story.append(Spacer(1, 4 * mm))

    def section(title_text, rows, widths=(42 * mm, 117 * mm)):
        data = [[Paragraph(f"<b>{title_text}</b>", center), ""]] + [
            [Paragraph(str(label), small), Paragraph(str(value or "-"), small)] for label, value in rows
        ]
        table = Table(data, colWidths=list(widths), repeatRows=1)
        table.setStyle(TableStyle([
            ("SPAN", (0, 0), (-1, 0)), ("BACKGROUND", (0, 0), (-1, 0), pale),
            ("BOX", (0, 0), (-1, -1), 0.8, border), ("LINEBELOW", (0, 0), (-1, 0), 0.8, border),
            ("VALIGN", (0, 0), (-1, -1), "TOP"), ("LEFTPADDING", (0, 0), (-1, -1), 6),
            ("RIGHTPADDING", (0, 0), (-1, -1), 6), ("TOPPADDING", (0, 0), (-1, -1), 3),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
        ]))
        story.extend([table, Spacer(1, 4 * mm)])

    section("GUEST DETAILS", [("Name", guest["name"]), ("Billing Address", guest["billing_address"]), ("Email Address", guest["email"])])
    room_rows = [("Hotel Name", booking["hotel_name"]), ("Period", f"{booking['check_in']} - {booking['check_out']} ({booking['nights']} night(s))")]
    for room in booking["rooms"]:
        room_rows.extend([("Room Type", room["room_type"]), ("No. of Rooms", room["quantity"]), ("No. of Extra Beds", room["extra_beds"])])
    section("BOOKING DETAILS", room_rows)

    amount_rows = [
        ("Total Room Charges", _money(invoice["room_charge_total"], currency)),
        ("Total Extra Bed Charges", _money(invoice["extra_bed_total"], currency)),
        ("Additional Charges", _money(invoice["additional_charge_total"], currency)),
        ("Discount", f"-{_money(invoice['discount_total'], currency)}"),
        ("Invoice Total", _money(invoice["invoice_total"], currency)),
        ("Amount Paid", _money(snapshot["amount_paid"], currency)),
        ("Remaining Balance", _money(snapshot["remaining_balance"], currency)),
    ]
    amount_table = Table(
        [[Paragraph("<b>DESCRIPTION</b>", center), Paragraph("<b>AMOUNT</b>", center)]]
        + [[Paragraph(label, small), Paragraph(amount, right)] for label, amount in amount_rows],
        colWidths=[114 * mm, 45 * mm], repeatRows=1,
    )
    amount_table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), pale), ("GRID", (0, 0), (-1, 0), 0.8, border),
        ("BOX", (0, 0), (-1, -1), 0.8, border), ("LINEBEFORE", (1, 0), (1, -1), 0.8, border),
        ("LINEABOVE", (0, -2), (-1, -2), 0.8, border), ("LINEABOVE", (0, -1), (-1, -1), 0.8, border),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"), ("TOPPADDING", (0, 0), (-1, -1), 4),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
    ]))
    story.append(amount_table)

    def footer(canvas, document):
        canvas.saveState()
        canvas.setFont("Helvetica", 8)
        canvas.drawRightString(A4[0] - 18 * mm, 10 * mm, f"Page {document.page} of {document.page}")
        canvas.restoreState()

    doc.build(story, onFirstPage=footer, onLaterPages=footer)
    return buffer.getvalue()


@transaction.atomic
def ensure_receipt_pdf(payment):
    payment = Payment.objects.select_for_update().get(pk=payment.pk)
    if payment.receipt_pdf:
        return payment
    if not payment.receipt_number:
        raise ValueError("A receipt PDF can only be generated for a completed payment.")
    finalize_receipt_snapshot(payment)
    pdf_bytes = render_receipt_pdf(payment.receipt_snapshot)
    payment.receipt_pdf.save(
        f"{payment.receipt_number}.pdf",
        ContentFile(pdf_bytes),
        save=False,
    )
    payment.receipt_pdf_generated_at = timezone.now()
    payment.save(update_fields=["receipt_pdf", "receipt_pdf_generated_at"])
    return payment
