from datetime import timedelta
from decimal import Decimal, InvalidOperation
from io import BytesIO
import re
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from django.contrib.auth.hashers import check_password, make_password
from django.db import transaction
from django.db.models import OuterRef, Q, Subquery
from django.db.models.functions import Lower
from django.http import FileResponse
from django.utils import timezone
from django.utils.dateparse import parse_date
from rest_framework.exceptions import ValidationError
from rest_framework.views import APIView

from booking.models import (
    Booking,
    Hotel,
    OTACommissionRate,
    OTARemittance,
    OTARemittanceActor,
    OTARemittanceAuditLog,
    OTARevenueLedger,
)
from booking.authentication import CoreJWTAuthentication
from booking.permissions import HasBookingAdminKey, IsCoreSuperAdmin
from booking.remittance_services import (
    active_payout_period,
    current_week_period,
    hotel_timezone,
    refresh_current_week_remittance,
    sync_all_payment_ledgers,
)
from config.response_formatter import success


ADMIN_RIGHTS = {"checker", "payer"}
PASSCODE_RE = re.compile(r"^[0-9]{4}$")


def _validate_actor_passcode(passcode):
    if not PASSCODE_RE.fullmatch(passcode):
        raise ValidationError({"passcode": "Passcode must be exactly 4 digits."})


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


def _action_actor_data(actor):
    if not actor:
        return None
    return {
        "id": str(actor.id),
        "name": actor.name,
    }


def _merchant_verification_data(hotel):
    snapshot = hotel.core_snapshot or {}
    verification = (
        snapshot.get("merchant_verification")
        or snapshot.get("business_verification")
        or snapshot.get("verification")
        or {}
    )
    if not isinstance(verification, dict):
        verification = {}
    nested = [
        value for key in (
            "bank_information", "bank_account", "payment_information",
            "settlement_information", "mobile_wallet",
        )
        if isinstance((value := verification.get(key)), dict)
    ]
    sources = [verification, *nested, snapshot]

    def pick(*keys, default=""):
        for source in sources:
            for key in keys:
                value = source.get(key)
                if value not in (None, ""):
                    return value
        return default

    return {
        "hotel_name": hotel.name,
        "contact_phone": pick(
            "business_contact_phone_number", "contact_phone", "phone", "phone_no",
            default=hotel.phone,
        ),
        "email": pick("business_contact_email", "contact_email", "email", "business_email"),
        "address": pick("hotel_address", "business_address", "address", default=hotel.address),
        "payment_account_number": pick(
            "payment_account_number", "bank_account_number", "account_number",
            "mobile_wallet_account", "mobile_wallet_number",
        ),
        "beneficiary_name": pick("beneficiary_name", "account_name", "account_holder_name"),
        "bank_or_mobile_wallet_name": pick(
            "bank_or_mobile_wallet_name", "bank_name", "wallet_name", "payment_provider_name",
        ),
        "branch": pick("branch", "bank_branch", "branch_name"),
    }


def _payment_receipt_url(remittance):
    if not remittance.payment_receipt:
        return None
    try:
        return remittance.payment_receipt.url
    except (ValueError, NotImplementedError):
        return None


def _absolute_url(request, url):
    if not url:
        return None
    return request.build_absolute_uri(url) if request is not None else url


def _filter_remittance_search(queryset, raw_search):
    search = str(raw_search or "").strip()
    if not search:
        return queryset
    lookup = Q(reference__icontains=search)
    if search.isdigit():
        lookup |= Q(id=int(search))
    return queryset.filter(lookup)


def _hotel_city(hotel):
    snapshot = hotel.core_snapshot or {}
    branches = [item for item in snapshot.get("branches", []) if isinstance(item, dict)]
    main_branch = next((item for item in branches if item.get("is_main") is True), None)
    branch = main_branch or (branches[0] if branches else {})
    value = (
        snapshot.get("city") or snapshot.get("city_name")
        or branch.get("city") or branch.get("city_name")
    )
    if isinstance(value, dict):
        return {"id": value.get("id"), "name": value.get("name") or value.get("title")}
    return value


def _hotel_refund_policy(hotel):
    policy = (hotel.core_snapshot or {}).get("hotel_cancellation_policy")
    if not isinstance(policy, dict) or not policy.get("type"):
        return {"type": "non_refundable", "name": "Non-Refundable"}
    return policy


def _remittance_local_datetime(remittance, value):
    try:
        hotel_tz = ZoneInfo(remittance.hotel.timezone or "Asia/Yangon")
    except ZoneInfoNotFoundError:
        hotel_tz = ZoneInfo("Asia/Yangon")
    return timezone.localtime(value, hotel_tz)


def _remittance_item_data(remittance, item):
    local_effective_at = _remittance_local_datetime(remittance, item.ledger_entry.effective_at)
    return {
        "id": item.id,
        "booking_id": str(item.booking_id),
        "booking_code": item.booking.booking_code,
        "payment_id": str(item.ledger_entry.payment_id),
        "type": item.item_type,
        "effective_at": item.ledger_entry.effective_at,
        "effective_date": local_effective_at.date(),
        "effective_time": local_effective_at.strftime("%H:%M"),
        "check_in": item.booking.check_in,
        "check_out": item.booking.check_out,
        "gross_amount": item.gross_amount,
        "amount": item.gross_amount,
        "tax_amount": item.tax_amount,
        "service_charge_amount": item.service_charge_amount,
        "refund_amount": item.refund_amount,
        "booking_settlement_status": item.booking.settlement_status,
    }


