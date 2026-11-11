from datetime import timedelta
from io import StringIO

from django.core.management import call_command
from django.test import TestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIClient

from booking.models import Booking, Hotel, Invoice, OTARevenueLedger, Payment
from booking.remittance_services import generate_weekly_remittance, sync_payment_ledger


@override_settings(BOOKING_ADMIN_API_KEY="key")
class HeldFundCalculationTests(TestCase):
    def setUp(self):
        self.hotel = Hotel.objects.create(core_business_id=77, name="Hotel", package="ota", timezone="UTC")
        self.today = timezone.now().date()
        self.booking = Booking.objects.create(
            hotel=self.hotel, reference="HELD-TEST", source=Booking.Source.OTA,
            check_in=self.today + timedelta(days=5), check_out=self.today + timedelta(days=6),
            contact_name="Guest", contact_phone="09", currency="MMK", grand_total=1000,
            cancellation_policy_snapshot={"1": {"type": "full_refund"}},
        )
        invoice = Invoice.objects.create(
            booking=self.booking, invoice_type=Invoice.Type.ROOM_BOOKING,
            charge_scope=Invoice.ChargeScope.OTA, currency="MMK", total=1000,
        )
        self.payment = Payment.objects.create(
            booking=self.booking, invoice=invoice, provider="cash", amount=1000,
            status=Payment.Status.PAID, paid_at=timezone.now(), currency="MMK",
        )

    @override_settings(HELD_FUND_CALCULATION=False)
    def test_refundable_advance_payment_is_available_in_ledger_and_api(self):
        rows = sync_payment_ledger(self.payment)
        self.assertEqual([row.event_type for row in rows], ["booking_collection"])
        self.booking.refresh_from_db()
        self.assertEqual(self.booking.settlement_status, Booking.SettlementStatus.AVAILABLE)
        response = APIClient().get(
            "/api/v1/admin/ota-revenue/",
            HTTP_X_BOOKING_ADMIN_KEY="key", HTTP_X_BOOKING_BUSINESS_ID="77",
        )
        self.assertEqual(response.status_code, 200, response.data)
        row = response.data["data"]["records"][0]
        self.assertEqual(row["status"], "available")
        self.assertEqual(row["available_amount"], 1000)
        self.assertIsNone(row["held_release_at"])
        with override_settings(HELD_FUND_CALCULATION=True):
            sync_payment_ledger(self.payment)
        self.assertEqual(self.payment.ota_revenue_ledger.count(), 1)

    @override_settings(HELD_FUND_CALCULATION=True)
    def test_release_command_previews_and_releases_once_without_backdating(self):
        held, = sync_payment_ledger(self.payment)
        self.assertEqual(held.event_type, "held")
        call_command("release_held_funds", business_id=77, stdout=StringIO())
        self.assertEqual(self.payment.ota_revenue_ledger.count(), 1)

        for _ in range(2):
            call_command("release_held_funds", business_id=77, apply=True, stdout=StringIO())
        released = self.payment.ota_revenue_ledger.get(event_type="held_release")
        self.assertEqual(released.effective_at.date(), self.today)
        self.assertEqual(released.gross_amount, held.gross_amount)
        self.booking.refresh_from_db()
        self.assertEqual(self.booking.settlement_status, Booking.SettlementStatus.AVAILABLE)

        with override_settings(HELD_FUND_CALCULATION=False):
            sync_payment_ledger(self.payment)
        sync_payment_ledger(self.payment)
        released.refresh_from_db()
        self.assertEqual(released.effective_at.date(), self.today)
        self.assertFalse(self.payment.ota_revenue_ledger.filter(event_type="booking_collection").exists())
        statement = generate_weekly_remittance(self.hotel, self.today, self.today, accruing=True)
        self.assertEqual(statement.released_held_funds_amount, 1000)
        self.assertEqual(statement.net_remittance_amount, 950)
        Booking.objects.filter(pk=self.booking.pk).update(settlement_status=Booking.SettlementStatus.PAID)
        sync_payment_ledger(self.payment)
        self.booking.refresh_from_db()
        self.assertEqual(self.booking.settlement_status, Booking.SettlementStatus.PAID)

    def test_disabling_flag_releases_an_existing_hold_on_next_sync(self):
        with override_settings(HELD_FUND_CALCULATION=True):
            sync_payment_ledger(self.payment)
        with override_settings(HELD_FUND_CALCULATION=False):
            sync_payment_ledger(self.payment)
            sync_payment_ledger(self.payment)
        self.assertEqual(set(self.payment.ota_revenue_ledger.values_list("event_type", flat=True)),
                         {"held", "held_release"})
