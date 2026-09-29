import hmac

from django.conf import settings
from django.utils import timezone
from django.utils.dateparse import parse_datetime
from rest_framework.exceptions import PermissionDenied
from rest_framework.permissions import BasePermission

from booking.models import Hotel


class HasBookingAdminKey(BasePermission):
    message = "A valid X-Booking-Admin-Key header is required."

    def deny(self, message):
        self.message = message
        raise PermissionDenied(detail=message)

    def has_permission(self, request, view):
        supplied = request.headers.get("X-Booking-Admin-Key", "")
        expected = settings.BOOKING_ADMIN_API_KEY
        if not supplied:
            return self.deny("X-Booking-Admin-Key header is required.")
        if not expected:
            return self.deny("BOOKING_ADMIN_API_KEY is not configured.")
        if not hmac.compare_digest(supplied, expected):
            return self.deny("X-Booking-Admin-Key is invalid.")
        raw_business_id = request.headers.get("X-Booking-Business-ID", "")
        if raw_business_id:
            try:
                request.booking_core_business_id = int(raw_business_id)
            except (TypeError, ValueError):
                return self.deny("X-Booking-Business-ID must be a positive integer.")
            if request.booking_core_business_id <= 0:
                return self.deny("X-Booking-Business-ID must be a positive integer.")
        else:
            request.booking_core_business_id = None
        if (
            getattr(view, "business_scoped", False)
            and settings.BOOKING_REQUIRE_BUSINESS_SCOPE
            and request.booking_core_business_id is None
        ):
            return self.deny("X-Booking-Business-ID is required for hotel-admin APIs.")
        if request.booking_core_business_id and request.method not in {"GET", "HEAD", "OPTIONS"}:
            hotel = Hotel.objects.filter(
                core_business_id=request.booking_core_business_id
            ).only("access_snapshot").first()
            trial = ((hotel.access_snapshot or {}).get("trial") or {}) if hotel else {}
            trial_ends_at = parse_datetime(str(trial.get("ends_at") or "")) if trial else None
            trial_expired = bool(
                trial and (
                    not trial.get("is_active", False)
                    or (trial_ends_at and trial_ends_at <= timezone.now())
                )
            )
            if trial_expired:
                return self.deny(
                    "Your Direct Booking trial has expired. Subscribe to continue using hotel actions."
                )
        return True


class IsCoreSuperAdmin(BasePermission):
    message = "A Visit77 Core superadmin access token is required."

    def has_permission(self, request, view):
        return bool(
            request.user
            and request.user.is_authenticated
            and getattr(request.user, "is_superuser", False)
        )
