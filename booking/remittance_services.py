from datetime import datetime, time, timedelta
from decimal import Decimal, ROUND_HALF_UP
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from django.db import transaction
from django.db.models import Q
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from booking.models import (
    Booking,
    Hotel,
    OTACommissionRate,
    OTARemittance,
    OTARemittanceAuditLog,
    OTARemittanceItem,
    OTARevenueLedger,
    Payment,
)


MONEY = Decimal("0.01")


def _money(value):
    return Decimal(value or 0).quantize(MONEY, rounding=ROUND_HALF_UP)


def hotel_timezone(hotel):
    try:
        return ZoneInfo(hotel.timezone or "Asia/Yangon")
    except ZoneInfoNotFoundError:
        return ZoneInfo("Asia/Yangon")


def is_non_refundable(booking):
    snapshot = booking.cancellation_policy_snapshot or {}
    values = [snapshot.get("type")] if snapshot.get("type") else [
        value.get("type") for value in snapshot.values() if isinstance(value, dict)
    ]
    values = [value for value in values if value]
    return bool(values) and set(values) == {"non_refundable"}


def commission_rate_for(hotel):
    configured = OTACommissionRate.objects.filter(package=hotel.package).values_list("rate", flat=True).first()
    return Decimal(configured if configured is not None else "5.00")


def payment_tax(payment):
    booking = payment.booking
    if booking.grand_total <= 0:
        return Decimal("0")
    return _money(booking.tax_total * payment.amount / booking.grand_total)


def payment_service_charge(payment):
    booking = payment.booking
    if booking.grand_total <= 0:
        return Decimal("0")
    return _money(booking.service_charge_total * payment.amount / booking.grand_total)


def _refund_time(payment):
    raw = (payment.metadata or {}).get("refunded_at")
    parsed = parse_datetime(str(raw)) if raw else None
    return parsed or payment.paid_at or payment.created_at


@transaction.atomic
def sync_payment_ledger(payment):
    payment = Payment.objects.select_related("booking__hotel").get(pk=payment.pk)
    booking = payment.booking
    if booking.source != Booking.Source.OTA or not payment.paid_at:
        return []
    if payment.status not in {Payment.Status.PAID, Payment.Status.PARTIALLY_REFUNDED, Payment.Status.REFUNDED}:
        return []

    held = not is_non_refundable(booking) and payment.paid_at.astimezone(
        hotel_timezone(booking.hotel)
    ).date() < booking.check_in
    initial_type = OTARevenueLedger.EventType.HELD if held else OTARevenueLedger.EventType.BOOKING_COLLECTION
    initial, _ = OTARevenueLedger.objects.update_or_create(
        event_key=f"payment:{payment.id}:{initial_type}",
        defaults={
            "hotel": booking.hotel,
            "booking": booking,
            "payment": payment,
            "event_type": initial_type,
            "gross_amount": payment.amount,
            "tax_amount": payment_tax(payment),
            "service_charge_amount": payment_service_charge(payment),
            "effective_at": payment.paid_at,
        },
    )
    entries = [initial]
    if held:
        local_release = datetime.combine(booking.check_in, time.min, tzinfo=hotel_timezone(booking.hotel))
        if timezone.now() >= local_release:
            release, _ = OTARevenueLedger.objects.update_or_create(
                event_key=f"payment:{payment.id}:held_release",
                defaults={
                    "hotel": booking.hotel,
                    "booking": booking,
                    "payment": payment,
                    "event_type": OTARevenueLedger.EventType.HELD_RELEASE,
                    "gross_amount": payment.amount,
                    "tax_amount": payment_tax(payment),
                    "service_charge_amount": payment_service_charge(payment),
                    "effective_at": local_release,
                },
            )
            entries.append(release)
            booking.settlement_status = Booking.SettlementStatus.AVAILABLE
        else:
            booking.settlement_status = Booking.SettlementStatus.HELD
    else:
        booking.settlement_status = Booking.SettlementStatus.AVAILABLE

    if payment.refunded_amount > 0:
        refund, _ = OTARevenueLedger.objects.update_or_create(
            event_key=f"payment:{payment.id}:refund",
            defaults={
                "hotel": booking.hotel,
                "booking": booking,
                "payment": payment,
                "event_type": OTARevenueLedger.EventType.REFUND,
                "refund_amount": payment.refunded_amount,
                "effective_at": _refund_time(payment),
            },
        )
        entries.append(refund)
    booking.save(update_fields=["settlement_status", "updated_at"])
    return entries


