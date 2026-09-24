from io import BytesIO
from decimal import Decimal
from pathlib import Path
from xml.sax.saxutils import escape
from urllib.parse import urlparse
import json

import httpx

from django.conf import settings
from django.core.files.base import ContentFile
from django.db import transaction
from django.utils import timezone
from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER, TA_RIGHT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import Image, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

from booking.models import Invoice, Payment


def _money(value, currency):
    amount = Decimal(str(value or 0))
    return f"{currency} {amount:,.2f}" if amount % 1 else f"{currency} {int(amount):,}"


def _hotel_address(hotel):
    snapshot = hotel.core_snapshot or {}
    value = (
        snapshot.get("address_info")
        or snapshot.get("address")
        or hotel.address
    )
    return ", ".join(_contact_values(value))


def _hotel_phone(hotel):
    snapshot = hotel.core_snapshot or {}
    value = snapshot.get("phone") or snapshot.get("phone_no") or hotel.phone
    return ", ".join(_contact_values(value))


def _hotel_email(hotel):
    snapshot = hotel.core_snapshot or {}
    # The synced `email` field is the active Merchant Verification business
    # contact. Booking-notification contacts are delivery recipients, not
    # document hotel information.
    value = snapshot.get("email") or ""
    return ", ".join(_contact_values(value))


def _contact_values(value):
    """Flatten contact/address values and hide empty JSON-list placeholders."""
    if value is None:
        return []
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return []
        if text[:1] in {"[", "{"}:
            try:
                return _contact_values(json.loads(text))
            except (json.JSONDecodeError, TypeError):
                pass
        return [text]
    if isinstance(value, dict):
        values = value.values()
    elif isinstance(value, (list, tuple, set)):
        values = value
    else:
        values = [value]
    flattened = []
    for item in values:
        flattened.extend(_contact_values(item))
    return flattened


def _hotel_logo(logo_url):
    """Load only Visit77-hosted hotel branding; fall back safely on any error."""
    if not logo_url:
        return None
    parsed = urlparse(str(logo_url))
    hostname = (parsed.hostname or "").lower()
    if parsed.scheme not in {"http", "https"} or not (
        hostname == "visit77.com" or hostname.endswith(".visit77.com")
    ):
        return None
    try:
        response = httpx.get(str(logo_url), timeout=3, follow_redirects=True)
        response.raise_for_status()
        final_hostname = (urlparse(str(response.url)).hostname or "").lower()
        if not (
            final_hostname == "visit77.com"
            or final_hostname.endswith(".visit77.com")
        ):
            return None
        content = response.content
        if not content or len(content) > 2 * 1024 * 1024:
            return None
        return Image(BytesIO(content), width=40 * mm, height=15 * mm, kind="proportional")
    except (httpx.HTTPError, OSError, ValueError):
        return None