def _group_remittance_items(remittance, items):
    section_types = (
        ("booking_collection", "Booking Collection"),
        ("held_release", "Release of Held Funds"),
    )
    refunds_by_payment = {}
    for item in items:
        if item.item_type == "refund":
            payment_id = item.ledger_entry.payment_id
            refunds_by_payment[payment_id] = (
                refunds_by_payment.get(payment_id, Decimal("0")) + item.refund_amount
            )

    section_rows = {key: [] for key, _label in section_types}
    matched_refund_payments = set()
    for item in items:
        if item.item_type not in section_rows:
            continue
        row = _remittance_item_data(remittance, item)
        payment_id = item.ledger_entry.payment_id
        if payment_id in refunds_by_payment:
            row["refund_amount"] = refunds_by_payment[payment_id]
            matched_refund_payments.add(payment_id)
        section_rows[item.item_type].append(row)

    # A refund can occur in a later remittance period than its original
    # collection/release. Keep it visible in Booking Collection with amount 0.
    for item in items:
        payment_id = item.ledger_entry.payment_id
        if item.item_type != "refund" or payment_id in matched_refund_payments:
            continue
        row = _remittance_item_data(remittance, item)
        row["gross_amount"] = Decimal("0")
        row["amount"] = Decimal("0")
        section_rows["booking_collection"].append(row)

    sections = []
    for section_type, label in section_types:
        rows = section_rows[section_type]
        date_groups = {}
        for row in rows:
            date_groups.setdefault(row["effective_date"], []).append(row)
        groups = []
        for effective_date in sorted(date_groups, reverse=True):
            group_rows = sorted(
                date_groups[effective_date],
                key=lambda row: row["effective_at"],
                reverse=True,
            )
            groups.append({"date": effective_date, "items": group_rows})
        sections.append({
            "type": section_type,
            "label": label,
            "total_amount": sum((row["amount"] for row in rows), Decimal("0")),
            "total_refund": sum((row["refund_amount"] for row in rows), Decimal("0")),
            "date_groups": groups,
        })
    return sections


def _pagination(request):
    try:
        limit = min(max(int(request.query_params.get("limit", 20)), 1), 100)
        offset = int(request.query_params.get("offset", 1))
    except (TypeError, ValueError) as exc:
        raise ValidationError({"pagination": "limit and offset must be integers."}) from exc
    if offset < 1:
        raise ValidationError({"offset": "Must be a page number starting from 1."})
    return offset, limit


def _ledger_item_data(hotel, entry, amount=None):
    local_effective_at = timezone.localtime(entry.effective_at, hotel_timezone(hotel))
    return {
        "ledger_id": str(entry.id),
        "booking_id": str(entry.booking_id),
        "booking_code": entry.booking.booking_code,
        "payment_id": str(entry.payment_id),
        "type": entry.event_type,
        "effective_at": entry.effective_at,
        "effective_date": local_effective_at.date(),
        "effective_time": local_effective_at.strftime("%H:%M"),
        "check_in": entry.booking.check_in,
        "check_out": entry.booking.check_out,
        "currency": entry.booking.currency,
        "gross_amount": entry.gross_amount,
        "amount": entry.gross_amount if amount is None else amount,
        "tax_amount": entry.tax_amount,
        "service_charge_amount": entry.service_charge_amount,
        "refund_amount": entry.refund_amount,
        "settlement_status": entry.booking.settlement_status,
    }


def _date_grouped_rows(rows):
    grouped = {}
    for row in rows:
        grouped.setdefault(row["effective_date"], []).append(row)
    return [
        {
            "date": effective_date,
            "items": sorted(
                grouped[effective_date],
                key=lambda item: item["effective_at"],
                reverse=True,
            ),
        }
        for effective_date in sorted(grouped, reverse=True)
    ]


def remittance_data(remittance, detail=False, request=None):
    available_amount = remittance.net_remittance_amount - remittance.adjustment_amount
    adjustment_operator = "-" if remittance.adjustment_amount < 0 else "+"
    adjustment_input_amount = abs(remittance.adjustment_amount)
    display_status = remittance.status
    receipt_url = _absolute_url(request, _payment_receipt_url(remittance))
    hotel_pdf_url = _absolute_url(
        request, f"/api/v1/admin/remittances/{remittance.id}/pdf/"
    )
    superadmin_pdf_url = _absolute_url(
        request, f"/api/v1/superadmin/remittances/{remittance.id}/pdf/"
    )
    receipt_is_available = bool(
        receipt_url
        and remittance.status in {
            OTARemittance.Status.PAID,
            OTARemittance.Status.RECEIVED,
        }
    )
    data = {
        "id": remittance.id,
        "reference": remittance.reference,
        "hotel_id": remittance.hotel_id,
        "core_business_id": remittance.hotel.core_business_id,
        "hotel_name": remittance.hotel.name,
        "payout_option": remittance.hotel.remittance_payout_option,
        "payout_option_label": remittance.hotel.get_remittance_payout_option_display(),
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
        "available": available_amount,
        "adjustment_amount": remittance.adjustment_amount,
        "adjustment_operator": adjustment_operator,
        "adjustment_input_amount": adjustment_input_amount,
        "adjustment_display": (
            f"{adjustment_operator} {remittance.currency} "
            f"{adjustment_input_amount:,.2f}"
        ),
        "adjustment_note": remittance.adjustment_note,
        "net_payout": remittance.net_remittance_amount,
        "payment_reference": remittance.payment_reference,
        "payment_receipt_url": receipt_url if receipt_is_available else None,
        "can_view_payment_receipt": receipt_is_available,
        "status": display_status,
        "system_generated": remittance.system_generated,
        "historical_backfill": remittance.historical_backfill,
        "checked_at": remittance.checked_at,
        "checker": _action_actor_data(remittance.checked_by),
        "paid_at": remittance.paid_at,
        "payer": _action_actor_data(remittance.paid_by),
        "received_at": remittance.received_at,
        "received_by": _action_actor_data(remittance.received_by),
        "can_confirm_receipt": remittance.status == OTARemittance.Status.PAID,
        "receipt_confirmation_required": remittance.status == OTARemittance.Status.PAID,
        "can_view_remittance_pdf": True,
        "hotel_statement_pdf_url": hotel_pdf_url,
        "superadmin_statement_pdf_url": superadmin_pdf_url,
        "booking_ids": list(dict.fromkeys(item.booking_id for item in remittance.items.all())),
    }
    if detail:
        items = list(
            remittance.items.select_related("booking", "ledger_entry")
            .order_by("-ledger_entry__effective_at", "-id")
        )
        data["items"] = [_remittance_item_data(remittance, item) for item in items]
        data["grouped_items"] = _group_remittance_items(remittance, items)
        data["audit_logs"] = [
            _audit_data(log)
            for log in remittance.audit_logs.select_related("actor")
        ]
        data["merchant_verification"] = _merchant_verification_data(remittance.hotel)
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
    _validate_actor_passcode(passcode)
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
        actors = OTARemittanceActor.objects.filter(
            scope=OTARemittanceActor.Scope.HOTEL,
            hotel=hotel,
        )
        if request.query_params.get("include_removed", "false").lower() != "true":
            actors = actors.filter(is_active=True)
        return success({"actors": [_actor_data(actor) for actor in actors]})

    def post(self, request):
        hotel = _hotel_from_request(request)
        name, passcode = str(request.data.get("name") or "").strip(), str(request.data.get("passcode") or "")
        if not name:
            raise ValidationError({"name": "Name is required."})
        _validate_actor_passcode(passcode)
        actor = OTARemittanceActor.objects.create(
            scope=OTARemittanceActor.Scope.HOTEL, hotel=hotel, name=name,
            passcode_hash=make_password(passcode), access_rights=["payee"],
        )
        return success({"actor": _actor_data(actor)})


