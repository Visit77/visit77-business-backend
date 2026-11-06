from datetime import date, timedelta
from io import BytesIO
from types import SimpleNamespace
from xml.etree import ElementTree
from zipfile import ZipFile

from django.test import TestCase
from django.urls import resolve
from rest_framework.test import APIRequestFactory, force_authenticate

from booking.models import Hotel, OTARemittance


class RemittancePaymentDateTests(TestCase):
    base_path = "/api/v1/superadmin/remittances/payment-list/"

    def get(self, suffix="", **params):
        path = self.base_path + suffix
        request = APIRequestFactory().get(path, params)
        force_authenticate(request, user=SimpleNamespace(is_authenticated=True, is_superuser=True))
        match = resolve(path)
        return match.func(request, **match.kwargs)

    def test_date_pages_preview_detail_and_export_share_eligible_records(self):
        payout_date = date(2026, 11, 6)
        hotels = [
            Hotel.objects.create(
                core_business_id=100 + i, name=f"Hotel {i}", package="ota_pms" if i % 2 else "ota",
                core_snapshot={"merchant_verification": {"payment_account_number": "001234"}},
            )
            for i in range(7)
        ]

        def statement(hotel, payout, **overrides):
            values = dict(status="pending", gross_booking_amount=100, net_remittance_amount=95)
            values.update(overrides)
            end = payout - timedelta(days=1)
            return OTARemittance.objects.create(hotel=hotel, period_from=end, period_to=end, **values)

        for hotel in hotels[:6]:
            statement(hotel, payout_date)
        statement(hotels[6], payout_date, gross_booking_amount=0)
        for i in range(1, 6):
            statement(hotels[0], payout_date - timedelta(days=i), status="paid")
        statement(hotels[1], payout_date - timedelta(days=1), net_remittance_amount=0)
        statement(hotels[2], payout_date - timedelta(days=1), net_remittance_amount=-1)
        statement(hotels[3], payout_date - timedelta(days=1), status="failed")
        statement(hotels[4], payout_date + timedelta(days=1), status="accruing")
        for index, package in enumerate(("pms", "free")):
            excluded = Hotel.objects.create(core_business_id=200 + index, name=package, package=package)
            statement(excluded, payout_date)

        response = self.get(offset=1, limit=5)
        self.assertEqual(response.status_code, 200)
        data = response.data["data"]
        self.assertEqual(data["date_count"], 6)
        self.assertEqual(len(data["results"]), 5)
        preview = data["results"][0]
        self.assertEqual(preview["date"], payout_date)
        self.assertEqual(len(preview["hotels"]), 5)
        self.assertEqual(preview["hotel_count"], 6)
        self.assertTrue(preview["has_more_hotels"])
        self.assertEqual(preview["total_amounts"], {"MMK": 570})
        self.assertEqual(data["results"][1]["hotel_count"], 1)
        self.assertEqual(len(self.get(offset=2, limit=5).data["data"]["results"]), 1)

        detail = self.get(f"{payout_date}/").data["data"]
        self.assertEqual([row["hotel_id"] for row in detail["hotels"]], [hotel.id for hotel in hotels[:6]])
        self.assertEqual(detail["total_amounts"], preview["total_amounts"])
        self.assertEqual(self.get(f"{payout_date}/", payment_filter="no_received").data["data"]["hotels"], [])

        response = self.get(f"{payout_date}/export/")
        self.assertEqual(response.status_code, 200)
        self.assertIn(f"remittance-payments-{payout_date}.xlsx", response["Content-Disposition"])
        with ZipFile(BytesIO(b"".join(response.streaming_content))) as workbook:
            self.assertIsNone(workbook.testzip())
            sheet = ElementTree.fromstring(workbook.read("xl/worksheets/sheet1.xml"))
        rows = sheet.findall(".//{*}row")
        self.assertEqual(len(rows), 7)
        self.assertEqual(
            [cell.text for cell in rows[0].findall(".//{*}t")],
            ["No.", "Hotel Name", "Net Amount", "Account Number", "Beneficiary Name",
             "Bank/Mobile Wallet Name", "Branch", "Status", "Receipt"],
        )
        self.assertEqual(rows[1].find("./{*}c[@r='D2']/{*}is/{*}t").text, "001234")
        self.assertEqual(rows[1].find("./{*}c[@r='C2']/{*}v").text, "95.00")

    def test_invalid_detail_date_returns_400(self):
        self.assertEqual(self.get("2026-02-30/").status_code, 400)
