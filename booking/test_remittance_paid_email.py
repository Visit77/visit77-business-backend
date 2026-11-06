from datetime import date
from types import SimpleNamespace
from unittest.mock import patch

from django.contrib.auth.hashers import make_password
from django.core import mail
from django.test import TestCase, override_settings
from rest_framework.test import APIClient

from booking.models import Hotel, OTARemittance, OTARemittanceActor
from booking.tasks import send_remittance_paid_email_task


@override_settings(EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend")
class RemittancePaidEmailTests(TestCase):
    def test_pay_queues_merchant_email_after_commit_with_statement_pdf(self):
        hotel = Hotel.objects.create(
            core_business_id=7532, name="Sample Hotel",
            core_snapshot={
                "booking_notification_email": "merchant@example.com",
                "email": "general@example.com",
            },
        )
        record = OTARemittance.objects.create(
            hotel=hotel, reference="V77-PAY-26-000059",
            period_from=date(2026, 11, 4), period_to=date(2026, 11, 5),
            status=OTARemittance.Status.CHECKED,
            gross_booking_amount=200000, commission_amount=10000, net_remittance_amount=190000,
        )
        payer = OTARemittanceActor.objects.create(
            scope=OTARemittanceActor.Scope.ADMIN, name="Test Payer",
            passcode_hash=make_password("1234"), access_rights=["payer"],
        )
        client = APIClient()
        client.force_authenticate(user=SimpleNamespace(is_authenticated=True, is_superuser=True))
        url = f"/api/v1/superadmin/remittances/{record.id}/pay/"
        body = {"actor_id": str(payer.id), "passcode": "1234", "payment_reference": "BANK-001"}

        with patch.object(send_remittance_paid_email_task, "delay",
                          side_effect=send_remittance_paid_email_task.run) as queue:
            with self.captureOnCommitCallbacks(execute=True):
                rejected = client.post(url, {**body, "passcode": "4321"}, format="json")
                self.assertEqual(rejected.status_code, 400)
                queue.assert_not_called()

                response = client.post(url, body, format="json")
                self.assertEqual(response.status_code, 200, response.data)
                queue.assert_not_called()
            queue.assert_called_once()

        record.refresh_from_db()
        self.assertEqual(record.status, OTARemittance.Status.PAID)
        self.assertEqual(len(mail.outbox), 1)
        message = mail.outbox[0]
        self.assertEqual(message.to, ["merchant@example.com"])
        self.assertIn("V77-PAY-26-000059", message.subject)
        self.assertIn("Net payout: MMK 190,000.00", message.body)
        self.assertIn("BANK-001", message.body)
        filename, pdf, mime_type = message.attachments[0]
        self.assertEqual(filename, f"remittance-{record.id}.pdf")
        self.assertEqual(mime_type, "application/pdf")
        self.assertTrue(pdf.startswith(b"%PDF"))
        self.assertFalse(send_remittance_paid_email_task.run(record.id, record.paid_at.isoformat()))
        self.assertEqual(len(mail.outbox), 1)
