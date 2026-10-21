from decimal import Decimal

from django.contrib.auth.hashers import check_password, make_password
from django.db import transaction
from django.utils import timezone
from rest_framework.exceptions import ValidationError
from rest_framework.views import APIView

from booking.models import (
    Booking,
    Hotel,
    OTACommissionRate,
    OTARemittance,
    OTARemittanceActor,
    OTARemittanceAuditLog,
)
from booking.authentication import CoreJWTAuthentication
from booking.permissions import HasBookingAdminKey, IsCoreSuperAdmin
from config.response_formatter import success


ADMIN_RIGHTS = {"checker", "payer"}


def _actor_data(actor):
    return {
        "id": str(actor.id),
        "scope": actor.scope,
        "hotel_id": actor.hotel_id,
        "name": actor.name,
        "access_rights": actor.access_rights,
        "is_active": actor.is_active,
        "created_at": actor.created_at,
        "removed_at": actor.removed_at,
    }


def _audit_data(log):
    return {
        "action": log.action,
        "actor_id": str(log.actor_id) if log.actor_id else None,
        "actor_name": log.actor_name,
        "created_at": log.created_at,
        "metadata": log.metadata,
    }


def remittance_data(remittance, detail=False):
    data = {
        "id": remittance.id,
        "reference": remittance.reference,
        "hotel_id": remittance.hotel_id,
        "core_business_id": remittance.hotel.core_business_id,
        "hotel_name": remittance.hotel.name,
        "period_from": remittance.period_from,
        "period_to": remittance.period_to,
        "currency": remittance.currency,
        "booking_collection": remittance.gross_booking_amount,
        "released_held_funds": remittance.released_held_funds_amount,
        "tax_excluded_from_commission": remittance.tax_amount,
        "service_charge_excluded_from_commission": remittance.service_charge_amount,
        "commission_rate": remittance.commission_rate,
        "commission": remittance.commission_amount,
        "refund": remittance.refund_amount,
        "net_payout": remittance.net_remittance_amount,
        "status": remittance.status,
        "system_generated": remittance.system_generated,
        "historical_backfill": remittance.historical_backfill,
        "checked_at": remittance.checked_at,
        "paid_at": remittance.paid_at,
        "received_at": remittance.received_at,
        "booking_ids": list(remittance.items.values_list("booking_id", flat=True).distinct()),
    }
    if detail:
        data["items"] = [
            {
                "id": item.id,
                "booking_id": str(item.booking_id),
                "booking_code": item.booking.booking_code,
                "payment_id": str(item.ledger_entry.payment_id),
                "type": item.item_type,
                "effective_at": item.ledger_entry.effective_at,
                "gross_amount": item.gross_amount,
                "tax_amount": item.tax_amount,
                "service_charge_amount": item.service_charge_amount,
                "refund_amount": item.refund_amount,
                "booking_settlement_status": item.booking.settlement_status,
            }
            for item in remittance.items.select_related("booking", "ledger_entry").all()
        ]
        data["audit_logs"] = [_audit_data(log) for log in remittance.audit_logs.select_related("actor")]
    return data


def _hotel_from_request(request):
    business_id = getattr(request, "booking_core_business_id", None)
    if not business_id:
        raise ValidationError({"X-Booking-Business-ID": "This header is required."})
    try:
        return Hotel.objects.get(core_business_id=business_id)
    except Hotel.DoesNotExist as exc:
        raise ValidationError({"X-Booking-Business-ID": "Hotel was not found."}) from exc


def _require_passcode_actor(data, *, scope, right=None, hotel=None):
    actor_id = data.get("actor_id")
    passcode = str(data.get("passcode") or "")
    if not actor_id or not passcode:
        raise ValidationError({"actor": "actor_id and passcode are required."})
    actor = OTARemittanceActor.objects.filter(id=actor_id, scope=scope, is_active=True).first()
    if not actor or (hotel and actor.hotel_id != hotel.id) or not check_password(passcode, actor.passcode_hash):
        raise ValidationError({"passcode": "Actor or passcode is invalid."})
    if right and right not in (actor.access_rights or []):
        raise ValidationError({"access_rights": f"{right} access is required."})
    return actor


class HotelRemittanceActorListCreateView(APIView):
    permission_classes = [HasBookingAdminKey]
    business_scoped = True

    def get(self, request):
        hotel = _hotel_from_request(request)
        actors = OTARemittanceActor.objects.filter(scope=OTARemittanceActor.Scope.HOTEL, hotel=hotel)
        return success({"actors": [_actor_data(actor) for actor in actors]})

    def post(self, request):
        hotel = _hotel_from_request(request)
        name, passcode = str(request.data.get("name") or "").strip(), str(request.data.get("passcode") or "")
        if not name or len(passcode) < 4:
            raise ValidationError({"actor": "name and a passcode of at least 4 characters are required."})
        actor = OTARemittanceActor.objects.create(
            scope=OTARemittanceActor.Scope.HOTEL, hotel=hotel, name=name,
            passcode_hash=make_password(passcode), access_rights=["checker"],
        )
        return success({"actor": _actor_data(actor)})