def _build_document_snapshot(*, booking, invoice, payment=None):
    """Build the shared immutable data used to render invoices and receipts."""
    is_pms = booking.source == booking.Source.PMS
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
                "metadata": line.metadata or {},
            })
    paid_before = Decimal("0")
    if invoice:
        paid_before = sum(
            (
                item.amount - item.refunded_amount
                for item in invoice.receipts.all()
                if (payment is None or item.pk != payment.pk)
                and item.status in {Payment.Status.PAID, Payment.Status.PARTIALLY_REFUNDED}
            ),
            Decimal("0"),
        )
    payment_amount = payment.amount if payment is not None else paid_before
    amount_applied = paid_before + payment_amount if payment is not None else paid_before
    remaining = max((invoice.total if invoice else booking.grand_total) - amount_applied, Decimal("0"))
    grouped_rooms = {}
    for room in booking.rooms.all():
        room_key = room.room_type_id
        if room_key not in grouped_rooms:
            grouped_rooms[room_key] = {
                "room_type": room.room_type.name,
                "quantity": 0,
                "extra_beds": 0,
            }
        grouped_rooms[room_key]["quantity"] += room.quantity
        grouped_rooms[room_key]["extra_beds"] += room.extra_beds
    return {
        "version": 2,
        "receipt_number": payment.receipt_number if payment is not None else "",
        "invoice_number": invoice.invoice_number if invoice else payment.invoice_number,
        "payment_date": (
            (payment.paid_at or payment.created_at)
            if payment is not None else invoice.issued_at
        ).isoformat(),
        "provider": payment.provider if payment is not None else "",
        "provider_reference": payment.provider_reference if payment is not None else "",
        "currency": payment.currency if payment is not None else invoice.currency,
        "amount_paid": str(payment_amount),
        "refunded_amount": str(payment.refunded_amount if payment is not None else Decimal("0")),
        "remaining_balance": str(remaining),
        "booking": {
            "id": str(booking.id),
            "source": booking.source,
            "booking_code": booking.booking_code,
            "reservation_code": booking.reservation_code,
            "check_in": booking.check_in.isoformat(),
            "check_out": booking.check_out.isoformat(),
            "nights": booking.nights,
            "hotel_name": booking.hotel.name,
            "hotel_address": _hotel_address(booking.hotel),
            "hotel_phone": _hotel_phone(booking.hotel),
            "hotel_email": _hotel_email(booking.hotel),
            "rooms": list(grouped_rooms.values()),
        },
        "guest": {
            "name": primary_guest.name if primary_guest else booking.contact_name,
            "phone": (primary_guest.phone if primary_guest else "") or booking.contact_phone,
            "email": (primary_guest.email if primary_guest else "") or booking.contact_email,
            "billing_address": "",
        },
        "invoice": {
            "lines": lines,
            "charge_scope": invoice.charge_scope if invoice else (
                Invoice.ChargeScope.OTA if booking.source == booking.Source.OTA else Invoice.ChargeScope.PMS
            ),
            "service_charges": (invoice.charge_snapshot or {}).get("service_charges", []) if invoice else [],
            "tax_charges": (invoice.charge_snapshot or {}).get("taxes", []) if invoice else [],
            "room_charge_total": str(room_charge),
            "extra_bed_total": str(extra_bed_charge),
            "additional_charge_total": str(additional_charge),
            "subtotal": str(invoice.subtotal if invoice else booking.grand_total),
            "tax_total": str(invoice.tax_total if invoice else booking.tax_total),
            "discount_total": str(invoice.discount_total if invoice else booking.discount_total),
            "invoice_total": str(invoice.total if invoice else booking.grand_total),
        },
        "issuer": {
            "name": (
                booking.hotel.name
                if is_pms
                else getattr(settings, "RECEIPT_ISSUER_NAME", "Visit 77 Company Limited")
            ),
            "address": (
                _hotel_address(booking.hotel)
                if is_pms
                else getattr(
                    settings,
                    "RECEIPT_ISSUER_ADDRESS",
                    "#10-06, Panchan Tower, Bargayar Street, San Chaung Tsp., Yangon, Myanmar. 11111",
                )
            ),
            "email": (
                _hotel_email(booking.hotel)
                if is_pms
                else getattr(settings, "RECEIPT_ISSUER_EMAIL", settings.DEFAULT_FROM_EMAIL)
            ),
            "phone": (
                _hotel_phone(booking.hotel)
                if is_pms
                else getattr(settings, "RECEIPT_ISSUER_PHONE", "(+95) 988 577 0011")
            ),
            "logo_url": booking.hotel.cover_image_url if is_pms else "",
            "branding": "hotel" if is_pms else "visit77",
            "footer_text": "Powered by Visit77" if is_pms else "",
        },
    }


def build_receipt_snapshot(payment):
    """Capture immutable financial and booking data used by the receipt."""
    payment = Payment.objects.select_related("booking__hotel", "invoice").prefetch_related(
        "booking__guests", "booking__rooms__room_type", "invoice__lines", "invoice__receipts",
    ).get(pk=payment.pk)
    return _build_document_snapshot(
        booking=payment.booking,
        invoice=payment.invoice,
        payment=payment,
    )


def build_invoice_snapshot(invoice):
    """Build an invoice snapshot even when the invoice has no payment yet."""
    invoice = Invoice.objects.select_related("booking__hotel").prefetch_related(
        "booking__guests", "booking__rooms__room_type", "lines", "receipts",
    ).get(pk=invoice.pk)
    return _build_document_snapshot(
        booking=invoice.booking,
        invoice=invoice,
    )


def finalize_receipt_snapshot(payment):
    if not payment.receipt_number or payment.receipt_snapshot:
        return payment
    payment.receipt_snapshot = build_receipt_snapshot(payment)
    payment.save(update_fields=["receipt_snapshot"])
    return payment