def sync_all_payment_ledgers(hotel=None):
    qs = Payment.objects.filter(
        booking__source=Booking.Source.OTA,
        status__in=[Payment.Status.PAID, Payment.Status.PARTIALLY_REFUNDED, Payment.Status.REFUNDED],
        paid_at__isnull=False,
    ).select_related("booking__hotel")
    if hotel:
        qs = qs.filter(booking__hotel=hotel)
    count = 0
    for payment in qs.iterator():
        sync_payment_ledger(payment)
        count += 1
    return count


@transaction.atomic
def generate_weekly_remittance(
    hotel, period_from, period_to, historical=False, completed=False,
    accruing=False,
):
    tz = hotel_timezone(hotel)
    start = datetime.combine(period_from, time.min, tzinfo=tz)
    end = datetime.combine(period_to + timedelta(days=1), time.min, tzinfo=tz)
    remittance = OTARemittance.objects.select_for_update().filter(
        hotel=hotel,
        period_from=period_from,
        period_to=period_to,
        currency=hotel.base_currency,
    ).first()
    if (
        remittance
        and remittance.items.exists()
        and (remittance.checked_by_id or remittance.paid_by_id or remittance.received_by_id)
    ):
        return remittance
    # A retry of the Monday finalizer must not roll an already checked/paid
    # statement back to pending. The rolling current-week record is the only
    # ordinary statement that is recalculated in place.
    if (
        remittance
        and not accruing
        and not historical
        and not completed
        and remittance.status != OTARemittance.Status.ACCRUING
        and remittance.items.exists()
    ):
        return remittance
    assigned_elsewhere = OTARemittanceItem.objects.all()
    if remittance:
        assigned_elsewhere = assigned_elsewhere.exclude(remittance=remittance)
    entries = list(
        OTARevenueLedger.objects.select_for_update().filter(
            hotel=hotel,
            effective_at__gte=start,
            effective_at__lt=end,
        ).filter(
            Q(event_type=OTARevenueLedger.EventType.BOOKING_COLLECTION)
            | Q(event_type=OTARevenueLedger.EventType.HELD_RELEASE)
            | Q(event_type=OTARevenueLedger.EventType.REFUND)
        ).exclude(
            id__in=assigned_elsewhere.values("ledger_entry_id")
        ).select_related("booking")
    )
    if not entries:
        return remittance
    collections = sum((e.gross_amount for e in entries if e.event_type == OTARevenueLedger.EventType.BOOKING_COLLECTION), Decimal("0"))
    releases = sum((e.gross_amount for e in entries if e.event_type == OTARevenueLedger.EventType.HELD_RELEASE), Decimal("0"))
    tax = sum((e.tax_amount for e in entries if e.event_type in {OTARevenueLedger.EventType.BOOKING_COLLECTION, OTARevenueLedger.EventType.HELD_RELEASE}), Decimal("0"))
    service_charge = sum((e.service_charge_amount for e in entries if e.event_type in {OTARevenueLedger.EventType.BOOKING_COLLECTION, OTARevenueLedger.EventType.HELD_RELEASE}), Decimal("0"))
    refunds = sum((e.refund_amount for e in entries if e.event_type == OTARevenueLedger.EventType.REFUND), Decimal("0"))
    if all(_money(value) == 0 for value in (collections, releases, tax, service_charge, refunds)):
        return None
    rate = commission_rate_for(hotel)
    commissionable = max(collections + releases - tax - service_charge, Decimal("0"))
    commission = _money(commissionable * rate / Decimal("100"))
    net = _money(collections + releases - commission - refunds)
    status = (
        OTARemittance.Status.ACCRUING if accruing
        else OTARemittance.Status.RECEIVED if completed
        else OTARemittance.Status.PENDING
    )
    remittance, created = OTARemittance.objects.get_or_create(
        hotel=hotel, period_from=period_from, period_to=period_to, currency=hotel.base_currency,
        defaults={
            "gross_booking_amount": _money(collections),
            "released_held_funds_amount": _money(releases),
            "tax_amount": _money(tax),
            "service_charge_amount": _money(service_charge),
            "refund_amount": _money(refunds),
            "commission_rate": rate,
            "commission_amount": commission,
            "net_remittance_amount": net,
            "status": status,
            "reference": f"V77-REM-{period_to:%Y%m%d}-{hotel.core_business_id}",
            "historical_backfill": historical,
            "checked_at": timezone.now() if completed else None,
            "paid_at": timezone.now() if completed else None,
            "received_at": timezone.now() if completed else None,
        },
    )
    if not created:
        remittance.gross_booking_amount = _money(collections)
        remittance.released_held_funds_amount = _money(releases)
        remittance.tax_amount = _money(tax)
        remittance.service_charge_amount = _money(service_charge)
        remittance.refund_amount = _money(refunds)
        remittance.commission_rate = rate
        remittance.commission_amount = commission
        remittance.net_remittance_amount = net
        remittance.status = status
        remittance.historical_backfill = historical
        remittance.checked_at = timezone.now() if completed else None
        remittance.paid_at = timezone.now() if completed else None
        remittance.received_at = timezone.now() if completed else None
        remittance.save()
    for entry in entries:
        OTARemittanceItem.objects.update_or_create(
            ledger_entry=entry,
            defaults={
                "remittance": remittance,
                "booking": entry.booking,
                "item_type": entry.event_type,
                "gross_amount": entry.gross_amount,
                "tax_amount": entry.tax_amount,
                "service_charge_amount": entry.service_charge_amount,
                "refund_amount": entry.refund_amount,
            },
        )
    bookings = Booking.objects.filter(id__in={entry.booking_id for entry in entries})
    bookings.update(
        settlement_status=(
            Booking.SettlementStatus.AVAILABLE if accruing
            else Booking.SettlementStatus.RECEIVED if completed
            else Booking.SettlementStatus.PENDING_REMITTANCE
        ),
        settled_at=timezone.now() if completed else None,
    )
    if created:
        OTARemittanceAuditLog.objects.create(
            remittance=remittance,
            action=(
                "current_week_accruing" if accruing
                else "historical_backfill" if historical
                else "system_generated"
            ),
            actor_name="System",
        )
    elif not accruing and remittance.audit_logs.filter(action="current_week_accruing").exists():
        OTARemittanceAuditLog.objects.get_or_create(
            remittance=remittance,
            action="weekly_finalized",
            defaults={"actor_name": "System"},
        )
    return remittance


