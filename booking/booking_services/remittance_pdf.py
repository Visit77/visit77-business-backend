from decimal import Decimal
from io import BytesIO
from pathlib import Path
import re
from xml.sax.saxutils import escape
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER, TA_RIGHT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import (
    Image,
    PageBreak,
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont

DOCUMENT_FONT = "NotoSansMyanmar"
DOCUMENT_FONT_BOLD = "NotoSansMyanmar-Bold"
BLUE = colors.HexColor("#1649C5")
DATE_BLUE = colors.HexColor("#AAB9CC")
GRID = colors.HexColor("#7E91AD")
LIGHT_GREY = colors.HexColor("#E8EBEF")
MYANMAR_TEXT_RE = re.compile(r"[\u1000-\u109f\uaa60-\uaa7f\ua9e0-\ua9ff]+")


def _register_document_fonts():
    if DOCUMENT_FONT in pdfmetrics.getRegisteredFontNames():
        return
    font_dir = Path(__file__).resolve().parents[1] / "static" / "booking" / "fonts"
    pdfmetrics.registerFont(TTFont(
        DOCUMENT_FONT, str(font_dir / "NotoSansMyanmar-Regular.ttf"), shapable=True,
    ))
    pdfmetrics.registerFont(TTFont(
        DOCUMENT_FONT_BOLD, str(font_dir / "NotoSansMyanmar-Bold.ttf"), shapable=True,
    ))
    pdfmetrics.registerFontFamily(
        DOCUMENT_FONT,
        normal=DOCUMENT_FONT,
        bold=DOCUMENT_FONT_BOLD,
        italic=DOCUMENT_FONT,
        boldItalic=DOCUMENT_FONT_BOLD,
    )


def _money(value, currency):
    amount = Decimal(str(value or 0))
    return f"{currency} {amount:,.2f}" if amount % 1 else f"{currency} {int(amount):,}"


def _local_datetime(remittance, value):
    try:
        hotel_tz = ZoneInfo(remittance.hotel.timezone or "Asia/Yangon")
    except ZoneInfoNotFoundError:
        hotel_tz = ZoneInfo("Asia/Yangon")
    return value.astimezone(hotel_tz)


def _merchant_details(hotel):
    snapshot = hotel.core_snapshot or {}
    verification = snapshot.get("merchant_verification") or {}
    if not isinstance(verification, dict):
        verification = {}

    def pick(*keys):
        for key in keys:
            value = verification.get(key) or snapshot.get(key)
            if value not in (None, "", []):
                return str(value)
        return "-"

    return [
        ("Hotel Name", hotel.name or "-"),
        ("Contact Phone", hotel.phone or pick("phone", "phone_no")),
        ("Email", pick("email", "business_email", "contact_email")),
        ("Address", hotel.address or pick("address", "address_info")),
        ("Payment Account Number", pick(
            "payment_account_number", "bank_account_number", "account_number",
            "mobile_wallet_account", "mobile_wallet_number",
        )),
        ("Beneficiary Name", pick(
            "beneficiary_name", "account_name", "account_holder_name",
        )),
        ("Bank / Mobile Wallet Name", pick(
            "bank_or_mobile_wallet_name", "bank_name", "wallet_name",
            "payment_provider_name",
        )),
        ("Branch", pick("branch", "bank_branch", "branch_name")),
    ]


def _paragraph(text, style):
    content = escape(str(text or "-")).replace("\n", "<br/>")
    if MYANMAR_TEXT_RE.search(content):
        content = MYANMAR_TEXT_RE.sub(
            lambda match: f'<font name="{DOCUMENT_FONT}">{match.group(0)}</font>',
            content,
        )
        style = style.clone(f"{style.name}-Shaped")
        style.shaping = True
    return Paragraph(content, style)


def _section_header(title, width, style):
    table = Table([[_paragraph(title, style)]], colWidths=[width])
    table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), LIGHT_GREY),
        ("BOX", (0, 0), (-1, -1), 0.8, colors.HexColor("#999999")),
        ("ALIGN", (0, 0), (-1, -1), "CENTER"),
        ("TOPPADDING", (0, 0), (-1, -1), 5),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
    ]))
    return table


