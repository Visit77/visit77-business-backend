from datetime import date, timedelta
from decimal import Decimal
from io import StringIO
from unittest.mock import patch

from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase
from django.utils import timezone

from booking.models import Hotel, OTACommissionRate, OTARemittance, OTARemittanceAuditLog
from booking.remittance_services import recalculate_unpaid_remittance


class RecalculateUnpaidRemittancesCommandTests(TestCase):
    def setUp(self):
        self.hotel = Hotel.objects.create(core_business_id=77, name='Hotel', package='ota')
        self.sequence = 0

    def record(self, **changes):
        self.sequence += 1
        start = date(2026, 11, 2) + timedelta(days=7 * self.sequence)
        data = dict(hotel=self.hotel, period_from=start, period_to=start + timedelta(days=6),
            reference=f'RECALC-{self.sequence}', gross_booking_amount=256, tax_amount=6,
            service_charge_amount=50, commission_rate=5, commission_amount=10,
            net_remittance_amount=246, commission_excludes_service_charge=True, status='pending')
        data.update(changes)
        return OTARemittance.objects.create(**data)

    def test_dry_run_does_not_write_amounts_approval_or_audit(self):
        row = self.record(status='checked', checked_at=timezone.now())
        output = StringIO()
        call_command('recalculate_unpaid_remittances', dry_run=True, stdout=output)
        self.assertIn('WOULD UPDATE', output.getvalue())
        self.assertIn('10.00 -> 12.50', output.getvalue())
        self.assertIn('checked -> pending', output.getvalue())
        row.refresh_from_db()
        self.assertEqual(row.commission_amount, Decimal('10'))
        self.assertEqual(row.status, 'checked')
        self.assertIsNotNone(row.checked_at)
        self.assertTrue(row.commission_excludes_service_charge)
        self.assertEqual(OTARemittanceAuditLog.objects.count(), 0)

    def test_recalculates_all_unpaid_statuses_but_never_paid_or_received(self):
        rows = [self.record(status=status, **({'checked_at': timezone.now()} if status == 'checked' else {}))
                for status in ('pending', 'accruing', 'checked', 'failed')]
        protected = [self.record(status='paid'), self.record(status='received'), self.record(paid_at=timezone.now()), self.record(received_at=timezone.now())]
        call_command('recalculate_unpaid_remittances', stdout=StringIO())
        for row in rows:
            row.refresh_from_db()
            self.assertEqual(row.commission_amount, Decimal('12.50'))
            self.assertEqual(row.net_remittance_amount, Decimal('243.50'))
            self.assertFalse(row.commission_excludes_service_charge)
            self.assertIsNone(row.checked_at)
            audit = row.audit_logs.get(action='commission_recalculated')
            self.assertEqual(audit.metadata['previous_commission'], '10.00')
        self.assertEqual([row.status for row in rows], ['pending', 'accruing', 'pending', 'failed'])
        for row in protected:
            row.refresh_from_db()
            self.assertEqual(row.commission_amount, Decimal('10'))
            self.assertTrue(row.commission_excludes_service_charge)
            self.assertFalse(row.audit_logs.exists())
        output = StringIO()
        call_command('recalculate_unpaid_remittances', stdout=output)
        self.assertIn('updated=0, unchanged=4', output.getvalue())
        self.assertEqual(OTARemittanceAuditLog.objects.count(), 4)

    def test_business_and_id_filters_preserve_rate_refund_adjustment_and_other_records(self):
        other_hotel = Hotel.objects.create(core_business_id=88, name='Other')
        row = self.record(commission_rate=Decimal('7.5'), refund_amount=11, adjustment_amount=3)
        unselected = self.record()
        other = self.record(hotel=other_hotel)
        OTACommissionRate.objects.create(package='ota', rate=99)
        call_command('recalculate_unpaid_remittances', business_id=77, remittance_id=[row.pk, other.pk], stdout=StringIO())
        row.refresh_from_db()
        self.assertEqual(row.commission_amount, Decimal('18.75'))
        self.assertEqual(row.net_remittance_amount, Decimal('229.25'))
        self.assertEqual(row.refund_amount, Decimal('11'))
        self.assertEqual(row.adjustment_amount, Decimal('3'))
        self.assertEqual(row.commission_rate, Decimal('7.5'))
        for untouched in (unselected, other):
            untouched.refresh_from_db()
            self.assertEqual(untouched.commission_amount, Decimal('10'))
        with self.assertRaises(CommandError):
            call_command('recalculate_unpaid_remittances', business_id=-1, stdout=StringIO())
        with self.assertRaises(CommandError):
            call_command('recalculate_unpaid_remittances', remittance_id=[0], stdout=StringIO())

    def test_payment_that_finishes_after_selection_is_skipped_under_lock(self):
        row = self.record()
        def pay_then_recalculate(record_id, **kwargs):
            OTARemittance.objects.filter(pk=record_id).update(status='paid', paid_at=timezone.now())
            return recalculate_unpaid_remittance(record_id, **kwargs)
        output = StringIO()
        with patch('booking.management.commands.recalculate_unpaid_remittances.recalculate_unpaid_remittance', side_effect=pay_then_recalculate):
            call_command('recalculate_unpaid_remittances', stdout=output)
        row.refresh_from_db()
        self.assertEqual(row.status, 'paid')
        self.assertEqual(row.commission_amount, Decimal('10'))
        self.assertEqual(OTARemittanceAuditLog.objects.count(), 0)
        self.assertIn('skipped paid/received=1', output.getvalue())
