import hmac

from django.conf import settings
from rest_framework.exceptions import PermissionDenied
from rest_framework.permissions import BasePermission


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
        return True


class IsCoreSuperAdmin(BasePermission):
    message = "A Visit77 Core superadmin access token is required."

    def has_permission(self, request, view):
        return bool(
            request.user
            and request.user.is_authenticated
            and getattr(request.user, "is_superuser", False)
        )