class HotelRemittanceActorDeleteView(APIView):
    permission_classes = [HasBookingAdminKey]
    business_scoped = True

    def delete(self, request, actor_id):
        hotel = _hotel_from_request(request)
        actor = OTARemittanceActor.objects.filter(
            id=actor_id,
            scope=OTARemittanceActor.Scope.HOTEL,
            hotel=hotel,
        ).first()
        if not actor:
            raise ValidationError({"actor_id": "Payee was not found."})
        if not actor.is_active:
            return success({"removed": True, "already_removed": True, "actor": _actor_data(actor)})
        actor.is_active, actor.removed_at = False, timezone.now()
        actor.save(update_fields=["is_active", "removed_at"])
        return success({"removed": True, "already_removed": False, "actor": _actor_data(actor)})


class SuperAdminRemittanceActorListCreateView(APIView):
    authentication_classes = [CoreJWTAuthentication]
    permission_classes = [IsCoreSuperAdmin]

    def get(self, request):
        actors = OTARemittanceActor.objects.filter(scope=OTARemittanceActor.Scope.ADMIN)
        if request.query_params.get("include_removed", "false").lower() != "true":
            actors = actors.filter(is_active=True)
        return success({"actors": [_actor_data(actor) for actor in actors]})

    def post(self, request):
        name, passcode = str(request.data.get("name") or "").strip(), str(request.data.get("passcode") or "")
        raw_rights = request.data.get("access_rights") or []
        if isinstance(raw_rights, str):
            raw_rights = [value.strip() for value in raw_rights.split(",") if value.strip()]
        rights = list(dict.fromkeys(raw_rights))
        if not name:
            raise ValidationError({"name": "Name is required."})
        _validate_actor_passcode(passcode)
        if not rights or set(rights) - ADMIN_RIGHTS:
            raise ValidationError({
                "access_rights": "Select checker, payer, or both."
            })
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
            id=actor_id, scope=OTARemittanceActor.Scope.ADMIN,
        ).first()
        if not actor:
            raise ValidationError({"actor_id": "Actor was not found."})
        if not actor.is_active:
            return success({"removed": True, "already_removed": True, "actor": _actor_data(actor)})
        actor.is_active, actor.removed_at = False, timezone.now()
        actor.save(update_fields=["is_active", "removed_at"])
        return success({"removed": True, "already_removed": False, "actor": _actor_data(actor)})


class HotelRemittanceListView(APIView):
    permission_classes = [HasBookingAdminKey]
    business_scoped = True

    def get(self, request):
        hotel = _hotel_from_request(request)
        offset, limit = _pagination(request)
        records = (
            OTARemittance.objects.filter(
                hotel=hotel,
                status__in=[
                    OTARemittance.Status.PAID,
                    OTARemittance.Status.RECEIVED,
                ],
            )
            .select_related("hotel", "checked_by", "paid_by", "received_by")
            .prefetch_related("items")
            .order_by("-period_to", "-id")
        )
        search = request.query_params.get("search") or request.query_params.get(
            "remittance_id"
        )
        records = _filter_remittance_search(records, search)
        total_count = records.count()
        start = (offset - 1) * limit
        page_records = list(records[start:start + limit])
        results = [remittance_data(item, request=request) for item in page_records]
        total_pages = (total_count + limit - 1) // limit if total_count else 0
        return success({
            "count": total_count,
            "offset": offset,
            "limit": limit,
            "total_pages": total_pages,
            "has_previous": offset > 1,
            "has_next": offset < total_pages,
            "search": str(search or "").strip(),
            "results": results,
        })


