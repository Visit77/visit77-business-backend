from datetime import date
from types import SimpleNamespace

from django.test import TestCase
from rest_framework.test import APIRequestFactory, force_authenticate

from booking.models import Hotel, OTARemittance
from booking.remittance_views import SuperAdminRemittanceListView


class RemittancePackageFilterTests(TestCase):
    def setUp(self):
        self.hotels={package:Hotel.objects.create(core_business_id=100+i,name=package.upper(),package=package)
                     for i,package in enumerate(('free','pms','ota','ota_pms'))}
        for package,status in (('pms','checked'),('ota','pending')):
            OTARemittance.objects.create(hotel=self.hotels[package],period_from=date(2026,11,2),period_to=date(2026,11,8),status=status)

    def get(self,**params):
        request=APIRequestFactory().get('/api/v1/superadmin/remittances/',params)
        force_authenticate(request,user=SimpleNamespace(is_authenticated=True,is_superuser=True))
        response=SuperAdminRemittanceListView.as_view()(request)
        self.assertEqual(response.status_code,200,response.data)
        return response.data['data']

    def test_list_facets_and_pagination_only_count_ota_packages(self):
        data=self.get(limit=1,offset=1)
        self.assertEqual(data['count'],2)
        self.assertEqual(data['hotel_count'],2)
        self.assertEqual(data['total_pages'],2)
        self.assertEqual(data['payout_counts']['all'],2)
        self.assertEqual(data['action_counts']['all'],2)
        self.assertEqual(data['action_counts']['need_to_pay'],0)
        self.assertEqual(data['action_counts']['need_to_check'],1)
        other=self.get(limit=1,offset=2)
        ids={row['hotel_id'] for row in data['results']+other['results']}
        self.assertEqual(ids,{self.hotels['ota'].id,self.hotels['ota_pms'].id})

    def test_business_filter_cannot_include_non_ota_or_inactive_hotels(self):
        for package in ('free','pms'):
            data=self.get(business_id=self.hotels[package].core_business_id)
            self.assertEqual(data['results'],[])
            self.assertEqual(data['count'],0)
            self.assertEqual(data['payout_counts']['all'],0)
        self.hotels['ota'].is_active=False
        self.hotels['ota'].save(update_fields=['is_active'])
        self.assertEqual(self.get()['count'],1)

    def test_test_city_scope_still_excludes_non_ota_packages(self):
        for package in ('free','pms','ota'):
            self.hotels[package].core_snapshot={'city':{'id':999999,'name':'Test City'}}
            self.hotels[package].save(update_fields=['core_snapshot'])
        data=self.get(is_test='true')
        self.assertEqual(data['count'],1)
        self.assertEqual(data['results'][0]['hotel_id'],self.hotels['ota'].id)
        self.assertEqual(data['payout_counts']['all'],1)
