from datetime import timedelta
from decimal import Decimal
from unittest.mock import patch

from django.test import TestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIClient

from booking.models import Hotel, RoomType, RatePlan, RoomSeasonalRate, RoomPriceDiscount
from booking.services import availability_for_hotel, create_booking, ensure_daily_inventory_for_room_type, estimate_booking
from booking.directory_pricing import minimum_room_prices_today
from booking.room_pricing import market_default_prices


class SeasonalRateTests(TestCase):
    def setUp(self):
        self.hotel = Hotel.objects.create(core_business_id=77, name='Season Hotel', package='ota_pms', inventory_mode='room_type_count')
        self.room = RoomType.objects.create(hotel=self.hotel, core_room_type_id=301, name='Deluxe', room_price_setup=True,
            room_type_count_inventory=5, default_inventory=5, local_mmk_price=20000, local_usd_price=20,
            foreign_mmk_price=30000, foreign_usd_price=30)
        self.plan = RatePlan.objects.create(room_type=self.room, code='local', name='Local', guest_market='local',
            is_default=True, default_price=20000, usd_display_price=20)
        self.start = timezone.localdate() + timedelta(days=10)
        self.end = self.start + timedelta(days=7)
        self.season = RoomSeasonalRate.objects.create(room_type=self.room, name='Seasonal Room Rate 1',
            start_date=self.start + timedelta(days=2), end_date=self.end - timedelta(days=1),
            local_base_price=50000, local_usd_price=50)
        self.payload = dict(core_business_id=77, check_in=self.start, check_out=self.end, contact_name='Guest', contact_phone='09',
            guest_market='local', rooms=[dict(core_room_type_id=301, rate_plan_id=self.plan.id, quantity=1, adults=1, children=0, extra_beds=0)])

    def test_single_season_stay_has_plain_room_name_on_invoice_and_receipt(self):
        from booking.booking_services.receipt import build_invoice_snapshot, build_receipt_snapshot
        from booking.services import record_payment
        self.payload['check_in'] = self.season.start_date
        self.payload['check_out'] = self.season.start_date + timedelta(days=2)
        booking, _ = create_booking(self.payload)
        invoice = booking.invoices.first()
        line = invoice.lines.get(metadata__line_type='room')
        self.assertEqual(line.total, 100000)
        self.assertEqual(line.metadata['display_title'], 'Deluxe')
        self.assertNotIn('Seasonal Room Rate', line.description)
        self.assertEqual(line.metadata['seasonal_rate_name'], 'Seasonal Room Rate 1')
        payment = record_payment(booking, {'provider': 'cash', 'amount': Decimal('100000'), 'status': 'paid'})
        for snapshot in (build_invoice_snapshot(invoice), build_receipt_snapshot(payment)):
            room_line = next(row for row in snapshot['invoice']['lines'] if row['line_type'] == 'room')
            self.assertEqual(room_line['metadata']['display_title'], 'Deluxe')
            self.assertNotIn('Seasonal Room Rate', room_line['description'])

    def test_mixed_stay_availability_booking_invoice_receipt_and_snapshot(self):
        from booking.booking_services.receipt import build_invoice_snapshot, build_receipt_snapshot
        from booking.services import record_payment
        ensure_daily_inventory_for_room_type(self.room, self.start, days=7)
        result = availability_for_hotel(self.hotel, self.start, self.end)[0]
        self.assertEqual(result['default_price']['rate_plan_id'], self.plan.id)
        nightly = result['rate_plans'][0]['nightly_prices']
        self.assertEqual([night['base_price'] for night in nightly], [20000]*2+[50000]*5)
        self.assertEqual(sum(night['is_seasonal'] for night in nightly), 5)
        self.assertEqual(estimate_booking(self.payload)['room_total'], 290000)
        booking, _ = create_booking(self.payload)
        invoice = booking.invoices.first()
        lines = list(invoice.lines.filter(metadata__line_type='room').order_by('id'))
        self.assertEqual([line.total for line in lines], [40000, 250000])
        self.assertEqual([line.metadata['night_count'] for line in lines], [2, 5])
        self.assertIn('Seasonal Room Rate 1', lines[1].description)
        self.assertEqual(Decimal(build_invoice_snapshot(invoice)['invoice']['room_charge_total']), 290000)
        payment = record_payment(booking, {'provider': 'cash', 'amount': Decimal('290000'), 'status': 'paid'})
        self.assertEqual(Decimal(build_receipt_snapshot(payment)['invoice']['room_charge_total']), 290000)
        self.season.local_base_price = 99999
        self.season.name = 'Changed'
        self.season.save()
        night = booking.rooms.get().nights.get(stay_date=self.season.start_date)
        self.assertEqual(night.unit_price, 50000)
        self.assertEqual(night.rate_snapshot['seasonal_rate_name'], 'Seasonal Room Rate 1')

    def test_season_suppresses_campaign_only_for_supplied_currency(self):
        self.season.local_usd_price = None
        self.season.save()
        campaign = RoomPriceDiscount.objects.create(room_type=self.room, timing_mode='scheduled', start_date=self.start,
            end_date=self.end, local_mmk_price=10000, local_usd_price=10)
        ensure_daily_inventory_for_room_type(self.room, self.start, days=7)
        nightly = availability_for_hotel(self.hotel, self.start, self.end)[0]['rate_plans'][0]['nightly_prices']
        self.assertTrue(nightly[0]['has_discount'])
        self.assertEqual(nightly[2]['base_price'], 50000)
        self.assertFalse(nightly[2]['has_discount'])
        self.assertEqual(nightly[2]['original_price'], 50000)
        self.assertEqual(nightly[2]['usd_display_price'], 10)
        self.assertTrue(nightly[2]['usd_has_discount'])
        matrix = market_default_prices(self.room, self.season.end_date, [campaign])
        self.assertFalse(matrix['local']['has_discount'])
        self.assertEqual(matrix['local']['base_price'], 50000)
        self.assertEqual(matrix['foreign']['base_price'], 30000)
        after = market_default_prices(self.room, self.season.end_date + timedelta(days=1), [campaign])
        self.assertEqual(after['local']['discount_price'], 10000)

    @override_settings(BOOKING_ADMIN_API_KEY='key')
    def test_setup_update_overlap_end_and_scope(self):
        client = APIClient()
        client.credentials(HTTP_X_BOOKING_ADMIN_KEY='key', HTTP_X_BOOKING_BUSINESS_ID='77')
        url = f'/api/v1/admin/room-types/{self.room.id}/seasonal-rates/'
        body = dict(name='Second season', start_date=str(self.end), end_date=str(self.end + timedelta(days=2)), foreign_base_price=40000)
        response = client.post(url, body, format='json')
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["status_code"], 201)
        pk = response.data['data']['id']
        self.assertEqual(client.post(url, body, format='json').status_code, 400)
        self.assertEqual(client.patch(f'{url}{pk}/', {'foreign_base_price': None}, format='json').status_code, 400)
        self.assertEqual(client.patch(f'{url}{pk}/', {'foreign_base_price': 45000}, format='json').status_code, 200)
        self.assertEqual(client.post(f'{url}{pk}/end/').status_code, 200)
        self.assertEqual(client.patch(f'{url}{pk}/', {'name': 'No'}, format='json').status_code, 400)
        self.assertEqual(len(client.get(url).data['data']['seasonal_rates']), 1)
        body['end_date'] = str(self.start)
        self.assertEqual(client.post(url, body, format='json').status_code, 400)
        client.credentials(HTTP_X_BOOKING_ADMIN_KEY='key', HTTP_X_BOOKING_BUSINESS_ID='999')
        self.assertEqual(client.get(url).status_code, 404)

    def test_directory_uses_hotel_today_season_and_regular_after_end(self):
        RoomPriceDiscount.objects.create(room_type=self.room, timing_mode='scheduled', start_date=self.start,
            end_date=self.end, local_mmk_price=10000)
        with patch('booking.directory_pricing.timezone.localdate', return_value=self.season.start_date):
            row = minimum_room_prices_today([77])[77]
            self.assertEqual(row['display_price'], 50000)
            self.assertFalse(row['has_discount'])
            self.assertTrue(row['is_seasonal'])
            self.assertEqual(row['seasonal_rate_id'], self.season.id)
        self.season.ended_at = timezone.now()
        self.season.save()
        with patch('booking.directory_pricing.timezone.localdate', return_value=self.season.start_date):
            row = minimum_room_prices_today([77])[77]
            self.assertEqual(row['display_price'], 10000)
            self.assertTrue(row['has_discount'])

    def test_catalog_today_and_multiple_seasons_keep_original_plan(self):
        from booking.serializers import PublicOTARoomTypeCatalogSerializer, RoomTypeSerializer
        with patch('booking.serializers.room_price_today', return_value=self.season.start_date):
            data = PublicOTARoomTypeCatalogSerializer(self.room, context={'guest_market': 'local'}).data
            self.assertEqual(data['default_prices']['local']['base_price'], 50000)
            self.assertFalse(data['default_prices']['local']['has_discount'])
            self.assertEqual(data['rate_plans'][0]['base_price'], 50000)
            self.assertTrue(data['rate_plans'][0]['is_seasonal'])
        second = RoomSeasonalRate.objects.create(room_type=self.room, name='Season 2', start_date=self.end,
            end_date=self.end + timedelta(days=1), local_base_price=60000, foreign_base_price=70000)
        from booking.services import _rate_amounts
        self.assertEqual(_rate_amounts(None, self.plan, second.start_date, 'local', []), (60000, 20))
        foreign = RatePlan.objects.create(room_type=self.room, code='foreign', name='Foreign', guest_market='foreign', default_price=30000)
        self.assertEqual(_rate_amounts(None, foreign, second.end_date, 'foreign', [])[0], 70000)
        self.assertEqual(_rate_amounts(None, foreign, second.end_date + timedelta(days=1), 'foreign', [])[0], 30000)
        self.assertEqual(len(RoomTypeSerializer(self.room).data['seasonal_rates']), 2)

    @override_settings(BOOKING_ADMIN_API_KEY='key')
    def test_optional_names_increment_per_room_even_after_end(self):
        client = APIClient()
        client.credentials(HTTP_X_BOOKING_ADMIN_KEY='key', HTTP_X_BOOKING_BUSINESS_ID='77')
        url = f'/api/v1/admin/room-types/{self.room.id}/seasonal-rates/'
        body = dict(start_date=str(self.end), end_date=str(self.end), local_base_price=45000)
        response = client.post(url, body, format='json')
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data['data']['name'], 'Seasonal Room Rate 2')
        pk = response.data['data']['id']
        self.assertEqual(client.post(f'{url}{pk}/end/').status_code, 200)
        body['name'] = None
        response = client.post(url, body, format='json')
        self.assertEqual(response.data['data']['name'], 'Seasonal Room Rate 3')
        self.assertEqual(response.data['data']['base_currency'], 'MMK')
        pk = response.data['data']['id']
        response = client.patch(f'{url}{pk}/', {'local_base_price': 46000}, format='json')
        self.assertEqual(response.data['data']['name'], 'Seasonal Room Rate 3')
        response = client.patch(f'{url}{pk}/', {'name': None}, format='json')
        self.assertEqual(response.data['data']['name'], 'Seasonal Room Rate 4')

    def test_seasonal_base_amount_uses_hotel_currency_for_non_mmk_hotel(self):
        from booking.services import _rate_amounts
        from booking.room_pricing_serializers import RoomSeasonalRateSerializer
        self.hotel.base_currency = 'THB'
        self.hotel.save(update_fields=['base_currency'])
        self.plan.currency = 'THB'
        self.plan.save(update_fields=['currency'])
        self.season.local_base_price = 2500
        self.season.local_usd_price = 75
        self.season.save()
        self.assertEqual(_rate_amounts(None, self.plan, self.season.start_date, 'local', []), (2500, 75))
        data = RoomSeasonalRateSerializer(self.season).data
        self.assertEqual(data['base_currency'], 'THB')
        self.assertEqual(Decimal(str(data['local_base_price'])), Decimal('2500'))
        self.assertNotIn('local_mmk_price', data)
        self.hotel.base_currency = 'USD'
        self.hotel.save(update_fields=['base_currency'])
        self.plan.currency = 'USD'
        self.plan.save(update_fields=['currency'])
        self.season.local_base_price = 80
        self.season.save()
        self.assertEqual(_rate_amounts(None, self.plan, self.season.start_date, 'local', []), (80, 80))
