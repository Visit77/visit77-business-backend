from datetime import timedelta
from decimal import Decimal
from io import BytesIO
from types import SimpleNamespace
from unittest.mock import patch

from botocore.signers import CloudFrontSigner

from django.test import TestCase, override_settings
from django.utils import timezone
from pypdf import PdfReader
from rest_framework.exceptions import ValidationError
from rest_framework.test import APIClient

from booking.authentication import CoreJWTUser
from booking.booking_services.receipt import build_invoice_snapshot, render_invoice_pdf, render_receipt_pdf
from booking.models import Booking, BookingCoupon, CouponCampaign, Hotel, Invoice, OTARevenueLedger, RatePlan, RoomType
from booking.remittance_services import generate_weekly_remittance, sync_payment_ledger
from booking.serializers import BookingSerializer
from booking.services import cancel_booking, create_booking, create_invoice, estimate_booking, record_payment, refund_payment


@override_settings(BOOKING_ADMIN_API_KEY="key", HELD_FUND_CALCULATION=False)
class BookingCouponTests(TestCase):
    def setUp(self):
        self.now = timezone.now()
        self.hotel = Hotel.objects.create(
            core_business_id=7529, name="Coupon Hotel", package="ota", inventory_mode="room_type_count",
            timezone="UTC", core_snapshot={"coupon_locations": [{"country_id": 1, "state_id": 5, "city_id": 68}]},
        )
        self.room = RoomType.objects.create(
            hotel=self.hotel, core_room_type_id=1977, name="Economy", room_price_setup=True,
            room_type_count_inventory=5, local_mmk_price=35000,
        )
        self.plan = RatePlan.objects.create(
            room_type=self.room, code="local", name="Local", guest_market="local", default_price=35000,
            cancellation_policy={"type": "free_full_refund"},
        )
        self.client = APIClient()
        self.client.force_authenticate(CoreJWTUser(id=1, is_superuser=True))
        self.campaign_data = {
            "title": "Visit 77 Hotel Coupon", "type": "booking", "discount_type": "fixed",
            "discount_value": 10000, "currency": "MMK", "quantity_per_user": 2, "country_id": 1,
            "state_ids": [5], "city_ids": [68], "business_ids": [7529],
            "start_at": (self.now - timedelta(days=1)).isoformat(),
            "end_at": (self.now + timedelta(days=10)).isoformat(),
        }
        start = self.now.date() + timedelta(days=2)
        self.data = {
            "core_business_id": 7529, "booked_by_core_user_id": 7,
            "check_in": start, "check_out": start + timedelta(days=2),
            "contact_name": "Guest", "contact_phone": "09",
            "rooms": [{"core_room_type_id": 1977, "rate_plan_id": self.plan.id, "quantity": 1}],
        }

    def campaign(self, **overrides):
        response = self.client.post(
            "/api/v1/admin/coupon-campaigns/", {**self.campaign_data, **overrides}, format="json",
        )
        self.assertEqual(response.status_code, 201, response.data)
        return response.data["data"]["id"]

    def claim(self, campaign_id, user=7):
        client = APIClient()
        client.force_authenticate(CoreJWTUser(id=user))
        return client.post(f"/api/v1/coupon-campaigns/{campaign_id}/claim/")

    def test_admin_campaign_get_resolves_scope_names_in_one_request(self):
        campaign_id = self.campaign()
        names = {
            "countries": {"1": "Myanmar"}, "states": {"5": "Yangon Region"},
            "cities": {"68": "Yangon"}, "businesses": {"7529": "Coupon Hotel"},
        }
        base_url = "/api/v1/admin/coupon-campaigns/"
        for url in (base_url, f"{base_url}{campaign_id}/"):
            with patch("booking.coupon_views.CoreClient.post", return_value=names) as lookup:
                response = self.client.get(url)
            lookup.assert_called_once_with(
                "booking-integrations/coupons/scope-names/",
                json={"countries": [1], "states": [5], "cities": [68], "businesses": [7529]},
            )
            self.assertEqual(response.status_code, 200)
            row = response.data["data"]
            if isinstance(row, list):
                row = row[0]
            self.assertEqual(row["country_name"], "Myanmar")
            self.assertEqual(row["state_names"], ["Yangon Region"])
            self.assertEqual(row["city_names"], ["Yangon"])
            self.assertEqual(row["business_names"], ["Coupon Hotel"])

    def test_admin_campaign_status(self):
        future = self.now + timedelta(days=1)
        active = self.campaign()
        scheduled = self.campaign(publish_at=future.isoformat())
        upcoming = self.campaign(start_at=future.isoformat())
        expired = self.campaign()
        disabled = self.campaign(is_active=False)
        CouponCampaign.objects.filter(pk=expired).update(end_at=self.now - timedelta(seconds=1))

        response = self.client.get("/api/v1/admin/coupon-campaigns/")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            {row["id"]: row["status"] for row in response.data["data"]},
            {active: "active", scheduled: "upcoming", upcoming: "upcoming",
             expired: "expired", disabled: "expired"},
        )

    def test_publish_at_accepts_now_and_scheduled_datetime(self):
        url = "/api/v1/admin/coupon-campaigns/"
        scheduled = self.now + timedelta(days=1)
        campaign_id = self.campaign(publish_at=scheduled.isoformat())
        self.assertEqual(CouponCampaign.objects.get(pk=campaign_id).publish_at, scheduled)

        before = timezone.now()
        response = self.client.patch(f"{url}{campaign_id}/", {"publish_at": "now"}, format="multipart")
        self.assertEqual(response.status_code, 200, response.data)
        self.assertLessEqual(before, CouponCampaign.objects.get(pk=campaign_id).publish_at)
        self.assertLessEqual(CouponCampaign.objects.get(pk=campaign_id).publish_at, timezone.now())

        now_id = self.campaign(publish_at="now")
        self.assertLessEqual(before, CouponCampaign.objects.get(pk=now_id).publish_at)

    def test_my_coupons_filters_by_core_business_id(self):
        general = self.campaign(state_ids=[], city_ids=[], business_ids=[], quantity_per_user=1)
        hotel_only = self.campaign(business_ids=[self.hotel.core_business_id], quantity_per_user=1)
        other_hotel = self.campaign(business_ids=[999], quantity_per_user=1)
        expired = self.campaign(quantity_per_user=1)
        for campaign_id in (general, hotel_only, other_hotel, expired):
            self.claim(campaign_id)
        CouponCampaign.objects.filter(pk=expired).update(end_at=self.now - timedelta(seconds=1))

        client = APIClient()
        client.force_authenticate(CoreJWTUser(id=7))
        url = "/api/v1/me/coupons/"
        for status in ({}, {"status": "available"}):
            response = client.get(url, {"business_id": self.hotel.core_business_id, **status})
            self.assertEqual(response.status_code, 200, response.data)
            self.assertEqual({ticket["campaign"]["id"] for ticket in response.data["data"]}, {general, hotel_only})
        self.assertEqual(client.get(url, {"business_id": "invalid"}).status_code, 400)
        self.assertEqual(client.get(url, {"business_id": 999}).status_code, 404)

    def test_my_coupons_filters_by_status_for_current_user(self):
        available = self.campaign(quantity_per_user=1)
        expired = self.campaign(quantity_per_user=1)
        self.claim(available)
        self.claim(expired)
        self.claim(available, user=8)
        CouponCampaign.objects.filter(pk=expired).update(end_at=self.now - timedelta(seconds=1))

        client = APIClient()
        client.force_authenticate(CoreJWTUser(id=7))
        url = "/api/v1/me/coupons/"
        for status, expected_campaign in (("available", available), ("expired", expired)):
            response = client.get(url, {"status": status})
            self.assertEqual(response.status_code, 200)
            self.assertEqual(
                [(ticket["status"], ticket["campaign"]["id"]) for ticket in response.data["data"]],
                [(status, expected_campaign)],
            )
        self.assertEqual(len(client.get(url).data["data"]), 2)
        self.assertEqual(client.get(url, {"status": "invalid"}).status_code, 400)

    def test_campaign_category_is_returned_without_filtering_hotel_discovery(self):
        campaign_id = self.campaign(category_id=4)
        url = "/api/v1/coupon-campaigns/"
        filters = {"country_id": 1, "business_id": self.hotel.core_business_id}
        client = APIClient()
        campaigns = client.get(url, filters).data["data"]
        self.assertEqual([(row["id"], row["category_id"]) for row in campaigns], [(campaign_id, 4)])

    def test_campaign_image_responses_sign_existing_cloudfront_paths(self):
        campaign_id = self.campaign()
        banner = "booking/coupons/banners/example.jpg"
        popup = "booking/coupons/popups/example.jpg"
        CouponCampaign.objects.filter(pk=campaign_id).update(banner_image=banner, popup_image=popup)
        storage = CouponCampaign._meta.get_field("banner_image").storage
        signer = CloudFrontSigner("TESTKEY", lambda message: b"test-signature")
        with (
            patch.object(storage, "custom_domain", "media.example.com", create=True),
            patch.object(storage, "url", side_effect=lambda name: f"https://media.example.com/{name}"),
            patch("booking.storage.get_private_document_storage",
                  return_value=SimpleNamespace(cloudfront_signer=signer, querystring_expire=900)),
        ):
            admin = self.client.get(f"/api/v1/admin/coupon-campaigns/{campaign_id}/").data["data"]
            discovery = APIClient().get("/api/v1/coupon-campaigns/", {"country_id": 1}).data["data"][0]
        for field, path in (("banner_image", banner), ("popup_image", popup)):
            self.assertNotIn(field, admin)
            self.assertNotIn(field, discovery)
            for url in (admin[f"{field}_url"], discovery[f"{field}_url"]):
                self.assertTrue(url.startswith(f"https://media.example.com/{path}?Expires="), url)
                self.assertIn("Signature=", url)
                self.assertIn("Key-Pair-Id=TESTKEY", url)

    def test_claim_and_coupon_invoice_payment_remittance_and_refund(self):
        campaign_id = self.campaign()
        claim = self.claim(campaign_id)
        self.assertEqual(claim.status_code, 200, claim.data)
        tickets = claim.data["data"]
        self.assertEqual(len(tickets), 2)
        self.assertNotEqual(tickets[0]["code"], tickets[1]["code"])
        duplicate_claim = self.claim(campaign_id)
        self.assertEqual(duplicate_claim.status_code, 400, duplicate_claim.data)
        self.assertIn("Already claimed.", str(duplicate_claim.data))
        self.assertEqual(BookingCoupon.objects.filter(campaign_id=campaign_id, user_id=7).count(), 2)

        data = {**self.data, "coupon_code": tickets[0]["code"].lower()}
        estimate = estimate_booking(data)
        self.assertEqual(estimate["grand_total"], Decimal("70000"))
        self.assertEqual(estimate["payable_total"], Decimal("60000"))
        self.assertNotIn("coupon_discount_amount", estimate)
        self.assertFalse(any(item["type"] == "visit77_coupon" for item in estimate["summary_items"]))
        with self.assertRaises(ValidationError):
            estimate_booking({**data, "booked_by_core_user_id": 8})
        booking, _ = create_booking(data, idempotency_key="coupon-test")
        booking_data = BookingSerializer(booking).data
        self.assertEqual(booking_data["grand_total"], "70000.00")
        self.assertNotIn("coupon_discount_amount", booking_data)
        self.assertEqual(create_booking(data, idempotency_key="coupon-test")[0].pk, booking.pk)
        with self.assertRaises(ValidationError):
            create_booking(data)
        invoice = booking.invoices.get()
        self.assertEqual((invoice.total, invoice.coupon_discount_amount, invoice.discount_total), (60000, 10000, 0))
        payment = record_payment(booking, {"provider": "cash", "amount": Decimal("60000")})
        self.assertEqual(payment.amount, 60000)
        self.assertIsNotNone(BookingCoupon.objects.get(pk=tickets[0]["id"]).used_at)
        for pdf in (render_invoice_pdf(build_invoice_snapshot(invoice)), render_receipt_pdf(payment.receipt_snapshot)):
            text = "".join(page.extract_text() for page in PdfReader(BytesIO(pdf)).pages)
            grand_total = text.index("Grand Total")
            coupon = text.index("Visit 77 Coupon Discount", grand_total)
            amount_paid = text.index("Amount Paid", coupon)
            self.assertIn("MMK 70,000", text[grand_total:coupon])
            self.assertIn("MMK 10,000", text[coupon:amount_paid])
            self.assertIn("MMK 60,000", text[amount_paid:])

        gross = payment.ota_revenue_ledger.get(event_type="booking_collection")
        self.assertEqual(gross.gross_amount, 70000)
        self.assertEqual(Decimal(gross.metadata["coupon_subsidy_amount"]), 10000)
        revenue = APIClient().get(
            "/api/v1/admin/ota-revenue/",
            HTTP_X_BOOKING_ADMIN_KEY="key", HTTP_X_BOOKING_BUSINESS_ID=str(self.hotel.core_business_id),
        )
        self.assertEqual(revenue.status_code, 200, revenue.data)
        revenue_data = revenue.data["data"]
        self.assertEqual(Decimal(str(revenue_data["records"][0]["gross_amount"])), 70000)
        for summary in (revenue_data["records"][0], revenue_data["totals"]):
            self.assertNotIn("coupon_subsidy_amount", summary)
            self.assertNotIn("coupon_subsidy_reversal_amount", summary)
        statement = generate_weekly_remittance(self.hotel, self.now.date(), self.now.date(), accruing=True)
        self.assertEqual(statement.gross_booking_amount, 70000)
        self.assertEqual(statement.net_remittance_amount, 66500)

        for _ in range(2):
            payment = refund_payment(payment, Decimal("30000"))
        sync_payment_ledger(payment)
        refunds = list(payment.ota_revenue_ledger.filter(event_type="refund"))
        self.assertEqual([row.refund_amount for row in refunds], [35000, 35000])
        self.assertTrue(all(Decimal(row.metadata["coupon_subsidy_reversal_amount"]) == 5000 for row in refunds))

    def test_targeted_grant_is_private_and_scope_checks_apply_at_checkout(self):
        campaign_id = self.campaign(audience="targeted", user_ids=[7])
        self.assertEqual(self.claim(campaign_id, user=8).status_code, 400)
        coupon = BookingCoupon.objects.filter(campaign_id=campaign_id, user_id=7).first()
        self.assertIsNotNone(coupon)
        client = APIClient()
        payload = {"business_ids": [7529], "core_user_id": 7}
        url = "/api/v1/admin/directory/minimum-room-prices/"
        response = client.post(url, payload, format="json", HTTP_X_BOOKING_ADMIN_KEY="key")
        row = response.data["data"]["minimum_rooms"]["7529"]
        self.assertEqual(row["available_coupons"][0]["remaining_quantity"], 2)
        response = client.post(url, {**payload, "core_user_id": 8}, format="json", HTTP_X_BOOKING_ADMIN_KEY="key")
        self.assertEqual(response.data["data"]["minimum_rooms"]["7529"]["available_coupons"], [])
        self.hotel.core_snapshot["coupon_locations"][0]["city_id"] = 99
        self.hotel.save(update_fields=["core_snapshot"])
        with self.assertRaises(ValidationError):
            estimate_booking({**self.data, "coupon_id": coupon.id})

    def test_fully_funded_booking_and_cancellation_reverse_subsidy(self):
        campaign_id = self.campaign(discount_type="percentage", discount_value=100)
        coupon = self.claim(campaign_id).data["data"][0]
        booking, _ = create_booking({**self.data, "coupon_id": coupon["id"]})
        self.assertEqual((booking.grand_total, booking.status), (0, Booking.Status.CONFIRMED))
        payment = booking.payments.get()
        self.assertEqual(payment.amount, 0)
        self.assertEqual(payment.ota_revenue_ledger.get(event_type="booking_collection").gross_amount, 70000)
        cancel_booking(booking)
        self.assertEqual(payment.ota_revenue_ledger.get(event_type=OTARevenueLedger.EventType.REFUND).refund_amount, 70000)

    def test_cancel_unpaid_reservation_releases_ticket_and_pms_invoice_has_no_coupon(self):
        coupon = self.claim(self.campaign()).data["data"][0]
        booking, _ = create_booking({**self.data, "coupon_id": coupon["id"]})
        cancel_booking(booking)
        ticket = BookingCoupon.objects.get(pk=coupon["id"])
        self.assertIsNone(ticket.booking_id)
        booking, _ = create_booking({**self.data, "coupon_id": coupon["id"]})
        extra = create_invoice(booking, Invoice.Type.EXTRA_SERVICE, [{"description": "Extra", "unit_price": 500}])
        self.assertEqual((extra.total, extra.coupon_discount_amount), (500, 0))