def _render_payment_document_pdf(snapshot, document_title, document_number):
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
    is_pms = invoice.get("charge_scope", booking.get("source", "pms")) == "pms"
    if not is_pms:
        # OTA documents always use the approved Visit77 legal/company details,
        # including when rendering an older immutable payment snapshot.
        issuer = {
            **issuer,
            "name": "Visit 77 Company Limited",
            "address": "#10-06, Panchan Tower, Bargayar Street, San Chaung Tsp., Yangon, Myanmar. 11111",
            "email": "contact.myanmar@visit77.com",
            "phone": "(+95) 988 577 0011",
            "branding": "visit77",
        }
    payment_date = timezone.datetime.fromisoformat(snapshot["payment_date"]).strftime("%d %b %Y")

    story = []
    if issuer.get("branding") == "hotel":
        issuer_detail_lines = []
        for field_name in ("address", "email", "phone"):
            issuer_detail_lines.extend(
                escape(item) for item in _contact_values(issuer.get(field_name))
            )
    else:
        issuer_detail_lines = [
            f"<b>{escape(str(issuer['name']))}</b>",
            escape(str(issuer.get("address") or "")),
            (
                f"<u><font color='#0066CC'>{escape(str(issuer.get('email') or ''))}</font></u>, "
                f"{escape(str(issuer.get('phone') or ''))}"
            ),
        ]
    issuer_details = "<br/>".join(issuer_detail_lines)
    brand_label = (
        issuer["name"]
        if issuer.get("branding") == "hotel"
        else "Visit77"
    )
    brand_content = None
    if issuer.get("branding") == "hotel":
        hotel_logo = _hotel_logo(issuer.get("logo_url"))
        hotel_name = Paragraph(
            f"<font color='#3039F5' size='18'><b>{escape(str(brand_label))}</b></font>",
            small,
        )
        if hotel_logo is not None:
            brand_content = Table(
                [[hotel_logo, hotel_name]],
                colWidths=[43 * mm, 61 * mm],
                style=TableStyle([
                    ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                    ("LEFTPADDING", (0, 0), (-1, -1), 0),
                    ("RIGHTPADDING", (0, 0), (-1, -1), 2),
                    ("TOPPADDING", (0, 0), (-1, -1), 0),
                    ("BOTTOMPADDING", (0, 0), (-1, -1), 0),
                ]),
            )
        else:
            brand_content = hotel_name
    else:
        image_dir = Path(__file__).resolve().parents[1] / "static" / "booking" / "images"
        icon = Image(str(image_dir / "visit77-icon.png"), width=13 * mm, height=12.35 * mm)
        wordmark = Image(str(image_dir / "visit77-logo.png"), width=39 * mm, height=11.73 * mm)
        brand_content = Table(
            [[icon, wordmark]],
            colWidths=[14 * mm, 40 * mm],
            style=TableStyle([
                ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                ("LEFTPADDING", (0, 0), (-1, -1), 0),
                ("RIGHTPADDING", (0, 0), (-1, -1), 0),
                ("TOPPADDING", (0, 0), (-1, -1), 0),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 0),
            ]),
        )
    if brand_content is None:
        brand_content = Paragraph(
            f"<font color='#3039F5' size='18'><b>{escape(str(brand_label))}</b></font>",
            right,
        )
    document_heading = escape(document_title)
    if is_pms and issuer.get("footer_text"):
        document_heading = (
            f"<font size='7' color='#3039F5'>{escape(issuer['footer_text'])}</font>"
            f"<br/><b>{document_heading}</b>"
        )
    identifier_label = "Reservation ID" if is_pms else "Booking ID"
    identifier_value = booking.get("booking_code")
    if is_pms:
        identifier_value = booking.get("reservation_code") or identifier_value
    header = Table([[
        brand_content,
        Paragraph(document_heading, ParagraphStyle(
            "DocumentHeaderTitle", parent=title, alignment=TA_RIGHT,
        )),
    ]], colWidths=[105 * mm, 54 * mm])
    header.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "BOTTOM"),
        ("LEFTPADDING", (0, 0), (-1, -1), 0),
        ("RIGHTPADDING", (0, 0), (-1, -1), 0),
        ("TOPPADDING", (0, 0), (-1, -1), 0),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 0),
    ]))
    story.extend([
        header,
        Spacer(1, 2 * mm),
        Table([[""]], colWidths=[159 * mm], rowHeights=[2 * mm], style=[
            ("BACKGROUND", (0, 0), (-1, -1), blue),
        ]),
        Spacer(1, 3 * mm),
        Table([[Paragraph(issuer_details, small)]], colWidths=[159 * mm], style=[
            ("LEFTPADDING", (0, 0), (-1, -1), 0),
            ("RIGHTPADDING", (0, 0), (-1, -1), 0),
            ("TOPPADDING", (0, 0), (-1, -1), 0),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 0),
        ]),
        Spacer(1, 2 * mm),
        Table([[""]], colWidths=[159 * mm], rowHeights=[0.4], style=[
            ("BACKGROUND", (0, 0), (-1, -1), border),
        ]),
        Spacer(1, 3 * mm),
        Table([[
            "",
            Table([
                [Paragraph(f"<b>{escape(document_title)} ID</b>", right), ":", Paragraph(escape(str(document_number)), right)],
                [Paragraph(f"<b>{identifier_label}</b>", right), ":", Paragraph(escape(str(identifier_value)), right)],
                [Paragraph("<b>Payment Date</b>", right), ":", Paragraph(payment_date, right)],
            ], colWidths=[28 * mm, 3 * mm, 44 * mm], style=TableStyle([
                ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                ("LEFTPADDING", (0, 0), (-1, -1), 0),
                ("RIGHTPADDING", (0, 0), (-1, -1), 1),
                ("TOPPADDING", (0, 0), (-1, -1), 2),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 2),
            ])),
        ]], colWidths=[84 * mm, 75 * mm]),
    ])
    story.append(Spacer(1, 4 * mm))

    def section(title_text, rows, widths=(42 * mm, 117 * mm)):
        data = [[Paragraph(f"<b>{title_text}</b>", center), ""]] + [
            [
                Paragraph(str(label) if label else " ", small),
                Paragraph(str(value) if value else (" " if not label else "-"), small),
            ] for label, value in rows
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

    detail_rows = [
        ("Guest Name", guest["name"]),
        ("Contact Phone", guest.get("phone", "")),
        ("Email Address", guest["email"]),
        ("", ""),
        ("Hotel Name", booking["hotel_name"]),
        ("Stay Period", f"{booking['check_in']} - {booking['check_out']} ({booking['nights']} night(s))"),
        ("Contact Phone", booking.get("hotel_phone", "")),
        ("Email Address", booking.get("hotel_email", "")),
    ]
    section("GUEST &amp; HOTEL DETAILS", detail_rows)

    amount_rows = []
    centered_label_rows = set()
    section_ends = []
    room_lines = [line for line in invoice["lines"] if line["line_type"] in {"room", "extra_bed"}]
    service_charge_lines = [
        line for line in invoice["lines"]
        if line["line_type"] == "invoice_service_charge"
        or (
            line["line_type"] in {"ota_other_charge", "invoice_other_charge"}
            and line["description"].strip().casefold() == "service charge"
        )
    ]
    adjustment_lines = [
        line for line in invoice["lines"]
        if line["line_type"] == "adjustment"
        or line["description"].strip().casefold() == "adjustment"
    ]
    additional_lines = [
        line for line in invoice["lines"]
        if line["line_type"] not in {"room", "extra_bed", "service_fee"}
        and line not in service_charge_lines and line not in adjustment_lines
    ]
    if room_lines:
        amount_rows.append(("<b>Room Charges</b>", ""))
        amount_rows.extend(
            (escape(line["description"]), _money(line["total"], currency))
            for line in room_lines
        )
        room_total = Decimal(invoice["room_charge_total"]) + Decimal(invoice["extra_bed_total"])
    else:
        room_total = Decimal(invoice["room_charge_total"]) + Decimal(invoice["extra_bed_total"])
    amount_rows.append(("<b>Total Room Charge</b>", f"<b>{_money(room_total, currency)}</b>"))
    section_ends.append(len(amount_rows))
    amount_rows.append(("<b>Additional Charges</b>", ""))
    if additional_lines:
        amount_rows.extend(
            (escape(line["description"]), _money(line["total"], currency))
            for line in additional_lines
        )
    else:
        amount_rows.append(("-", "0"))
    section_ends.append(len(amount_rows) + 1)
    service_charge_total = sum(
        (Decimal(str(line["total"])) for line in service_charge_lines), Decimal("0"),
    )
    adjustment_total = sum(
        (Decimal(str(line["total"])) for line in adjustment_lines), Decimal("0"),
    )
    discount_total = Decimal(str(invoice["discount_total"]))
    if is_pms:
        total_charges = Decimal(str(invoice["subtotal"])) - service_charge_total - adjustment_total
        display_subtotal = total_charges - discount_total + adjustment_total
        amount_rows.extend([
            ("<b>Total Charges</b>", f"<b>{_money(total_charges, currency)}</b>"),
            ("Discount", f"-{_money(discount_total, currency)}" if discount_total else "0"),
            ("Adjustment", _money(adjustment_total, currency) if adjustment_total else "0"),
        ])
    else:
        # OTA documents have no Total Charges/discount/adjustment section.
        display_subtotal = Decimal(str(invoice["subtotal"])) - service_charge_total - discount_total
        section_ends.pop()
    section_ends.append(len(amount_rows) + 1)
    amount_rows.append(("<b>Subtotal</b>", f"<b>{_money(display_subtotal, currency)}</b>"))
    service_charge_rules = invoice.get("service_charges") or []
    for line in service_charge_lines:
        metadata = line.get("metadata") or {}
        charge_index = metadata.get("charge_index")
        rule = (
            service_charge_rules[charge_index]
            if isinstance(charge_index, int) and 0 <= charge_index < len(service_charge_rules)
            else next((item for item in service_charge_rules if item.get("title") == line["description"]), {})
        )
        mode_suffix = {
            "percentage": " (%)",
            "fixed": " (fixed amount)",
        }.get(rule.get("mode"), "")
        amount_rows.append((
            f"{escape(line['description'])}{mode_suffix}",
            _money(line["total"], currency),
        ))
    if is_pms and not service_charge_lines:
        amount_rows.append(("Service Charge", "0"))
    tax_charges = invoice.get("tax_charges") or []
    for charge in tax_charges:
        if charge["mode"] == "do_not_show":
            continue
        mode_suffix = {
            "percentage": " (%)",
            "fixed": " (fixed amount)",
        }.get(charge["mode"], "")
        amount_rows.append((
            f"{escape(charge['title'])}{mode_suffix}",
            "Included" if charge["mode"] == "included" else _money(charge["amount"], currency),
        ))
    if is_pms and not tax_charges:
        amount_rows.append((
            "Tax",
            _money(invoice["tax_total"], currency) if Decimal(invoice["tax_total"]) else "Included",
        ))
    if not is_pms and not service_charge_lines and not any(
        charge["mode"] != "do_not_show" for charge in tax_charges
    ):
        amount_rows.append(("-", "0"))
    section_ends.append(len(amount_rows) + 1)
    centered_label_rows.add(len(amount_rows))
    amount_rows.append(
        ("<b>Grand Total</b>", f"<b>{_money(invoice['invoice_total'], currency)}</b>"),
    )
    if is_pms and document_title == "Invoice":
        invoice_total = Decimal(str(invoice["invoice_total"]))
        remaining_balance = Decimal(str(snapshot["remaining_balance"]))
        paid_total = max(invoice_total - remaining_balance, Decimal("0"))
        payment_status = (
            "Paid" if remaining_balance == 0
            else "Partially Paid" if paid_total > 0
            else "Unpaid"
        )
        for row in (
            ("<b>Amount Paid</b>", f"<b>{_money(paid_total, currency)}</b>"),
            ("<b>Payment Status</b>", f"<b>{payment_status}</b>"),
        ):
            centered_label_rows.add(len(amount_rows))
            amount_rows.append(row)
    else:
        for row in (
            ("<b>Amount Paid</b>", f"<b>{_money(snapshot['amount_paid'], currency)}</b>"),
            ("<b>Amount Due</b>", f"<b>{_money(snapshot['remaining_balance'], currency)}</b>"),
        ):
            centered_label_rows.add(len(amount_rows))
            amount_rows.append(row)
    amount_table = Table(
        [[Paragraph("<b>DESCRIPTION</b>", center), Paragraph("<b>AMOUNT</b>", center)]]
        + [
            [
                Paragraph(label, center if index in centered_label_rows else small),
                Paragraph(amount, right),
            ]
            for index, (label, amount) in enumerate(amount_rows)
        ],
        colWidths=[114 * mm, 45 * mm], repeatRows=1,
    )
    amount_table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), pale), ("GRID", (0, 0), (-1, 0), 0.8, border),
        ("BOX", (0, 0), (-1, -1), 0.8, border), ("LINEBEFORE", (1, 0), (1, -1), 0.8, border),
        *(("LINEABOVE", (0, row), (-1, row), 0.8, border) for row in section_ends),
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


def render_receipt_pdf(snapshot):
    return _render_payment_document_pdf(snapshot, "Receipt", snapshot["receipt_number"])


def render_invoice_pdf(snapshot):
    return _render_payment_document_pdf(snapshot, "Invoice", snapshot["invoice_number"])


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
