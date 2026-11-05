from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo
from types import SimpleNamespace
from unittest.mock import patch

from django.test import TestCase
from rest_framework.test import APIRequestFactory, force_authenticate

from booking.models import Booking, Hotel, Invoice, OTARemittanceItem, OTARevenueLedger, Payment
from booking.remittance_services import active_payout_period, generate_previous_week_remittances, generate_weekly_remittance, refresh_current_week_remittance
from booking.remittance_views import SuperAdminHotelPayoutOptionView, SuperAdminRemittanceListView


class ThreeTimesWeeklyRemittanceTests(TestCase):
    def setUp(self):
        self.hotel = Hotel.objects.create(core_business_id=77, name='Hotel', timezone='Asia/Yangon', package='ota')

    def test_default_and_all_days_partition_without_overlap(self):
        self.assertEqual(self.hotel.remittance_payout_option, 'three_times_weekly')
        monday = date(2026, 11, 2)
        expected = [(0, 1), (0, 1), (2, 3), (2, 3), (4, 6), (4, 6), (4, 6)]
        for day, (start, end) in enumerate(expected):
            self.assertEqual(active_payout_period(self.hotel, monday + timedelta(days=day)),
                             (monday + timedelta(days=start), monday + timedelta(days=end)))

    @patch('booking.remittance_services.sync_all_payment_ledgers')
    @patch('booking.remittance_services.generate_weekly_remittance')
    def test_payout_days_finalize_correct_period_and_month_year_boundaries(self, generate, sync):
        for today, start, end in [
            (date(2026, 11, 2), date(2026, 10, 30), date(2026, 11, 1)),
            (date(2026, 11, 4), date(2026, 11, 2), date(2026, 11, 3)),
            (date(2026, 11, 6), date(2026, 11, 4), date(2026, 11, 5)),
            (date(2027, 1, 4), date(2027, 1, 1), date(2027, 1, 3)),
        ]:
            generate.reset_mock()
            generate_previous_week_remittances(today)
            generate.assert_called_once_with(self.hotel, start, end)

    @patch('booking.remittance_services.generate_weekly_remittance')
    def test_refresh_finalizes_closed_segments_and_accrues_current_segment(self, generate):
        refresh_current_week_remittance(self.hotel, date(2026, 11, 6), sync_ledger=False)
        self.assertEqual(generate.call_args_list[0].args[1:], (date(2026, 11, 2), date(2026, 11, 3)))
        self.assertEqual(generate.call_args_list[1].args[1:], (date(2026, 11, 4), date(2026, 11, 5)))
        self.assertEqual(generate.call_args_list[2].args[1:], (date(2026, 11, 6), date(2026, 11, 8)))
        self.assertEqual(generate.call_args_list[2].kwargs, {'accruing': True})

    def test_legacy_options_keep_their_periods(self):
        self.hotel.remittance_payout_option = 'weekly'
        self.assertEqual(active_payout_period(self.hotel, date(2026, 11, 6)), (date(2026, 11, 2), date(2026, 11, 8)))
        self.hotel.remittance_payout_option = 'twice_weekly'
        self.assertEqual(active_payout_period(self.hotel, date(2026, 11, 6)), (date(2026, 11, 5), date(2026, 11, 8)))

    def test_ledger_days_are_assigned_once_to_the_correct_payout(self):
        booking = Booking.objects.create(hotel=self.hotel, reference='THREE', source='ota', status='confirmed',
            check_in=date(2026, 11, 1), check_out=date(2026, 11, 10), contact_name='Guest', currency='MMK')
        invoice = Invoice.objects.create(booking=booking, currency='MMK', charge_scope='ota', invoice_type='room_booking')
        payment = Payment.objects.create(booking=booking, invoice=invoice, provider='cash', amount=700, currency='MMK')
        for day in range(7):
            local_day = date(2026, 10, 30) + timedelta(days=day)
            OTARevenueLedger.objects.create(hotel=self.hotel, booking=booking, payment=payment,
                event_key=f'three:{day}', event_type='booking_collection', gross_amount=100,
                effective_at=datetime.combine(local_day, time.min, tzinfo=ZoneInfo('Asia/Yangon')))
        periods = [(date(2026, 10, 30), date(2026, 11, 1), 3),
                   (date(2026, 11, 2), date(2026, 11, 3), 2),
                   (date(2026, 11, 4), date(2026, 11, 5), 2)]
        for start, end, count in periods:
            row = generate_weekly_remittance(self.hotel, start, end)
            self.assertEqual(row.items.count(), count)
            self.assertEqual(generate_weekly_remittance(self.hotel, start, end).pk, row.pk)
        self.assertEqual(OTARemittanceItem.objects.count(), 7)

    def test_superadmin_choices_and_list_filter_accept_new_option(self):
        user = SimpleNamespace(is_authenticated=True, is_superuser=True)
        request = APIRequestFactory().get('/')
        force_authenticate(request, user=user)
        response = SuperAdminHotelPayoutOptionView.as_view()(request, hotel_id=self.hotel.pk)
        self.assertIn({'value': 'three_times_weekly', 'label': 'Three times a week'}, response.data['data']['choices'])
        request = APIRequestFactory().get('/', {'payout_option': 'three_times_weekly'})
        force_authenticate(request, user=user)
        response = SuperAdminRemittanceListView.as_view()(request)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data['data']['payout_counts']['three_times_weekly'], 1)
        self.assertEqual(response.data['data']['results'][0]['hotel_id'], self.hotel.pk)
