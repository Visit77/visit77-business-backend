from django.urls import path
from rest_framework.routers import DefaultRouter

from booking.views import (
    AddOnTemplateView,
    AddOnTemplateRequestViewSet,
    AddOnViewSet,
    BookingViewSet,
    CorePaymentSuccessView,
    CoreSyncView,
    CoreEventView,
    DailyInventoryViewSet,
    DailyRateViewSet,
    HotelViewSet,
    IntegrationStatusView,
    MealPlanViewSet,
    PhysicalRoomViewSet,
    PublicAvailabilityView,
    PublicAYAPaymentView,
    PublicBookingCreateView,
    PublicBookingEstimateView,
    PublicBookingDetailView,
    PublicDemoPaymentView,
    PublicGlobalAvailabilityView,
    PublicHotelAddOnsView,
    RatePlanViewSet,
    RatePeriodViewSet,
    RoomBoardView,
    RoomTypeMealPlanViewSet,
    RoomTypeViewSet,
    SuperAdminAddOnTemplateRequestViewSet,
    SuperAdminAddOnTemplateViewSet,
)


router = DefaultRouter()
router.register("admin/hotels", HotelViewSet, basename="hotel")
router.register("admin/room-types", RoomTypeViewSet, basename="room-type")
router.register("admin/meal-plans", MealPlanViewSet, basename="meal-plan")
router.register("admin/room-type-meal-plans", RoomTypeMealPlanViewSet, basename="room-type-meal-plan")
router.register("admin/physical-rooms", PhysicalRoomViewSet, basename="physical-room")
router.register("admin/rate-plans", RatePlanViewSet, basename="rate-plan")
router.register("admin/inventory", DailyInventoryViewSet, basename="inventory")
router.register("admin/rates", DailyRateViewSet, basename="daily-rate")
router.register("admin/rate-periods", RatePeriodViewSet, basename="rate-period")
router.register("admin/add-ons", AddOnViewSet, basename="add-on")
router.register("admin/add-on-template-requests", AddOnTemplateRequestViewSet, basename="add-on-template-request")
router.register("admin/bookings", BookingViewSet, basename="booking")
router.register("superadmin/add-on-templates", SuperAdminAddOnTemplateViewSet, basename="superadmin-add-on-template")
router.register("superadmin/add-on-template-requests", SuperAdminAddOnTemplateRequestViewSet, basename="superadmin-add-on-template-request")

urlpatterns = [
    path("admin/integration-status/", IntegrationStatusView.as_view()),
    path("admin/add-on-templates/", AddOnTemplateView.as_view()),
    path("admin/room-board/", RoomBoardView.as_view()),
    path("public/search/availability/", PublicGlobalAvailabilityView.as_view()),
    path("public/hotels/<int:core_business_id>/availability/", PublicAvailabilityView.as_view()),
    path("public/hotels/<int:core_business_id>/add-ons/", PublicHotelAddOnsView.as_view()),
    path("public/bookings/estimate/", PublicBookingEstimateView.as_view()),
    path("public/bookings/", PublicBookingCreateView.as_view()),
    path("public/bookings/<uuid:public_token>/", PublicBookingDetailView.as_view()),
    path("public/bookings/<uuid:public_token>/aya-payment/", PublicAYAPaymentView.as_view()),
    path("public/bookings/<uuid:public_token>/demo-payment/", PublicDemoPaymentView.as_view()),
    path("admin/core-sync/businesses/<int:core_business_id>/", CoreSyncView.as_view()),
    path("admin/core-events/", CoreEventView.as_view()),
    path("admin/payments/core-success/", CorePaymentSuccessView.as_view()),
] + router.urls