class HotelRemittanceActorDeleteView(APIView):
    permission_classes = [HasBookingAdminKey]
    business_scoped = True

    def delete(self, request, actor_id):
        hotel = _hotel_from_request(request)
        actor = OTARemittanceActor.objects.filter(id=actor_id, hotel=hotel, is_active=True).first()
        if not actor:
            raise ValidationError({"actor_id": "Active checker was not found."})
        actor.is_active, actor.removed_at = False, timezone.now()
        actor.save(update_fields=["is_active", "removed_at"])
        return success({"removed": True, "actor": _actor_data(actor)})


class SuperAdminRemittanceActorListCreateView(APIView):
    authentication_classes = [CoreJWTAuthentication]
    permission_classes = [IsCoreSuperAdmin]

    def get(self, request):
        actors = OTARemittanceActor.objects.filter(scope=OTARemittanceActor.Scope.ADMIN)
        return success({"actors": [_actor_data(actor) for actor in actors]})

    def post(self, request):
        name, passcode = str(request.data.get("name") or "").strip(), str(request.data.get("passcode") or "")
        raw_rights = request.data.get("access_rights") or []
        if isinstance(raw_rights, str):
            raw_rights = [value.strip() for value in raw_rights.split(",") if value.strip()]
        rights = list(dict.fromkeys(raw_rights))
        if not name or len(passcode) < 4:
            raise ValidationError({"actor": "name and a passcode of at least 4 characters are required."})
        if not rights or set(rights) - ADMIN_RIGHTS:
            raise ValidationError({"access_rights": "Select checker, payer, or both."})
        actor = OTARemittanceActor.objects.create(
            scope=OTARemittanceActor.Scope.ADMIN, name=name,
            passcode_hash=make_password(passcode), access_rights=rights,
        )
        return success({"actor": _actor_data(actor)})


class SuperAdminRemittanceActorDeleteView(APIView):
    authentication_classes = [CoreJWTAuthentication]
    permission_classes = [IsCoreSuperAdmin]

    def delete(self, request, actor_id):
        actor = OTARemittanceActor.objects.filter(
            id=actor_id, scope=OTARemittanceActor.Scope.ADMIN, is_active=True,
        ).first()
        if not actor:
            raise ValidationError({"actor_id": "Active actor was not found."})
        actor.is_active, actor.removed_at = False, timezone.now()
        actor.save(update_fields=["is_active", "removed_at"])
        return success({"removed": True, "actor": _actor_data(actor)})


class HotelRemittanceListView(APIView):
    permission_classes = [HasBookingAdminKey]
    business_scoped = True

    def get(self, request):
        hotel = _hotel_from_request(request)
        records = OTARemittance.objects.filter(hotel=hotel).select_related("hotel").prefetch_related("items")
        return success({"count": records.count(), "remittances": [remittance_data(item) for item in records]})


class HotelRemittanceDetailView(APIView):
    permission_classes = [HasBookingAdminKey]
    business_scoped = True

    def get(self, request, remittance_id):
        hotel = _hotel_from_request(request)
        record = OTARemittance.objects.select_related("hotel").filter(id=remittance_id, hotel=hotel).first()
        if not record:
            raise ValidationError({"remittance_id": "Remittance was not found."})
        return success({"remittance": remittance_data(record, detail=True)})


class HotelRemittanceReceiveView(APIView):
    permission_classes = [HasBookingAdminKey]
    business_scoped = True

    @transaction.atomic
    def post(self, request, remittance_id):
        hotel = _hotel_from_request(request)
        record = OTARemittance.objects.select_for_update().filter(id=remittance_id, hotel=hotel).first()
        if not record or record.status != OTARemittance.Status.PAID:
            raise ValidationError({"status": "Only a paid remittance can be confirmed as received."})
        actor = _require_passcode_actor(
            request.data, scope=OTARemittanceActor.Scope.HOTEL,
            right="checker", hotel=hotel,
        )
        record.status, record.received_at, record.received_by = OTARemittance.Status.RECEIVED, timezone.now(), actor
        record.save(update_fields=["status", "received_at", "received_by", "updated_at"])
        record.items.values_list("booking_id", flat=True)
        Booking.objects.filter(remittance_items__remittance=record).update(
            settlement_status=Booking.SettlementStatus.RECEIVED, settled_at=record.received_at,
        )
        OTARemittanceAuditLog.objects.create(remittance=record, action="received", actor=actor, actor_name=actor.name)
        return success({"remittance": remittance_data(record, detail=True)})