class HotelAvailableRevenueView(APIView):
    permission_classes = [HasBookingAdminKey]
    business_scoped = True

    def get(self, request):
        hotel = _hotel_from_request(request)
        local_today = timezone.now().astimezone(hotel_timezone(hotel)).date()
        period_from, period_to = active_payout_period(hotel, local_today)
        record = refresh_current_week_remittance(hotel)
        blocked_by_first_period = False
        if (
            hotel.remittance_payout_option == Hotel.RemittancePayoutOption.TWICE_WEEKLY
            and local_today.weekday() >= 3
        ):
            week_from, _week_to = current_week_period(local_today, hotel=hotel)
            first_period = OTARemittance.objects.filter(
                hotel=hotel,
                period_from=week_from,
                period_to=week_from + timedelta(days=2),
            ).select_related("hotel").first()
            if first_period and first_period.status not in {
                OTARemittance.Status.PAID,
                OTARemittance.Status.RECEIVED,
            }:
                record = first_period
                period_from, period_to = first_period.period_from, first_period.period_to
                blocked_by_first_period = True
        if not record:
            return success({
                "payout_option": hotel.remittance_payout_option,
                "payout_option_label": hotel.get_remittance_payout_option_display(),
                "period_from": period_from,
                "period_to": period_to,
                "blocked_by_first_period": blocked_by_first_period,
                "currency": hotel.base_currency,
                "status": OTARemittance.Status.ACCRUING,
                "booking_collection": Decimal("0"),
                "released_held_funds": Decimal("0"),
                "tax_excluded_from_commission": Decimal("0"),
                "service_charge_excluded_from_commission": Decimal("0"),
                "commission": Decimal("0"),
                "refund": Decimal("0"),
                "available": Decimal("0"),
                "remittance": None,
                "grouped_items": [],
            })
        data = remittance_data(record, detail=True, request=request)
        return success({
            "payout_option": hotel.remittance_payout_option,
            "payout_option_label": hotel.get_remittance_payout_option_display(),
            "period_from": record.period_from,
            "period_to": record.period_to,
            "blocked_by_first_period": blocked_by_first_period,
            "currency": record.currency,
            "status": record.status,
            "booking_collection": record.gross_booking_amount,
            "released_held_funds": record.released_held_funds_amount,
            "tax_excluded_from_commission": record.tax_amount,
            "service_charge_excluded_from_commission": record.service_charge_amount,
            "commission_rate": record.commission_rate,
            "commission": record.commission_amount,
            "refund": record.refund_amount,
            "available": record.net_remittance_amount,
            "remittance": data,
            "grouped_items": data["grouped_items"],
        })


class SuperAdminHotelPayoutOptionView(APIView):
    authentication_classes = [CoreJWTAuthentication]
    permission_classes = [IsCoreSuperAdmin]

    def get(self, request, hotel_id):
        hotel = Hotel.objects.filter(id=hotel_id).first()
        if not hotel:
            raise ValidationError({"hotel_id": "Hotel was not found."})
        return success({
            "hotel_id": hotel.id,
            "core_business_id": hotel.core_business_id,
            "hotel_name": hotel.name,
            "payout_option": hotel.remittance_payout_option,
            "payout_option_label": hotel.get_remittance_payout_option_display(),
            "choices": [
                {"value": value, "label": label}
                for value, label in Hotel.RemittancePayoutOption.choices
            ],
        })

    @transaction.atomic
    def post(self, request, hotel_id):
        hotel = Hotel.objects.select_for_update().filter(id=hotel_id).first()
        if not hotel:
            raise ValidationError({"hotel_id": "Hotel was not found."})
        payout_option = str(request.data.get("payout_option") or "").strip()
        valid_options = {value for value, _label in Hotel.RemittancePayoutOption.choices}
        if payout_option not in valid_options:
            raise ValidationError({
                "payout_option": "Use weekly or twice_weekly."
            })
        if payout_option == hotel.remittance_payout_option:
            return success({
                "changed": False,
                "hotel_id": hotel.id,
                "payout_option": hotel.remittance_payout_option,
                "payout_option_label": hotel.get_remittance_payout_option_display(),
            })
        week_from, week_to = current_week_period(hotel=hotel)
        locked_period = OTARemittance.objects.filter(
            hotel=hotel,
            period_from__gte=week_from,
            period_to__lte=week_to,
        ).exclude(status=OTARemittance.Status.ACCRUING).exists()
        if locked_period:
            raise ValidationError({
                "payout_option": (
                    "Current week already has a finalized remittance period. "
                    "Change the payout option after the current weekly cycle ends."
                )
            })
        OTARemittance.objects.filter(
            hotel=hotel,
            status=OTARemittance.Status.ACCRUING,
            period_from__gte=week_from,
            period_to__lte=week_to,
        ).delete()
        hotel.remittance_payout_option = payout_option
        hotel.save(update_fields=["remittance_payout_option"])
        refresh_current_week_remittance(hotel)
        return success({
            "changed": True,
            "hotel_id": hotel.id,
            "core_business_id": hotel.core_business_id,
            "hotel_name": hotel.name,
            "payout_option": hotel.remittance_payout_option,
            "payout_option_label": hotel.get_remittance_payout_option_display(),
        })


class HotelHeldFundsView(APIView):
    permission_classes = [HasBookingAdminKey]
    business_scoped = True

    def get(self, request):
        hotel = _hotel_from_request(request)
        sync_all_payment_ledgers(hotel=hotel)
        offset, limit = _pagination(request)
        released_payment_ids = OTARevenueLedger.objects.filter(
            hotel=hotel,
            event_type=OTARevenueLedger.EventType.HELD_RELEASE,
        ).values("payment_id")
        entries = list(
            OTARevenueLedger.objects.filter(
                hotel=hotel,
                event_type=OTARevenueLedger.EventType.HELD,
            )
            .exclude(payment_id__in=released_payment_ids)
            .select_related("booking", "payment")
            .order_by("-effective_at", "-id")
        )
        rows = []
        for entry in entries:
            remaining = max(entry.gross_amount - entry.payment.refunded_amount, Decimal("0"))
            if remaining <= 0:
                continue
            row = _ledger_item_data(hotel, entry, amount=remaining)
            row["held_amount"] = remaining
            row["refund_amount"] = entry.payment.refunded_amount
            row["release_date"] = entry.booking.check_in
            rows.append(row)
        total_count = len(rows)
        start = (offset - 1) * limit
        page_rows = rows[start:start + limit]
        total_pages = (total_count + limit - 1) // limit if total_count else 0
        return success({
            "count": total_count,
            "offset": offset,
            "limit": limit,
            "total_pages": total_pages,
            "has_previous": offset > 1,
            "has_next": offset < total_pages,
            "currency": hotel.base_currency,
            "total_held_fund": sum((row["held_amount"] for row in rows), Decimal("0")),
            "results": page_rows,
            "date_groups": _date_grouped_rows(page_rows),
        })


