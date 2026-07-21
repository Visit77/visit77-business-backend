from dataclasses import dataclass
from typing import Any

import httpx
from django.conf import settings
from django.utils import timezone
from rest_framework.exceptions import APIException

from booking.models import Hotel, PhysicalRoom, RatePlan, RoomType


class CoreIntegrationError(APIException):
    status_code = 502
    default_code = "core_integration_error"


def _unwrap(payload: Any):
    if isinstance(payload, dict) and "data" in payload:
        return payload["data"]
    return payload


def _image_url(value):
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        return value.get("url", "")
    return ""


def _first_present(payload, *keys, default=None):
    for key in keys:
        if key in payload and payload[key] is not None:
            return payload[key]
    return default


@dataclass
class CoreClient:
    base_url: str = settings.CORE_BASE_URL
    token: str = settings.CORE_SERVICE_TOKEN
    service_key: str = settings.CORE_SERVICE_KEY

    @property
    def headers(self):
        headers = {"X-Booking-Service-Key": self.service_key} if self.service_key else {}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        return headers

    def get(self, path: str, params=None):
        try:
            response = httpx.get(f"{self.base_url}/{path.lstrip('/')}", params=params, headers=self.headers, timeout=15)
            response.raise_for_status()
            return _unwrap(response.json())
        except httpx.HTTPStatusError as exc:
            detail = exc.response.text[:1000] if exc.response is not None else ""
            raise CoreIntegrationError(
                f"Visit77 Core returned HTTP {exc.response.status_code}: {detail}"
            ) from exc
        except (httpx.HTTPError, ValueError) as exc:
            raise CoreIntegrationError(f"Visit77 Core request failed: {exc}") from exc

    def business(self, core_business_id: int):
        return self.get(f"business/{core_business_id}/")

    def room_types(self, core_business_id: int):
        return self.get("room_types/", {"business_id": core_business_id})

    def physical_rooms(self, core_business_id: int):
        return self.get("physical_rooms/", {"business_id": core_business_id})

    def provisioning_bundle(self, core_business_id: int):
        if not self.service_key:
            raise CoreIntegrationError("Booking Engine CORE_SERVICE_KEY is not configured.")
        return self.get(f"booking-integrations/businesses/{core_business_id}/provisioning/")

    def check_access(self, core_business_id: int, bearer_token: str):
        original_token = self.token
        self.token = bearer_token
        try:
            return self.get(f"booking-integrations/businesses/{core_business_id}/access/")
        finally:
            self.token = original_token


