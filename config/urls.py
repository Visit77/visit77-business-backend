from django.contrib import admin
from django.urls import include, path
from rest_framework.permissions import AllowAny
from rest_framework.views import APIView

from config.response_formatter import success


class HealthView(APIView):
    permission_classes = [AllowAny]

    def get(self, request):
        return success({"status": "ok", "service": "visit77-booking-engine"})


urlpatterns = [
    path("admin/", admin.site.urls),
    path("health/", HealthView.as_view()),
    path("api/v1/", include("booking.urls")),
]