def _item_sections(remittance, normal, bold, small):
    items = list(
        remittance.items.select_related("booking", "ledger_entry")
        .order_by("-ledger_entry__effective_at", "-id")
    )
    refunds = {}
    for item in items:
        if item.item_type == "refund":
            refunds[item.ledger_entry.payment_id] = (
                refunds.get(item.ledger_entry.payment_id, Decimal("0"))
                + item.refund_amount
            )

    definitions = (
        ("booking_collection", "Booking Collection"),
        ("held_release", "Release of Held Funds"),
    )
    collection_payment_ids = {
        item.ledger_entry.payment_id
        for item in items
        if item.item_type in {"booking_collection", "held_release"}
    }
    sections = []
    for item_type, label in definitions:
        rows = [item for item in items if item.item_type == item_type]
        if item_type == "booking_collection":
            rows.extend(
                item for item in items
                if item.item_type == "refund"
                and item.ledger_entry.payment_id not in collection_payment_ids
            )
        rows.sort(key=lambda item: item.ledger_entry.effective_at, reverse=True)
        if not rows:
            continue
        sections.extend([Spacer(1, 6 * mm), _paragraph(label, bold), Spacer(1, 2 * mm)])
        data = [[
            _paragraph("Time", bold),
            _paragraph("Booking ID", bold),
            _paragraph("Check-in\nCheck-out", bold),
            _paragraph(f"Amount\n{remittance.currency}", bold),
            _paragraph(f"Refund\n{remittance.currency}", bold),
        ]]
        row_styles = [
            ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#D5DDE8")),
            ("ALIGN", (0, 0), (-1, 0), "CENTER"),
        ]
        current_date = None
        table_row = 1
        for item in rows:
            effective = _local_datetime(remittance, item.ledger_entry.effective_at)
            if effective.date() != current_date:
                current_date = effective.date()
                data.append([_paragraph(current_date.strftime("%d.%m.%Y"), bold), "", "", "", ""])
                row_styles.extend([
                    ("SPAN", (0, table_row), (-1, table_row)),
                    ("BACKGROUND", (0, table_row), (-1, table_row), DATE_BLUE),
                ])
                table_row += 1
            refund = refunds.get(item.ledger_entry.payment_id, Decimal("0"))
            data.append([
                _paragraph(effective.strftime("%H:%M"), small),
                _paragraph(item.booking.booking_code, small),
                _paragraph(
                    f"{item.booking.check_in:%d.%m.%Y}\n{item.booking.check_out:%d.%m.%Y}",
                    small,
                ),
                _paragraph(f"{item.gross_amount:,.0f}", small),
                _paragraph(f"{refund:,.0f}" if refund else "-", small),
            ])
            table_row += 1
        table = Table(data, colWidths=[22 * mm, 44 * mm, 38 * mm, 31 * mm, 31 * mm], repeatRows=1)
        table.setStyle(TableStyle([
            ("FONTNAME", (0, 0), (-1, -1), "Helvetica"),
            ("FONTSIZE", (0, 0), (-1, -1), 8),
            ("TEXTCOLOR", (0, 0), (-1, -1), colors.black),
            ("BOX", (0, 0), (-1, -1), 0.8, GRID),
            ("LINEBELOW", (0, 0), (-1, -1), 0.25, colors.HexColor("#BBBBBB")),
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("TOPPADDING", (0, 0), (-1, -1), 5),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
            *row_styles,
        ]))
        sections.append(table)
    return sections