class SuperAdminRemittanceListView(APIView):
    authentication_classes = [CoreJWTAuthentication]
    permission_classes = [IsCoreSuperAdmin]

    def get(self, request):
        all_records = OTARemittance.objects.select_related("hotel").prefetch_related("items")
        business_id = request.query_params.get("business_id")
        if business_id:
            all_records = all_records.filter(hotel__core_business_id=business_id)
        outstanding_statuses = [OTARemittance.Status.PENDING, OTARemittance.Status.CHECKED]
        hotel_summaries = {}
        for item in all_records.filter(status__in=outstanding_statuses):
            summary = hotel_summaries.setdefault(item.hotel_id, {
                "hotel_id": item.hotel_id,
                "core_business_id": item.hotel.core_business_id,
                "hotel_name": item.hotel.name,
                "currency": item.currency,
                "outstanding_remittance_count": 0,
                "unpaid_week_count": 0,
                "outstanding_net_payout": Decimal("0"),
                "oldest_unpaid_period_from": item.period_from,
            })
            summary["outstanding_remittance_count"] += 1
            summary["unpaid_week_count"] += 1
            summary["outstanding_net_payout"] += item.net_remittance_amount
            summary["oldest_unpaid_period_from"] = min(
                summary["oldest_unpaid_period_from"], item.period_from
            )

        records = all_records
        status = request.query_params.get("status")
        if status:
            records = records.filter(status=status)
        return success({
            "count": records.count(),
            "hotel_count": len(hotel_summaries),
            "hotel_summaries": list(hotel_summaries.values()),
            "remittances": [remittance_data(item) for item in records],
        })


class SuperAdminRemittanceDetailView(APIView):
    authentication_classes = [CoreJWTAuthentication]
    permission_classes = [IsCoreSuperAdmin]

    def get(self, request, remittance_id):
        record = OTARemittance.objects.select_related("hotel").filter(id=remittance_id).first()
        if not record:
            raise ValidationError({"remittance_id": "Remittance was not found."})
        return success({"remittance": remittance_data(record, detail=True)})


class SuperAdminRemittanceCheckView(APIView):
    authentication_classes = [CoreJWTAuthentication]
    permission_classes = [IsCoreSuperAdmin]

    @transaction.atomic
    def post(self, request, remittance_id):
        record = OTARemittance.objects.select_for_update().filter(id=remittance_id).first()
        if not record or record.status != OTARemittance.Status.PENDING:
            raise ValidationError({"status": "Only a pending remittance can be checked."})
        actor = _require_passcode_actor(request.data, scope=OTARemittanceActor.Scope.ADMIN, right="checker")
        record.status, record.checked_at, record.checked_by = OTARemittance.Status.CHECKED, timezone.now(), actor
        record.save(update_fields=["status", "checked_at", "checked_by", "updated_at"])
        Booking.objects.filter(remittance_items__remittance=record).update(settlement_status=Booking.SettlementStatus.CHECKED)
        OTARemittanceAuditLog.objects.create(remittance=record, action="checked", actor=actor, actor_name=actor.name)
        return success({"remittance": remittance_data(record, detail=True)})


class SuperAdminRemittancePayView(APIView):
    authentication_classes = [CoreJWTAuthentication]
    permission_classes = [IsCoreSuperAdmin]

    @transaction.atomic
    def post(self, request, remittance_id):
        record = OTARemittance.objects.select_for_update().filter(id=remittance_id).first()
        if not record or record.status != OTARemittance.Status.CHECKED:
            raise ValidationError({"status": "Remittance must be checked before payment."})
        actor = _require_passcode_actor(request.data, scope=OTARemittanceActor.Scope.ADMIN, right="payer")
        record.status, record.paid_at, record.paid_by = OTARemittance.Status.PAID, timezone.now(), actor
        record.reference = str(request.data.get("payment_reference") or record.reference)
        record.save(update_fields=["status", "paid_at", "paid_by", "reference", "updated_at"])
        Booking.objects.filter(remittance_items__remittance=record).update(
            settlement_status=Booking.SettlementStatus.PAID, settled_at=record.paid_at,
        )
        OTARemittanceAuditLog.objects.create(
            remittance=record, action="paid", actor=actor, actor_name=actor.name,
            metadata={"payment_reference": record.reference},
        )
        return success({"remittance": remittance_data(record, detail=True)})


class SuperAdminCommissionRateView(APIView):
    authentication_classes = [CoreJWTAuthentication]
    permission_classes = [IsCoreSuperAdmin]

    def get(self, request):
        configured = {item.package: item.rate for item in OTACommissionRate.objects.all()}
        return success({"rates": [
            {"package": package, "rate": configured.get(package, Decimal("5.00"))}
            for package in (Hotel.Package.OTA, Hotel.Package.OTA_PMS)
        ]})

    def post(self, request):
        package = request.data.get("package")
        if package not in {Hotel.Package.OTA, Hotel.Package.OTA_PMS}:
            raise ValidationError({"package": "Use ota or ota_pms."})
        try:
            rate = Decimal(str(request.data.get("rate")))
        except Exception as exc:
            raise ValidationError({"rate": "A numeric percentage is required."}) from exc
        if rate < 0 or rate > 100:
            raise ValidationError({"rate": "Rate must be between 0 and 100."})
        item, _ = OTACommissionRate.objects.update_or_create(package=package, defaults={"rate": rate})
        return success({"package": item.package, "rate": item.rate})