class HotelRefundsView(APIView):
    permission_classes = [HasBookingAdminKey]
    business_scoped = True

    def get(self, request):
        hotel = _hotel_from_request(request)
        sync_all_payment_ledgers(hotel=hotel)
        offset, limit = _pagination(request)
        entries = (
            OTARevenueLedger.objects.filter(
                hotel=hotel,
                event_type=OTARevenueLedger.EventType.REFUND,
            )
            .select_related("booking", "payment")
            .order_by("-effective_at", "-id")
        )
        total_count = entries.count()
        start = (offset - 1) * limit
        page_entries = list(entries[start:start + limit])
        rows = [_ledger_item_data(hotel, entry, amount=entry.refund_amount) for entry in page_entries]
        total_pages = (total_count + limit - 1) // limit if total_count else 0
        return success({
            "count": total_count,
            "offset": offset,
            "limit": limit,
            "total_pages": total_pages,
            "has_previous": offset > 1,
            "has_next": offset < total_pages,
            "currency": hotel.base_currency,
            "total_refund": sum(
                entries.values_list("refund_amount", flat=True), Decimal("0")
            ),
            "results": rows,
            "date_groups": _date_grouped_rows(rows),
        })


class HotelRemittanceDetailView(APIView):
    permission_classes = [HasBookingAdminKey]
    business_scoped = True

    def get(self, request, remittance_id):
        hotel = _hotel_from_request(request)
        record = OTARemittance.objects.select_related("hotel").filter(id=remittance_id, hotel=hotel).first()
        if not record:
            raise ValidationError({"remittance_id": "Remittance was not found."})
        return success({"remittance": remittance_data(record, detail=True, request=request)})


class HotelRemittancePDFView(APIView):
    permission_classes = [HasBookingAdminKey]
    business_scoped = True

    def get(self, request, remittance_id):
        hotel = _hotel_from_request(request)
        record = OTARemittance.objects.filter(id=remittance_id, hotel=hotel).first()
        if not record:
            raise ValidationError({"remittance_id": "Remittance was not found."})
        from booking.booking_services.remittance_pdf import render_remittance_statement_pdf
        return FileResponse(
            BytesIO(render_remittance_statement_pdf(record)),
            content_type="application/pdf",
            as_attachment=False,
            filename=f"{record.reference}.pdf",
        )


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
            right="payee", hotel=hotel,
        )
        record.status, record.received_at, record.received_by = OTARemittance.Status.RECEIVED, timezone.now(), actor
        record.save(update_fields=["status", "received_at", "received_by", "updated_at"])
        record.items.values_list("booking_id", flat=True)
        Booking.objects.filter(remittance_items__remittance=record).update(
            settlement_status=Booking.SettlementStatus.RECEIVED, settled_at=record.received_at,
        )
        OTARemittanceAuditLog.objects.create(remittance=record, action="received", actor=actor, actor_name=actor.name)
        return success({"remittance": remittance_data(record, detail=True, request=request)})