def sync_business_from_core(core_business_id: int, client=None):
    client = client or CoreClient()
    bundle = client.provisioning_bundle(core_business_id)
    business_data = bundle.get("business") if isinstance(bundle, dict) else None
    if not business_data:
        raise CoreIntegrationError("Business was not found in Visit77 Core.")

    now = timezone.now()
    access_data = bundle.get("access") or {}
    existing_hotel = Hotel.objects.filter(core_business_id=core_business_id).first()
    package = (
        access_data.get("package")
        or business_data.get("hotel_package")
        or (existing_hotel.package if existing_hotel else None)
        or Hotel.Package.OTA
    )
    features = (
        access_data.get("features")
        or business_data.get("features")
        or (existing_hotel.features if existing_hotel else None)
        or {}
    )
    base_currency = (
        business_data.get("base_currency")
        or business_data.get("currency")
        or business_data.get("default_currency")
        or (existing_hotel.base_currency if existing_hotel else None)
        or "MMK"
    )
    hotel, _ = Hotel.objects.update_or_create(
        core_business_id=core_business_id,
        defaults={
            "name": business_data.get("name_1") or business_data.get("name") or f"Business {core_business_id}",
            "slug": business_data.get("slug") or "",
            "address": business_data.get("address_info") or business_data.get("address") or "",
            "phone": business_data.get("phone") or business_data.get("phone_no") or "",
            "cover_image_url": _image_url(business_data.get("profile")),
            "base_currency": base_currency,
            "package": package,
            "features": features,
            "is_active": bool(business_data.get("status", business_data.get("is_active", True))),
            "core_snapshot": business_data,
            "access_snapshot": access_data,
            "synced_at": now,
        },
    )

    synced_room_types = []
    core_room_type_ids = []
    for payload in bundle.get("room_types", []) or []:
        core_room_type_ids.append(payload["id"])
        photos = payload.get("photos") or []
        cover = next((photo for photo in photos if photo.get("is_cover")), photos[0] if photos else {})
        room_type, created = RoomType.objects.update_or_create(
            hotel=hotel,
            core_room_type_id=payload["id"],
            defaults={
                "name": payload.get("name") or f"Room type {payload['id']}",
                "description": payload.get("description") or "",
                "cover_image_url": _image_url(cover.get("image")),
                "max_adults": payload.get("max_adults") or 1,
                "max_children": payload.get("max_children") or 0,
                "max_occupancy": payload.get("max_occupancy") or 1,
                "booking_enabled": payload.get("is_active", True),
                "core_active": payload.get("is_active", True),
                "core_snapshot": payload,
                "synced_at": now,
            },
        )
        synced_core_rate_plan_ids = []
        for rate_payload in payload.get("rate_plans", []):
            external_id = str(
                rate_payload.get("core_rate_plan_id")
                or f"room-{payload['id']}-{rate_payload['code']}"
            )
            synced_core_rate_plan_ids.append(external_id)
            rate_currency = rate_payload.get("currency") or base_currency
            default_price = _first_present(rate_payload, "default_price", default=0)
            base_price = _first_present(
                rate_payload,
                "base_price",
                "default_base_price",
                default=default_price,
            )
            usd_display_price = _first_present(
                rate_payload,
                "usd_display_price",
                "usd_price",
                default=default_price if rate_currency == "USD" else None,
            )
            extra_bed_price = _first_present(rate_payload, "extra_bed_price", default=0)
            extra_bed_base_price = _first_present(
                rate_payload,
                "extra_bed_base_price",
                "extra_bed_default_base_price",
                default=extra_bed_price,
            )
            extra_bed_usd_display_price = _first_present(
                rate_payload,
                "extra_bed_usd_display_price",
                "extra_bed_usd_price",
                default=extra_bed_price if rate_currency == "USD" else None,
            )
            RatePlan.objects.update_or_create(
                core_rate_plan_id=external_id,
                defaults={
                    "room_type": room_type,
                    "code": rate_payload["code"],
                    "source": RatePlan.Source.CORE,
                    "is_default": True,
                    "name": rate_payload.get("name") or rate_payload["code"],
                    "guest_market": rate_payload.get("guest_market") or RatePlan.GuestMarket.ALL,
                    "base_price": base_price,
                    "usd_display_price": usd_display_price,
                    "extra_bed_base_price": extra_bed_base_price,
                    "extra_bed_usd_display_price": extra_bed_usd_display_price,
                    "currency": base_currency,
                    "default_price": base_price,
                    "extra_bed_price": extra_bed_base_price,
                    "breakfast_included": rate_payload.get("breakfast_included", False),
                    "refundable": rate_payload.get("refundable", True),
                    "cancellation_policy": rate_payload.get("cancellation_policy") or {},
                    "is_active": rate_payload.get("is_active", True),
                },
            )
        room_type.rate_plans.filter(source=RatePlan.Source.CORE).exclude(
            core_rate_plan_id__in=synced_core_rate_plan_ids,
        ).update(is_active=False)
        synced_room_types.append(room_type)

    hotel.room_types.exclude(core_room_type_id__in=core_room_type_ids).update(core_active=False, booking_enabled=False)

    room_type_by_core_id = {item.core_room_type_id: item for item in synced_room_types}
    physical_room_count = 0
    core_rooms = bundle.get("physical_rooms", []) or []
    seen_core_room_ids = []
    for payload in core_rooms:
        core_room_type = payload.get("room_type") or {}
        core_room_type_id = payload.get("room_type_id") or core_room_type.get("id")
        room_type = room_type_by_core_id.get(core_room_type_id)
        if not room_type:
            continue
        seen_core_room_ids.append(payload["id"])
        room, created = PhysicalRoom.objects.update_or_create(
            hotel=hotel,
            room_number=payload.get("room_no") or str(payload["id"]),
            defaults={
                "room_type": room_type,
                "core_physical_room_id": payload["id"],
                "floor": payload.get("floor") or "",
                "building": payload.get("building") or "",
                "is_active": payload.get("is_active", True),
                "core_snapshot": payload,
            },
        )
        if created:
            room.status = payload.get("status") or PhysicalRoom.Status.VACANT
            room.save(update_fields=["status"])
        if created:
            physical_room_count += 1

    hotel.physical_rooms.filter(core_physical_room_id__isnull=False).exclude(core_physical_room_id__in=seen_core_room_ids).update(is_active=False)

    from booking.services import active_sellable_room_count, ensure_daily_inventory_for_room_type

    inventory_created = 0
    inventory_updated = 0
    for room_type in synced_room_types:
        active_rooms = active_sellable_room_count(room_type)
        result = ensure_daily_inventory_for_room_type(room_type, total_rooms=active_rooms)
        inventory_created += result["created"]
        inventory_updated += result["updated"]

    return {
        "hotel_id": hotel.id,
        "core_business_id": core_business_id,
        "room_types": len(synced_room_types),
        "new_physical_rooms": physical_room_count,
        "inventory_created": inventory_created,
        "inventory_updated": inventory_updated,
    }
