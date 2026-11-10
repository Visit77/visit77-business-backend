import uuid
from unittest.mock import patch

from django.test import TestCase, override_settings
from rest_framework.test import APIClient

from booking.models import Hotel
from booking.remittance_views import _merchant_verification_data


@override_settings(BOOKING_ADMIN_API_KEY='key')
class MerchantVerificationSyncTests(TestCase):
    @patch('booking.integrations.core.CoreClient')
    def test_catalog_events_create_update_and_clear_merchant_without_touching_access(self, core_client):
        hotel = Hotel.objects.create(core_business_id=77, name='Hotel', package='ota_pms', is_active=False,
            features={'online_booking': False}, access_snapshot={'status': 'expired'})
        bundle = {'business': {'id': 77, 'name': 'Hotel', 'address': 'Yangon', 'phone': '09', 'timezone': 'Asia/Yangon'},
                  'merchant_verification': {'version': 1, 'bank_name': 'KBZ', 'bank_account_name': 'Hotel Ltd',
                                            'bank_account_number': '001234', 'bank_branch': 'Main'}}
        core_client.return_value.provisioning_bundle.return_value = bundle
        client = APIClient()
        client.credentials(HTTP_X_BOOKING_ADMIN_KEY='key')

        def event(action):
            response = client.post('/api/v1/admin/core-events/', {
                'event_id': str(uuid.uuid4()), 'event_type': 'direct_booking.catalog_changed', 'business_id': 77,
                'payload': {'resource': 'merchant_verification', 'action': action},
            }, format='json')
            self.assertEqual(response.status_code, 200, response.data)
            hotel.refresh_from_db()
            self.assertFalse(hotel.is_active)
            self.assertEqual(hotel.package, 'ota_pms')
            self.assertEqual(hotel.access_snapshot, {'status': 'expired'})
            self.assertEqual(hotel.features, {'online_booking': False})

        event('created')
        self.assertEqual(_merchant_verification_data(hotel)['payment_account_number'], '001234')
        bundle['merchant_verification']['bank_account_number'] = '009999'
        bundle['merchant_verification']['version'] = 2
        event('updated')
        self.assertEqual(_merchant_verification_data(hotel)['payment_account_number'], '009999')
        self.assertEqual(hotel.core_snapshot['merchant_verification']['version'], 2)
        bundle['merchant_verification'] = None
        event('deleted')
        self.assertNotIn('merchant_verification', hotel.core_snapshot)
        self.assertEqual(_merchant_verification_data(hotel)['payment_account_number'], '')