class SuperAdminRemittanceListView(APIView):
    authentication_classes = [CoreJWTAuthentication]
    permission_classes = [IsCoreSuperAdmin]

    def get(self, request):
        try:
            limit = min(max(int(request.query_params.get("limit", 20)), 1), 100)
            offset = int(request.query_params.get("offset", 1))
        except (TypeError, ValueError) as exc:
            raise ValidationError({"pagination": "limit and offset must be integers."}) from exc
        if offset < 1:
            raise ValidationError({"offset": "Must be a page number starting from 1."})

        paid_cutoff = timezone.now() - timedelta(days=1)
        outstanding_statuses = [OTARemittance.Status.PENDING, OTARemittance.Status.CHECKED]

        oldest_outstanding_status = (
            OTARemittance.objects.filter(
                hotel_id=OuterRef("pk"), status__in=outstanding_statuses,
            )
            .order_by("period_from", "id")
            .values("status")[:1]
        )
        recent_paid_status = (
            OTARemittance.objects.filter(
                hotel_id=OuterRef("pk"),
                status__in=[OTARemittance.Status.PAID, OTARemittance.Status.RECEIVED],
                paid_at__gte=paid_cutoff,
            )
            .order_by("-paid_at", "-id")
            .values("status")[:1]
        )
        hotels = Hotel.objects.filter(is_active=True)
        business_id = request.query_params.get("business_id")
        if business_id:
            hotels = hotels.filter(core_business_id=business_id)
        hotels = hotels.annotate(
            remittance_action_status=Subquery(oldest_outstanding_status),
            recent_paid_remittance_status=Subquery(recent_paid_status),
        )

        paid_statuses = [OTARemittance.Status.PAID, OTARemittance.Status.RECEIVED]
        payout_counts = {
            "all": hotels.count(),
            "weekly": hotels.filter(
                remittance_payout_option=Hotel.RemittancePayoutOption.WEEKLY,
            ).count(),
            "twice_weekly": hotels.filter(
                remittance_payout_option=Hotel.RemittancePayoutOption.TWICE_WEEKLY,
            ).count(),
        }
        payout_option = request.query_params.get("payout_option", "all")
        if payout_option in {
            Hotel.RemittancePayoutOption.WEEKLY,
            Hotel.RemittancePayoutOption.TWICE_WEEKLY,
        }:
            hotels = hotels.filter(remittance_payout_option=payout_option)
        elif payout_option != "all":
            raise ValidationError({"payout_option": "Use all, weekly, or twice_weekly."})

        action_counts = {
            "all": hotels.count(),
            "need_to_pay": hotels.filter(
                remittance_action_status=OTARemittance.Status.CHECKED,
            ).count(),
            "need_to_check": hotels.filter(
                remittance_action_status=OTARemittance.Status.PENDING,
            ).count(),
            "paid": hotels.filter(
                remittance_action_status__isnull=True,
                recent_paid_remittance_status__in=paid_statuses,
            ).count(),
            "need_to_action": hotels.filter(
                Q(remittance_action_status=OTARemittance.Status.PENDING)
                | Q(remittance_action_status=OTARemittance.Status.CHECKED)
            ).count(),
        }
        action_filter = request.query_params.get("action_filter", "all")
        if action_filter == "need_to_pay":
            hotels = hotels.filter(remittance_action_status=OTARemittance.Status.CHECKED)
        elif action_filter == "need_to_check":
            hotels = hotels.filter(remittance_action_status=OTARemittance.Status.PENDING)
        elif action_filter == "paid":
            hotels = hotels.filter(
                remittance_action_status__isnull=True,
                recent_paid_remittance_status__in=paid_statuses,
            )
        elif action_filter in {"need_to_action", "action"}:
            hotels = hotels.filter(
                Q(remittance_action_status=OTARemittance.Status.PENDING)
                | Q(remittance_action_status=OTARemittance.Status.CHECKED)
            )
            action_filter = "need_to_action"
        elif action_filter != "all":
            raise ValidationError({
                "action_filter": (
                    "Use all, need_to_action, need_to_check, need_to_pay, or paid."
                )
            })

        hotels = hotels.order_by(Lower("name"), "name", "id")
        total_count = hotels.count()
        start = (offset - 1) * limit
        page_hotels = list(hotels[start:start + limit])

        all_records = list(
            OTARemittance.objects.filter(hotel_id__in=[hotel.id for hotel in page_hotels])
            .exclude(status=OTARemittance.Status.ACCRUING)
            .select_related("hotel", "checked_by", "paid_by", "received_by")
            .prefetch_related("items")
        )
        records_by_hotel = {hotel.id: [] for hotel in page_hotels}
        for record in all_records:
            records_by_hotel[record.hotel_id].append(record)

        hotel_rows = []
        for hotel in page_hotels:
            hotel_records = records_by_hotel[hotel.id]
            outstanding = sorted(
                (item for item in hotel_records if item.status in outstanding_statuses),
                key=lambda item: (item.period_from, item.id),
            )
            recent_paid = sorted(
                (
                    item for item in hotel_records
                    if item.status in paid_statuses
                    and item.paid_at
                    and item.paid_at >= paid_cutoff
                ),
                key=lambda item: (item.paid_at, item.id),
                reverse=True,
            )
            # An unpaid period always takes priority. A paid badge remains visible
            # for only 24 hours after payment, regardless of payout cadence.
            action_record = outstanding[0] if outstanding else (recent_paid[0] if recent_paid else None)
            action = None
            if action_record:
                action = {
                    OTARemittance.Status.PENDING: "pay",
                    OTARemittance.Status.CHECKED: "pay",
                    OTARemittance.Status.PAID: "paid",
                    OTARemittance.Status.RECEIVED: "paid",
                }.get(action_record.status)

            outstanding_net_payout = sum(
                (item.net_remittance_amount for item in outstanding), Decimal("0")
            )
            refund_policy = _hotel_refund_policy(hotel)
            hotel_rows.append({
                "hotel_id": hotel.id,
                "core_business_id": hotel.core_business_id,
                "hotel_name": hotel.name,
                "package": hotel.package,
                "payout_option": hotel.remittance_payout_option,
                "payout_option_label": hotel.get_remittance_payout_option_display(),
                "refund_policy_type": refund_policy["type"],
                "refund_policy": refund_policy,
                "address": hotel.address,
                "city": _hotel_city(hotel),
                "currency": hotel.base_currency,
                "outstanding_remittance_count": len(outstanding),
                "unpaid_week_count": len(outstanding),
                "outstanding_net_payout": outstanding_net_payout,
                "oldest_unpaid_period_from": outstanding[0].period_from if outstanding else None,
                "action": action,
                "action_label": action.title() if action else None,
                "action_required": action == "pay",
                "can_check": bool(
                    action_record and action_record.status == OTARemittance.Status.PENDING
                ),
                "can_pay": bool(
                    action_record and action_record.status in outstanding_statuses
                ),
                "is_paid": action == "paid",
                "action_remittance_id": action_record.id if action_record and action else None,
                "checker": (
                    _action_actor_data(action_record.checked_by)
                    if action_record else None
                ),
                "payer": (
                    _action_actor_data(action_record.paid_by)
                    if action_record and action_record.status in {
                        OTARemittance.Status.PAID, OTARemittance.Status.RECEIVED,
                    } else None
                ),
                "remittance": (
                    remittance_data(action_record, request=request)
                    if action_record else None
                ),
            })

        total_pages = (total_count + limit - 1) // limit if total_count else 0
        return success({
            "count": total_count,
            "hotel_count": total_count,
            "offset": offset,
            "limit": limit,
            "total_pages": total_pages,
            "has_previous": offset > 1,
            "has_next": offset < total_pages,
            "action_filter": action_filter,
            "action_counts": action_counts,
            "payout_option": payout_option,
            "payout_counts": payout_counts,
            "paid_display_hours": 24,
            "results": hotel_rows,
            "hotel_summaries": hotel_rows,
        })


