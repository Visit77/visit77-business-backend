from datetime import date, datetime, timezone as dt_timezone
from decimal import Decimal
from importlib import import_module
from types import SimpleNamespace

from django.apps import apps
from django.db import connection
from django.test import TestCase
from django.utils import timezone

from booking.models import Booking, Hotel, Invoice, OTARemittance, OTARemittanceItem, OTARevenueLedger, Payment
from booking.remittance_services import cleanup_non_ota_revenue_ledgers, generate_weekly_remittance
from booking.remittance_views import remittance_data


class RemittanceCommissionPolicyTests(TestCase):
    def setUp(self):
        self.hotel=Hotel.objects.create(core_business_id=77,name='Hotel',package='ota',timezone='UTC')
        self.start=date(2026,11,2)
        self.end=date(2026,11,8)
        self.sequence=0

    def entry(self,gross,tax=0,service=0,event='booking_collection',refund=0,ota=True):
        self.sequence+=1
        booking=Booking.objects.create(hotel=self.hotel,reference=f'COMM-{self.sequence}',source='ota',status='confirmed',
            check_in=self.start,check_out=self.end,contact_name='Guest',contact_phone='09',currency='MMK',grand_total=gross)
        invoice=Invoice.objects.create(booking=booking,invoice_type=Invoice.Type.ROOM_BOOKING,
            charge_scope=Invoice.ChargeScope.OTA if ota else Invoice.ChargeScope.PMS,currency='MMK',subtotal=gross,total=gross)
        payment=Payment.objects.create(booking=booking,invoice=invoice,provider='cash',amount=gross,currency='MMK')
        return OTARevenueLedger.objects.create(hotel=self.hotel,booking=booking,payment=payment,event_key=f'commission:{self.sequence}',
            event_type=event,gross_amount=gross,tax_amount=tax,service_charge_amount=service,refund_amount=refund,
            effective_at=datetime(2026,11,3,12,tzinfo=dt_timezone.utc))

    def generate(self,**kwargs):
        return generate_weekly_remittance(self.hotel,self.start,self.end,**kwargs)

    def test_detail_receipt_uses_ledger_ota_payment_not_later_pms_receipt(self):
        entry = self.entry(700)
        payment = entry.payment
        payment.receipt_number = 'OTA-RECEIPT'
        payment.save(update_fields=['receipt_number'])
        pms_invoice = Invoice.objects.create(
            booking=entry.booking, invoice_type=Invoice.Type.ROOM_BOOKING,
            charge_scope=Invoice.ChargeScope.PMS, currency='MMK', subtotal=100, total=100,
        )
        Payment.objects.create(
            booking=entry.booking, invoice=pms_invoice, provider='cash', amount=100,
            currency='MMK', receipt_number='LATER-PMS-RECEIPT',
        )
        from rest_framework.test import APIRequestFactory
        request = APIRequestFactory().get('/')
        row = self.generate()
        payload = remittance_data(row, detail=True, request=request)
        expected = (
            f'http://testserver/api/v1/public/bookings/{entry.booking.public_token}/'
            f'receipts/{payment.id}/pdf/'
        )
        self.assertEqual(payload['items'][0]['ota_receipt_url'], expected)
        self.assertEqual(
            payload['grouped_items'][0]['date_groups'][0]['items'][0]['ota_receipt_url'], expected,
        )
        payment.receipt_number = None
        payment.save(update_fields=['receipt_number'])
        self.assertIsNone(remittance_data(row, detail=True)['items'][0]['ota_receipt_url'])

    def test_booking_collection_excludes_only_collection_tax(self):
        self.entry(100000, tax=500, service=1000)
        self.entry(10000, tax=100, event='held_release')
        payload = remittance_data(self.generate())
        self.assertEqual(payload['booking_collection'], Decimal('100000'))
        self.assertEqual(payload['tax_excluded_from_commission'], Decimal('600'))
        self.assertEqual(payload['booking_collection_tax'], Decimal('500'))
        self.assertEqual(payload['booking_collection_excluding_tax'], Decimal('99500'))

    def test_single_payment_service_charge_is_commissionable(self):
        self.entry(256,tax=6,service=50)
        row=self.generate()
        self.assertEqual(row.commission_amount,Decimal('12.50'))
        self.assertEqual(row.net_remittance_amount,Decimal('244.00'))
        self.assertEqual(row.service_charge_amount,Decimal('50'))
        payload=remittance_data(row)
        self.assertEqual(payload['service_charge_amount'],Decimal('50'))
        self.assertEqual(payload['booking_collection_tax'], Decimal('6'))
        self.assertEqual(payload['booking_collection_excluding_tax'], Decimal('250'))
        self.assertEqual(payload['service_charge_excluded_from_commission'],0)
        self.assertFalse(payload['commission_excludes_service_charge'])

    def test_collections_releases_and_refunds_use_tax_only_base(self):
        self.entry(550,service=50)
        self.entry(680,tax=30,service=50)
        self.entry(500,event='held_release')
        row=self.generate(accruing=True)
        self.assertEqual(row.commission_amount,Decimal('85'))
        self.assertEqual(row.net_remittance_amount,Decimal('1645'))
        self.entry(0,event='refund',refund=200)
        row=self.generate(accruing=True)
        self.assertEqual(row.commission_amount,Decimal('85'))
        self.assertEqual(row.net_remittance_amount,Decimal('1445'))

    def test_cleanup_recalculates_drafts_with_service_charge_included(self):
        self.entry(256,tax=6,service=50)
        bad=self.entry(100,service=20,ota=False)
        row=self.generate(accruing=True)
        row.commission_excludes_service_charge=True
        row.save(update_fields=['commission_excludes_service_charge'])
        result=cleanup_non_ota_revenue_ledgers(self.hotel)
        self.assertEqual(result['updated_remittances'],1)
        self.assertFalse(OTARevenueLedger.objects.filter(pk=bad.pk).exists())
        row.refresh_from_db()
        self.assertEqual(row.commission_amount,Decimal('12.50'))
        self.assertEqual(row.net_remittance_amount,Decimal('244.00'))
        self.assertFalse(row.commission_excludes_service_charge)

    def test_policy_migration_updates_unapproved_drafts_and_preserves_paid_history(self):
        draft=OTARemittance.objects.create(hotel=self.hotel,period_from=self.start,period_to=self.end,
            gross_booking_amount=256,tax_amount=6,service_charge_amount=50,commission_rate=5,
            commission_amount=10,net_remittance_amount=249,adjustment_amount=3,status='pending')
        paid=OTARemittance.objects.create(hotel=self.hotel,period_from=date(2026,10,12),period_to=date(2026,10,18),
            gross_booking_amount=256,tax_amount=6,service_charge_amount=50,commission_rate=5,
            commission_amount=10,net_remittance_amount=246,status='paid')
        migration=import_module('booking.migrations.0076_commission_includes_service_charge')
        migration.apply_new_commission_policy(apps,SimpleNamespace(connection=connection))
        draft.refresh_from_db();paid.refresh_from_db()
        self.assertEqual(draft.commission_amount,Decimal('12.50'))
        self.assertEqual(draft.net_remittance_amount,Decimal('246.50'))
        self.assertFalse(draft.commission_excludes_service_charge)
        self.assertTrue(draft.audit_logs.filter(action='commission_policy_changed').exists())
        self.assertEqual(paid.commission_amount,Decimal('10'))
        self.assertEqual(paid.net_remittance_amount,Decimal('246'))
        self.assertTrue(paid.commission_excludes_service_charge)
        self.assertEqual(remittance_data(paid)['service_charge_excluded_from_commission'],Decimal('50'))

    def test_finalized_statement_keeps_legacy_commission_when_regenerated(self):
        self.entry(256,tax=6,service=50)
        row=self.generate()
        row.status='paid'
        row.commission_excludes_service_charge=True
        row.commission_amount=Decimal('10')
        row.net_remittance_amount=Decimal('246')
        row.paid_at=timezone.now()
        row.save()
        refreshed=self.generate(historical=True)
        self.assertEqual(refreshed.commission_amount,Decimal('10'))
        self.assertTrue(refreshed.commission_excludes_service_charge)