def current_week_period(today=None, hotel=None):
    if today is None:
        today = (
            timezone.now().astimezone(hotel_timezone(hotel)).date()
            if hotel else timezone.localdate()
        )
    start = today - timedelta(days=today.weekday())
    return start, start + timedelta(days=6)


def active_payout_period(hotel, today=None):
    monday, sunday = current_week_period(today, hotel=hotel)
    local_today = today or timezone.now().astimezone(hotel_timezone(hotel)).date()
    if hotel.remittance_payout_option == Hotel.RemittancePayoutOption.TWICE_WEEKLY:
        wednesday = monday + timedelta(days=2)
        if local_today <= wednesday:
            return monday, wednesday
        return monday + timedelta(days=3), sunday
    return monday, sunday


def refresh_current_week_remittance(hotel, today=None, sync_ledger=True):
    if sync_ledger:
        sync_all_payment_ledgers(hotel=hotel)
    local_today = today or timezone.now().astimezone(hotel_timezone(hotel)).date()
    week_from, _week_to = current_week_period(local_today, hotel=hotel)
    start, end = active_payout_period(hotel, local_today)
    stale_periods = list(
        OTARemittance.objects.filter(
            hotel=hotel,
            status=OTARemittance.Status.ACCRUING,
            period_to__lt=local_today,
        ).values_list("period_from", "period_to")
    )
    for period_from, period_to in stale_periods:
        generate_weekly_remittance(hotel, period_from, period_to)
    if (
        hotel.remittance_payout_option == Hotel.RemittancePayoutOption.TWICE_WEEKLY
        and local_today.weekday() >= 3
    ):
        first_period_to = week_from + timedelta(days=2)
        generate_weekly_remittance(hotel, week_from, first_period_to)
    return generate_weekly_remittance(hotel, start, end, accruing=True)


def refresh_current_week_remittances(today=None):
    return [
        result
        for hotel in Hotel.objects.filter(is_active=True)
        if (result := refresh_current_week_remittance(
            hotel, today=today, sync_ledger=False,
        ))
    ]


def generate_previous_week_remittances(today=None):
    sync_all_payment_ledgers()
    results = []
    for hotel in Hotel.objects.filter(is_active=True):
        current_monday, _current_sunday = current_week_period(today, hotel=hotel)
        if hotel.remittance_payout_option == Hotel.RemittancePayoutOption.TWICE_WEEKLY:
            start = current_monday - timedelta(days=4)
            end = current_monday - timedelta(days=1)
        else:
            start = current_monday - timedelta(days=7)
            end = current_monday - timedelta(days=1)
        result = generate_weekly_remittance(hotel, start, end)
        if result:
            results.append(result)
    return results
