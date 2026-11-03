from datetime import date, datetime, timezone as dt_timezone
from decimal import Decimal
from unittest.mock import patch

from django.test import TestCase, override_settings
from rest_framework.test import APIClient

from booking.directory_pricing import minimum_room_prices_today
from booking.models import Hotel, RoomType, RatePlan, RoomPriceDiscount, DailyRate


@patch('django.utils.timezone.now', return_value=datetime(2026, 11, 2, 20, tzinfo=dt_timezone.utc))
class DirectoryMinimumPriceTests(TestCase):
    def setUp(self):
        self.hotel = Hotel.objects.create(core_business_id=77, name='Hotel', timezone='Asia/Yangon')
        self.room = RoomType.objects.create(hotel=self.hotel, core_room_type_id=1913, name='Room A', room_price_setup=True,
            local_mmk_price=100000, local_usd_price=25, foreign_mmk_price=150000, foreign_usd_price=40)
        self.plan = RatePlan.objects.create(room_type=self.room, code='local', name='Local', guest_market='local', default_price=100000, usd_display_price=25)
        self.other = RoomType.objects.create(hotel=self.hotel, core_room_type_id=1914, name='Room B', room_price_setup=True, local_mmk_price=90000)
        RatePlan.objects.create(room_type=self.other, code='local', name='Local', guest_market='local', default_price=90000)

    def campaign(self, **changes):
        values = dict(room_type=self.room, timing_mode='scheduled', start_date=date(2026,11,3), end_date=date(2026,11,3), local_mmk_price=80000, local_usd_price=20)
        values.update(changes)
        return RoomPriceDiscount.objects.create(**values)

    def test_minimum_uses_hotel_today_discount_and_core_room_identity(self, now):
        self.campaign()
        row = minimum_room_prices_today([77])[77]
        self.assertEqual(row['room_type_id'], 1913)
        self.assertEqual(row['booking_room_type_id'], self.room.id)
        self.assertEqual(row['price_date'], '2026-11-03')
        self.assertEqual(row['original_price'], Decimal('100000'))
        self.assertEqual(row['display_price'], Decimal('80000'))
        self.assertEqual(row['discount_percentage'], Decimal('20'))
        self.assertTrue(row['has_discount'])
        self.assertEqual(self.room.default_inventory, 0)  # advertised rate, not an inventory claim

    def test_ended_future_expired_campaigns_are_not_advertised(self, now):
        discount = self.campaign(ended_at=now.return_value)
        for dates in ((date(2026,11,3),date(2026,11,3),now.return_value),
                      (date(2026,11,4),date(2026,11,5),None), (date(2026,11,1),date(2026,11,2),None)):
            discount.start_date, discount.end_date, discount.ended_at = dates
            discount.save()
            row = minimum_room_prices_today([77])[77]
            self.assertEqual(row['room_type_id'], 1914)
            self.assertEqual(row['display_price'], Decimal('90000'))
            self.assertFalse(row['has_discount'])
            self.assertIsNone(row['discount_price'])

    def test_daily_rate_discount_and_price_range_choose_matching_room(self, now):
        self.campaign()
        DailyRate.objects.create(rate_plan=self.plan, stay_date=date(2026,11,3), price=120000)
        row = minimum_room_prices_today([77])[77]
        self.assertEqual(row['original_price'], Decimal('120000'))
        self.assertEqual(row['discount_percentage'], Decimal('33.33'))
        row = minimum_room_prices_today([77], min_price=Decimal('85000'), max_price=Decimal('95000'))[77]
        self.assertEqual(row['room_type_id'], 1914)

    def test_selected_market_and_currency_and_unconfigured_rooms(self, now):
        self.campaign(foreign_usd_price=32)
        RatePlan.objects.create(room_type=self.room, code='foreign', name='Foreign', guest_market='foreign', default_price=150000, usd_display_price=40)
        row = minimum_room_prices_today([77], guest_market='foreign', display_currency='USD')[77]
        self.assertEqual(row['display_price'], Decimal('32'))
        self.assertEqual(row['original_price'], Decimal('40'))
        self.assertEqual(row['discount_percentage'], Decimal('20'))
        self.room.room_price_setup = False
        self.room.save()
        self.assertEqual(minimum_room_prices_today([77], guest_market='foreign'), {})
        self.hotel.is_active = False
        self.hotel.save()
        self.assertEqual(minimum_room_prices_today([77]), {})

    @override_settings(BOOKING_ADMIN_API_KEY='key')
    def test_bulk_api_authentication_scope_validation_and_empty_result(self, now):
        client = APIClient()
        url = '/api/v1/admin/directory/minimum-room-prices/'
        self.assertEqual(client.post(url, {'business_ids':[77]}, format='json').status_code, 403)
        client.credentials(HTTP_X_BOOKING_ADMIN_KEY='key')
        self.campaign()
        response = client.post(url, {'business_ids':[77]}, format='json')
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data['data']['minimum_rooms']['77']['display_price'], 80000)
        self.assertEqual(client.post(url, {'business_ids':[77], 'min_price':100, 'max_price':10}, format='json').status_code,400)
        client.credentials(HTTP_X_BOOKING_ADMIN_KEY='key',HTTP_X_BOOKING_BUSINESS_ID='999')
        self.assertEqual(client.post(url, {'business_ids':[77]}, format='json').data['data']['minimum_rooms'],{})

    def test_usd_base_hotel_does_not_require_a_separate_usd_display_value(self, now):
        self.hotel.base_currency='USD'
        self.hotel.save()
        self.plan.base_price=Decimal('25')
        self.plan.default_price=Decimal('25')
        self.plan.currency='USD'
        self.plan.usd_display_price=None
        self.plan.save()
        self.other.room_price_setup=False
        self.other.save()
        self.campaign()
        row=minimum_room_prices_today([77],display_currency='USD')[77]
        self.assertEqual(row['original_price'],Decimal('25'))
        self.assertEqual(row['display_price'],Decimal('20'))
        self.assertEqual(minimum_room_prices_today([77],display_currency='MMK'),{})
