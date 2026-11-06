from decimal import Decimal
from io import BytesIO
from zipfile import ZIP_DEFLATED, ZipFile
from xml.etree.ElementTree import Element, SubElement, tostring
import re

from django.http import FileResponse


SHEET_NS = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
REL_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
PACKAGE_REL_NS = "http://schemas.openxmlformats.org/package/2006/relationships"
INVALID_XML_CHARS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\ufffe\uffff]")
HEADERS = (
    "No.", "Hotel Name", "Net Amount", "Account Number", "Beneficiary Name",
    "Bank/Mobile Wallet Name", "Branch", "Status", "Receipt",
)


def remittance_payment_workbook(payout_date, hotels):
    rows = (
        (
            number, hotel["hotel_name"], hotel["net_amount"], hotel["account_number"],
            hotel["beneficiary_name"], hotel["bank_or_mobile_wallet_name"], hotel["branch"],
            hotel["status"].replace("_", " ").title(), hotel["payment_receipt_url"],
        )
        for number, hotel in enumerate(hotels, 1)
    )
    return _workbook_response(
        f"remittance-payments-{payout_date}.xlsx", "Payment List", HEADERS, rows,
        (7, 34, 18, 24, 28, 29, 20, 17, 45),
    )


def remittance_detail_workbook(record, sections):
    currency = record.currency
    rows = (
        (section["label"], item["effective_date"], item["effective_time"], item["booking_code"],
         item["check_in"], item["check_out"], item["amount"], item["refund_amount"])
        for section in sections
        for group in section["date_groups"]
        for item in group["items"]
    )
    return _workbook_response(
        f"remittance-{record.id}.xlsx", "Remittance Details",
        ("Category", "Date", "Time", "Booking ID", "Check-In", "Check-Out",
         f"Amount ({currency})", f"Refund ({currency})"),
        rows, (29, 16, 12, 27, 16, 16, 20, 20),
        title=f"Remittance Details - {record.reference or record.id}",
    )


def _workbook_response(filename, sheet_name, headers, rows, widths, title=None):
    sheet = Element("worksheet", xmlns=SHEET_NS)
    views = SubElement(sheet, "sheetViews")
    view = SubElement(views, "sheetView", workbookViewId="0")
    header_row = 2 if title else 1
    SubElement(view, "pane", ySplit=str(header_row), topLeftCell=f"A{header_row + 1}", activePane="bottomLeft", state="frozen")
    columns = SubElement(sheet, "cols")
    for index, width in enumerate(widths, 1):
        SubElement(columns, "col", min=str(index), max=str(index), width=str(width), customWidth="1")
    sheet_data = SubElement(sheet, "sheetData")

    def append_row(index, values):
        row = SubElement(sheet_data, "row", r=str(index))
        for column, value in zip("ABCDEFGHI", values):
            cell = SubElement(row, "c", r=f"{column}{index}")
            if isinstance(value, (int, Decimal)):
                SubElement(cell, "v").text = str(value)
            else:
                cell.set("t", "inlineStr")
                inline = SubElement(cell, "is")
                SubElement(inline, "t", {"xml:space": "preserve"}).text = INVALID_XML_CHARS.sub("", str(value or ""))

    if title:
        append_row(1, (title,))
    append_row(header_row, headers)
    for index, values in enumerate(rows, header_row + 1):
        append_row(index, values)

    output = BytesIO()
    with ZipFile(output, "w", ZIP_DEFLATED) as workbook:
        workbook.writestr("[Content_Types].xml", """
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
  <Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
  <Default Extension="xml" ContentType="application/xml"/>
  <Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>
  <Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>
</Types>""".strip())
        workbook.writestr("_rels/.rels", f"""
<Relationships xmlns="{PACKAGE_REL_NS}">
  <Relationship Id="rId1" Type="{REL_NS}/officeDocument" Target="xl/workbook.xml"/>
</Relationships>""".strip())
        workbook.writestr("xl/workbook.xml", f"""
<workbook xmlns="{SHEET_NS}" xmlns:r="{REL_NS}">
  <sheets><sheet name="{sheet_name}" sheetId="1" r:id="rId1"/></sheets>
</workbook>""".strip())
        workbook.writestr("xl/_rels/workbook.xml.rels", f"""
<Relationships xmlns="{PACKAGE_REL_NS}">
  <Relationship Id="rId1" Type="{REL_NS}/worksheet" Target="worksheets/sheet1.xml"/>
</Relationships>""".strip())
        workbook.writestr("xl/worksheets/sheet1.xml", tostring(sheet, encoding="utf-8", xml_declaration=True))
    output.seek(0)
    return FileResponse(
        output, as_attachment=True, filename=filename,
        content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )
