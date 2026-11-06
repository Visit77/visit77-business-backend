from datetime import date
from unittest.mock import patch

from django.test import TestCase, override_settings
from rest_framework.test import APIClient
from rest_framework_simplejwt.backends import TokenBackend

from booking.models import Booking, Hotel, OTARemittanceActor


@override_settings(CORE_JWT_SIGNING_KEY="finance-test-key", CORE_JWT_ALGORITHM="HS256")
class RemittanceFinanceAccessTests(TestCase):
    def setUp(self):
        from time import time
        token = TokenBackend("HS256", signing_key="finance-test-key").encode({
            "user_id": 42, "token_type": "access", "is_staff": True,
            "is_superuser": False, "exp": int(time()) + 300,
        })
        self.client = APIClient()
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {token}")

    @patch("booking.integrations.core.CoreClient.get", return_value={"finance_access": True})
    def test_finance_can_read_remittance_but_cannot_create_passcodes_or_change_schedule(self, core):
        for route in (
            "remittances/", "remittances/history/", "remittances/payment-list/",
            "remittances/payment-list/2026-11-06/",
            "remittances/payment-list/2026-11-06/export/", "remittance-actors/",
        ):
            response = self.client.get(f"/api/v1/superadmin/{route}")
            self.assertEqual(response.status_code, 200, route)
            response.close()
        core.assert_called_with("accounts/finance-access/")
        for method, action in (
            ("post", "check"), ("patch", "adjustment"), ("delete", "adjustment"),
            ("patch", "receipt"), ("post", "pay"),
        ):
            response = getattr(self.client, method)(f"/api/v1/superadmin/remittances/999/{action}/")
            self.assertEqual(response.status_code, 400, action)  # Missing remittance, after authorization.
        self.assertEqual(self.client.post("/api/v1/superadmin/remittance-actors/", {
            "name": "Self-assigned payer", "passcode": "123456", "access_rights": ["payer"],
        }, format="json").status_code, 403)
        self.assertFalse(OTARemittanceActor.objects.exists())
        self.assertEqual(self.client.post(
            "/api/v1/superadmin/hotels/1/remittance-payout-option/",
            {"payout_option": "weekly"}, format="json",
        ).status_code, 403)

        core.return_value = {"finance_access": False}
        self.assertEqual(self.client.get("/api/v1/superadmin/remittances/payment-list/").status_code, 403)

    @patch("booking.integrations.core.CoreClient.get", return_value={"finance_access": True})
    def test_finance_ota_booking_access_is_read_only(self, core):
        hotel = Hotel.objects.create(core_business_id=77, name="Test Hotel", package="ota")
        booking = Booking.objects.create(
            hotel=hotel, reference="FINANCE-OTA", status="confirmed",
            check_in=date(2026, 11, 6), check_out=date(2026, 11, 7), contact_name="Test Guest",
        )
        base = "/api/v1/superadmin/"
        follow_up = f"ota-bookings/{booking.pk}/follow-up/"
        for route in (
            "ota-bookings/?offset=1&limit=20", f"ota-bookings/{booking.pk}/",
            follow_up, "ota-booking-dashboard/",
        ):
            self.assertEqual(self.client.get(base + route).status_code, 200, route)
        self.assertEqual(self.client.patch(base + follow_up, {"note": "Changed"}, format="json").status_code, 403)
        core.return_value = {"finance_access": False}
        self.assertEqual(self.client.get(base + follow_up).status_code, 403)