class SuperAdminRemittanceHistoryView(APIView):
    authentication_classes = [CoreJWTAuthentication]
    permission_classes = [IsCoreSuperAdmin]

    def get(self, request):
        try:
            limit = min(max(int(request.query_params.get("limit", 20)), 1), 100)
            offset = int(request.query_params.get("offset", 1))
        except (TypeError, ValueError) as exc:
            raise ValidationError({"pagination": "limit and offset must be integers."}) from exc
        if offset < 1:
            raise ValidationError({"offset": "Must be a page number starting from 1."})

        records = (
            OTARemittance.objects.select_related(
                "hotel", "checked_by", "paid_by", "received_by",
            )
            .exclude(status=OTARemittance.Status.ACCRUING)
            .prefetch_related("items")
            .order_by("-period_to", "hotel__name", "-id")
        )
        business_id = request.query_params.get("business_id")
        if business_id:
            records = records.filter(hotel__core_business_id=business_id)

        search = request.query_params.get("search") or request.query_params.get(
            "remittance_id"
        )
        records = _filter_remittance_search(records, search)

        status = request.query_params.get("status")
        valid_statuses = {
            choice for choice, _label in OTARemittance.Status.choices
            if choice != OTARemittance.Status.ACCRUING
        }
        if status:
            if status not in valid_statuses:
                raise ValidationError({"status": f"Use one of: {', '.join(sorted(valid_statuses))}."})
            records = records.filter(status=status)

        for parameter, lookup in (("period_from", "period_from__gte"), ("period_to", "period_to__lte")):
            raw_value = request.query_params.get(parameter)
            if raw_value:
                value = parse_date(raw_value)
                if not value:
                    raise ValidationError({parameter: "Use YYYY-MM-DD format."})
                records = records.filter(**{lookup: value})

        total_count = records.count()
        start = (offset - 1) * limit
        page_records = list(records[start:start + limit])
        total_pages = (total_count + limit - 1) // limit if total_count else 0
        return success({
            "count": total_count,
            "offset": offset,
            "limit": limit,
            "total_pages": total_pages,
            "has_previous": offset > 1,
            "has_next": offset < total_pages,
            "search": str(search or "").strip(),
            "results": [remittance_data(item, request=request) for item in page_records],
        })


class SuperAdminRemittanceDetailView(APIView):
    authentication_classes = [CoreJWTAuthentication]
    permission_classes = [IsCoreSuperAdmin]

    def get(self, request, remittance_id):
        record = OTARemittance.objects.select_related(
            "hotel", "checked_by", "paid_by", "received_by",
        ).filter(id=remittance_id).first()
        if not record:
            raise ValidationError({"remittance_id": "Remittance was not found."})
        return success({"remittance": remittance_data(record, detail=True, request=request)})


class SuperAdminRemittancePDFView(APIView):
    authentication_classes = [CoreJWTAuthentication]
    permission_classes = [IsCoreSuperAdmin]

    def get(self, request, remittance_id):
        record = OTARemittance.objects.filter(id=remittance_id).first()
        if not record:
            raise ValidationError({"remittance_id": "Remittance was not found."})
        from booking.booking_services.remittance_pdf import render_remittance_statement_pdf
        return FileResponse(
            BytesIO(render_remittance_statement_pdf(record)),
            content_type="application/pdf",
            as_attachment=False,
            filename=f"{record.reference}.pdf",
        )


class SuperAdminRemittanceCheckView(APIView):
    authentication_classes = [CoreJWTAuthentication]
    permission_classes = [IsCoreSuperAdmin]

    @transaction.atomic
    def post(self, request, remittance_id):
        record = (
            OTARemittance.objects.select_for_update()
            .select_related("hotel")
            .filter(id=remittance_id)
            .first()
        )
        if not record:
            raise ValidationError({"remittance_id": "Remittance was not found."})
        if record.status != OTARemittance.Status.PENDING:
            raise ValidationError({
                "status": "Only a pending remittance can be checked."
            })
        actor = _require_passcode_actor(
            request.data,
            scope=OTARemittanceActor.Scope.ADMIN,
            right="checker",
        )
        record.status = OTARemittance.Status.CHECKED
        record.checked_at = timezone.now()
        record.checked_by = actor
        record.save(update_fields=["status", "checked_at", "checked_by", "updated_at"])
        Booking.objects.filter(remittance_items__remittance=record).update(
            settlement_status=Booking.SettlementStatus.CHECKED,
            settled_at=None,
        )
        OTARemittanceAuditLog.objects.create(
            remittance=record,
            action="checked",
            actor=actor,
            actor_name=actor.name,
        )
        return success({
            "checked": True,
            "remittance": remittance_data(record, detail=True, request=request),
        })


class SuperAdminRemittancePayView(APIView):
    authentication_classes = [CoreJWTAuthentication]
    permission_classes = [IsCoreSuperAdmin]

    @transaction.atomic
    def post(self, request, remittance_id):
        record = (
            OTARemittance.objects.select_for_update()
            .select_related("hotel")
            .filter(id=remittance_id)
            .first()
        )
        if not record or record.status not in {
            OTARemittance.Status.PENDING,
            OTARemittance.Status.CHECKED,
        }:
            raise ValidationError({"status": "Only an unpaid remittance can be paid."})
        actor = _require_passcode_actor(request.data, scope=OTARemittanceActor.Scope.ADMIN, right="payer")
        raw_adjustment = request.data.get("adjustment_amount", "0")
        try:
            adjustment_input = Decimal(str(raw_adjustment)).quantize(Decimal("0.01"))
        except (InvalidOperation, TypeError, ValueError) as exc:
            raise ValidationError({"adjustment_amount": "Use a positive numeric amount, for example 5000."}) from exc
        if adjustment_input < 0:
            raise ValidationError({
                "adjustment_amount": "Amount must be positive; send + or - in adjustment_operator."
            })
        if adjustment_input > Decimal("999999999999.99"):
            raise ValidationError({"adjustment_amount": "Adjustment amount is too large."})
        adjustment_operator = str(request.data.get("adjustment_operator") or "+").strip()
        if adjustment_operator not in {"+", "-"}:
            raise ValidationError({"adjustment_operator": "Use + or -."})
        adjustment = adjustment_input if adjustment_operator == "+" else -adjustment_input
        adjustment_note = str(request.data.get("adjustment_note") or "").strip()
        if adjustment and not adjustment_note:
            raise ValidationError({"adjustment_note": "A note is required when an adjustment is applied."})
        available_amount = record.net_remittance_amount - record.adjustment_amount
        net_payout = available_amount + adjustment
        if net_payout < 0:
            raise ValidationError({"adjustment_amount": "Adjustment cannot make net payout negative."})
        receipt = request.FILES.get("receipt")
        if not receipt:
            raise ValidationError({"receipt": "Payment receipt JPEG, JPG, or PDF is required."})
        extension = str(receipt.name or "").lower().rsplit(".", 1)[-1]
        if extension not in {"jpg", "jpeg", "pdf"}:
            raise ValidationError({"receipt": "Only JPEG, JPG, or PDF files are accepted."})
        if receipt.size > 10 * 1024 * 1024:
            raise ValidationError({"receipt": "Payment receipt must not exceed 10 MB."})
        record.status, record.paid_at, record.paid_by = OTARemittance.Status.PAID, timezone.now(), actor
        record.adjustment_amount = adjustment
        record.adjustment_note = adjustment_note
        record.net_remittance_amount = net_payout
        record.payment_reference = str(request.data.get("payment_reference") or "").strip()
        record.payment_receipt = receipt
        record.save(update_fields=[
            "status", "paid_at", "paid_by", "adjustment_amount", "adjustment_note",
            "net_remittance_amount", "payment_reference", "payment_receipt", "updated_at",
        ])
        Booking.objects.filter(remittance_items__remittance=record).update(
            settlement_status=Booking.SettlementStatus.PAID, settled_at=record.paid_at,
        )
        OTARemittanceAuditLog.objects.create(
            remittance=record, action="paid", actor=actor, actor_name=actor.name,
            metadata={
                "available_amount": str(available_amount),
                "adjustment_operator": adjustment_operator,
                "adjustment_input_amount": str(adjustment_input),
                "adjustment_amount": str(adjustment),
                "adjustment_note": adjustment_note,
                "net_payout": str(net_payout),
                "payment_reference": record.payment_reference,
                "payment_receipt": record.payment_receipt.name,
            },
        )
        return success({"remittance": remittance_data(record, detail=True, request=request)})