def render_remittance_statement_pdf(remittance):
    _register_document_fonts()
    buffer = BytesIO()
    doc = SimpleDocTemplate(
        buffer,
        pagesize=A4,
        leftMargin=18 * mm,
        rightMargin=18 * mm,
        topMargin=15 * mm,
        bottomMargin=15 * mm,
        title=f"Remittance {remittance.reference}",
    )
    styles = getSampleStyleSheet()
    normal = ParagraphStyle(
        "RemittanceNormal", parent=styles["BodyText"], fontName="Helvetica",
        fontSize=8.5, leading=12,
    )
    small = ParagraphStyle(
        "RemittanceSmall", parent=normal, fontSize=7.5, leading=10,
    )
    bold = ParagraphStyle(
        "RemittanceBold", parent=normal, fontName="Helvetica-Bold",
    )
    title = ParagraphStyle(
        "RemittanceTitle", parent=bold, fontSize=15, leading=18,
    )
    right = ParagraphStyle("RemittanceRight", parent=normal, alignment=TA_RIGHT)
    center = ParagraphStyle("RemittanceCenter", parent=bold, alignment=TA_CENTER)

    story = []
    image_dir = Path(__file__).resolve().parents[1] / "static" / "booking" / "images"
    logo = Image(str(image_dir / "visit77-icon.png"), width=18 * mm, height=17 * mm)
    issuer = _paragraph(
        "Visit 77 Company Limited\n#10-06, Panchan Tower, Bargayar Street, "
        "San Chaung Tsp., Yangon, Myanmar. 11111\ncontact.myanmar@visit77.com, "
        "(+95) 988 577 0011",
        normal,
    )
    header = Table([[logo, issuer]], colWidths=[26 * mm, 140 * mm])
    header.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("LEFTPADDING", (0, 0), (-1, -1), 0),
        ("RIGHTPADDING", (0, 0), (-1, -1), 2),
    ]))
    story.extend([header, Spacer(1, 3 * mm)])
    rule = Table([[""]], colWidths=[166 * mm], rowHeights=[1.2 * mm])
    rule.setStyle(TableStyle([("BACKGROUND", (0, 0), (-1, -1), BLUE)]))
    story.extend([rule, Spacer(1, 6 * mm)])

    statement_date = remittance.created_at
    status_label = (
        "Pending" if getattr(remittance, "status", None) == "checked"
        else remittance.get_status_display()
    )
    meta = Table([
        [_paragraph("Remittance", title), _paragraph("Statement Date", bold), _paragraph(statement_date.strftime("%d %b %Y"), right)],
        [_paragraph(remittance.reference, bold), _paragraph("Payout Period Covered", bold), _paragraph(
            f"{remittance.period_from:%d %b %Y} - {remittance.period_to:%d %b %Y}", right,
        )],
        ["", _paragraph("Status", bold), _paragraph(status_label, right)],
    ], colWidths=[80 * mm, 42 * mm, 44 * mm])
    meta.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("LEFTPADDING", (0, 0), (-1, -1), 2),
        ("RIGHTPADDING", (0, 0), (-1, -1), 2),
    ]))
    story.extend([meta, Spacer(1, 5 * mm)])

    story.append(_section_header("HOTEL DETAILS", 166 * mm, center))
    hotel_rows = [[_paragraph(label, normal), _paragraph(value, normal)] for label, value in _merchant_details(remittance.hotel)]
    hotel_table = Table(hotel_rows, colWidths=[50 * mm, 116 * mm])
    hotel_table.setStyle(TableStyle([
        ("BOX", (0, 0), (-1, -1), 0.8, colors.HexColor("#999999")),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (-1, -1), 5),
        ("RIGHTPADDING", (0, 0), (-1, -1), 5),
        ("TOPPADDING", (0, 0), (-1, -1), 3),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
    ]))
    story.extend([hotel_table, Spacer(1, 3 * mm)])

    available = remittance.net_remittance_amount - remittance.adjustment_amount
    adjustment_sign = "+" if remittance.adjustment_amount >= 0 else "-"
    summary_rows = [
        ("Total Gross Booking Collection", remittance.gross_booking_amount),
        ("Release of Held Funds", remittance.released_held_funds_amount),
        ("Tax (Excluded from commission)", remittance.tax_amount),
        ("Service Charge (Excluded from commission)", remittance.service_charge_amount),
        ("Visit 77 Commission", remittance.commission_amount),
        ("Refund", remittance.refund_amount),
    ]
    payment_data = [[_paragraph(label, normal), _paragraph(_money(value, remittance.currency), right)] for label, value in summary_rows]
    payment_data.extend([
        [_paragraph("Available", bold), _paragraph(_money(available, remittance.currency), right)],
        [_paragraph("Adjustment", normal), _paragraph(
            f"{adjustment_sign} {_money(abs(remittance.adjustment_amount), remittance.currency)}", right,
        )],
        [_paragraph("Net Payout", bold), _paragraph(_money(remittance.net_remittance_amount, remittance.currency), right)],
    ])
    story.append(_section_header("PAYMENT SUMMARY", 166 * mm, center))
    payment_table = Table(payment_data, colWidths=[105 * mm, 61 * mm])
    payment_table.setStyle(TableStyle([
        ("BOX", (0, 0), (-1, -1), 0.8, colors.HexColor("#999999")),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (-1, -1), 5),
        ("RIGHTPADDING", (0, 0), (-1, -1), 5),
        ("TOPPADDING", (0, 0), (-1, -1), 3),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
    ]))
    story.append(payment_table)
    if remittance.adjustment_note:
        story.extend([
            Spacer(1, 3 * mm),
            _paragraph(f"Adjustment Note: {remittance.adjustment_note}", normal),
        ])

    item_sections = _item_sections(remittance, normal, bold, small)
    if item_sections:
        story.extend([PageBreak(), *item_sections])

    def decorate_page(canvas, document):
        canvas.saveState()
        canvas.setFillColor(colors.white)
        canvas.rect(0, 0, A4[0], A4[1], stroke=0, fill=1)
        canvas.setStrokeColor(BLUE)
        canvas.setLineWidth(0.7)
        canvas.rect(6 * mm, 6 * mm, A4[0] - 12 * mm, A4[1] - 12 * mm)
        canvas.setFont("Helvetica", 7)
        canvas.setFillColor(colors.HexColor("#555555"))
        canvas.drawRightString(A4[0] - 10 * mm, 10 * mm, str(document.page))
        canvas.restoreState()

    doc.build(story, onFirstPage=decorate_page, onLaterPages=decorate_page)
    return buffer.getvalue()