class SuperAdminRemittanceReopenReceiptView(APIView):
    authentication_classes = [CoreJWTAuthentication]
    permission_classes = [IsCoreSuperAdmin]

    @transaction.atomic
    def post(self, request, remittance_id):
        record = (
            OTARemittance.objects.select_for_update()
            .select_related("hotel")
            .filter(id=remittance_id)
            .first()
        )
        if not record:
            raise ValidationError({"remittance_id": "Remittance was not found."})
        if record.status != OTARemittance.Status.RECEIVED:
            raise ValidationError({"status": "Only a received remittance can be reopened."})
        if record.received_by_id:
            raise ValidationError({
                "status": "This remittance was confirmed by the hotel and cannot be reopened."
            })
        actor = _require_passcode_actor(
            request.data,
            scope=OTARemittanceActor.Scope.ADMIN,
            right="payer",
        )
        reason = str(request.data.get("reason") or "").strip()
        record.status = OTARemittance.Status.PAID
        record.received_at = None
        record.received_by = None
        record.save(update_fields=["status", "received_at", "received_by", "updated_at"])
        Booking.objects.filter(remittance_items__remittance=record).update(
            settlement_status=Booking.SettlementStatus.PAID,
            settled_at=record.paid_at,
        )
        OTARemittanceAuditLog.objects.create(
            remittance=record,
            action="receipt_reopened",
            actor=actor,
            actor_name=actor.name,
            metadata={"reason": reason},
        )
        return success({
            "reopened": True,
            "remittance": remittance_data(record, detail=True, request=request),
        })


class SuperAdminRemittanceResetToPendingView(APIView):
    """Reset one remittance to the unpaid action state for controlled retesting."""

    authentication_classes = [CoreJWTAuthentication]
    permission_classes = [IsCoreSuperAdmin]

    @transaction.atomic
    def post(self, request, remittance_id):
        record = (
            OTARemittance.objects.select_for_update()
            # Only join the required, non-nullable hotel relation while locking.
            # PostgreSQL rejects FOR UPDATE when nullable actor relations create
            # outer joins on the locked query.
            .select_related("hotel")
            .filter(id=remittance_id)
            .first()
        )
        if not record:
            raise ValidationError({"remittance_id": "Remittance was not found."})
        if record.status == OTARemittance.Status.ACCRUING:
            raise ValidationError({
                "status": "A current accruing remittance cannot be reset."
            })

        actor = _require_passcode_actor(
            request.data,
            scope=OTARemittanceActor.Scope.ADMIN,
            right="payer",
        )
        reason = str(request.data.get("reason") or "Remittance action retest").strip()
        previous_status = record.status
        previous_receipt = record.payment_receipt.name if record.payment_receipt else ""
        available_amount = record.net_remittance_amount - record.adjustment_amount

        record.status = OTARemittance.Status.PENDING
        record.checked_at = None
        record.checked_by = None
        record.paid_at = None
        record.paid_by = None
        record.received_at = None
        record.received_by = None
        record.adjustment_amount = Decimal("0")
        record.adjustment_note = ""
        record.net_remittance_amount = available_amount
        record.payment_reference = ""
        record.payment_receipt = None
        record.save(update_fields=[
            "status", "checked_at", "checked_by", "paid_at", "paid_by",
            "received_at", "received_by", "adjustment_amount", "adjustment_note",
            "net_remittance_amount", "payment_reference", "payment_receipt", "updated_at",
        ])
        Booking.objects.filter(remittance_items__remittance=record).update(
            settlement_status=Booking.SettlementStatus.PENDING_REMITTANCE,
            settled_at=None,
        )
        OTARemittanceAuditLog.objects.create(
            remittance=record,
            action="reset_to_pending",
            actor=actor,
            actor_name=actor.name,
            metadata={
                "previous_status": previous_status,
                "previous_payment_receipt": previous_receipt,
                "reason": reason,
            },
        )
        return success({
            "reset": True,
            "action_filter": "need_to_action",
            "action": "pay",
            "remittance": remittance_data(record, detail=True, request=request),
        })


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
