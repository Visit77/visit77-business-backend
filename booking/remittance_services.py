from datetime import datetime, time, timedelta
from decimal import Decimal, ROUND_HALF_UP
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from django.conf import settings
from django.db import transaction
from django.db.models import Exists, OuterRef, Q, Sum
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from booking.money_display import round_mmk
from booking.models import (
    Booking,
    Hotel,
    Invoice,
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


def _coupon_payment_share(payment, amount):
    """Allocate a funded invoice amount by cumulative guest collections."""
    invoice = payment.invoice
    if invoice.total == 0:
        return amount if payment.provider == Payment.Provider.COUPON else Decimal("0")
    previous = invoice.receipts.filter(
        status__in=[Payment.Status.PAID, Payment.Status.PARTIALLY_REFUNDED, Payment.Status.REFUNDED],
        paid_at__isnull=False,
    ).filter(
        Q(created_at__lt=payment.created_at)
        | Q(created_at=payment.created_at, pk__lt=payment.pk)
    ).aggregate(total=Sum("amount", default=Decimal("0")))["total"]
    before = min(previous, invoice.total)
    after = min(previous + payment.amount, invoice.total)
    return _money(amount * after / invoice.total) - _money(amount * before / invoice.total)


def payment_coupon_subsidy(payment):
    if not payment.invoice_id or not payment.invoice.coupon_discount_amount:
        return Decimal("0")
    return _coupon_payment_share(payment, payment.invoice.coupon_discount_amount)


def coupon_subsidy_reversal(payment, subsidy):
    if payment.amount:
        return _money(subsidy * payment.refunded_amount / payment.amount)
    if payment.provider == Payment.Provider.COUPON:
        return min(subsidy, Decimal(str((payment.metadata or {}).get("coupon_only_reversal_amount", 0))))
    return Decimal("0")


def payment_tax(payment):
    booking = payment.booking
    if payment.invoice_id and payment.invoice.coupon_discount_amount:
        return _coupon_payment_share(payment, payment.invoice.tax_total)
    if booking.grand_total <= 0:
        return Decimal("0")
    return _money(booking.tax_total * payment.amount / booking.grand_total)


def payment_service_charge(payment):
    booking = payment.booking
    if payment.invoice_id and payment.invoice.coupon_discount_amount:
        amount = sum((Decimal(charge["amount"]) for charge in
                      (payment.invoice.charge_snapshot or {}).get("service_charges", [])), Decimal("0"))
        return _coupon_payment_share(payment, amount)
    if booking.grand_total <= 0:
        return Decimal("0")
    return _money(booking.service_charge_total * payment.amount / booking.grand_total)


def _refund_time(payment):
    raw = (payment.metadata or {}).get("refunded_at")
    parsed = parse_datetime(str(raw)) if raw else None
    return parsed or payment.paid_at or payment.created_at


def is_ota_revenue_payment(payment):
    """Return whether a payment belongs to the original OTA booking sale.

    PMS invoices created later for meals, extensions, damage, and other hotel
    charges remain PMS revenue even when their parent booking originated on
    OTA. They must never enter Visit77 commission or remittance calculations.
    """
    invoice = payment.invoice
    return bool(
        invoice
        and invoice.charge_scope == Invoice.ChargeScope.OTA
        and invoice.invoice_type == Invoice.Type.ROOM_BOOKING
    )


PAYABLE_LEDGER_TYPES = (
    OTARevenueLedger.EventType.BOOKING_COLLECTION,
    OTARevenueLedger.EventType.HELD_RELEASE,
)


def unreleased_held_entries():
    payable = OTARevenueLedger.objects.filter(
        payment_id=OuterRef("payment_id"), event_type__in=PAYABLE_LEDGER_TYPES,
    )
    return OTARevenueLedger.objects.filter(
        event_type=OTARevenueLedger.EventType.HELD,
    ).filter(~Exists(payable))


@transaction.atomic
def sync_payment_ledger(payment, *, release_held=False):
    booking = Booking.objects.select_for_update(of=("self",)).select_related("hotel").get(pk=payment.booking_id)
    payment = Payment.objects.select_related("invoice").get(pk=payment.pk)
    if booking.source != Booking.Source.OTA or not payment.paid_at:
        return []
    if not is_ota_revenue_payment(payment):
        return []
    if payment.status not in {Payment.Status.PAID, Payment.Status.PARTIALLY_REFUNDED, Payment.Status.REFUNDED}:
        return []

    # Keep the original settlement path when the flag or refund policy changes.
    existing = {
        row.event_type: row for row in OTARevenueLedger.objects.filter(
            payment=payment, event_type__in=(*PAYABLE_LEDGER_TYPES, OTARevenueLedger.EventType.HELD),
        )
    }
    held_entry = existing.get(OTARevenueLedger.EventType.HELD)
    payable_entry = next((existing[kind] for kind in PAYABLE_LEDGER_TYPES if kind in existing), None)
    new_entry = False
    held_enabled = settings.HELD_FUND_CALCULATION
    if not payable_entry and not held_entry:
        subsidy = payment_coupon_subsidy(payment)
        amounts = {
            "gross_amount": payment.amount + subsidy,
            "tax_amount": payment_tax(payment),
            "service_charge_amount": payment_service_charge(payment),
        }
        held = (
            held_enabled and not is_non_refundable(booking)
            and payment.paid_at.astimezone(hotel_timezone(booking.hotel)).date() < booking.check_in
        )
        kind = OTARevenueLedger.EventType.HELD if held else OTARevenueLedger.EventType.BOOKING_COLLECTION
        entry = OTARevenueLedger.objects.create(
            event_key=f"payment:{payment.id}:{kind}",
            hotel=booking.hotel, booking=booking, payment=payment,
            event_type=kind, effective_at=payment.paid_at,
            metadata={"coupon_subsidy_amount": str(subsidy)} if subsidy else {}, **amounts,
        )
        new_entry = True
        if held:
            held_entry = entry
        else:
            payable_entry = entry

    if held_entry and not payable_entry:
        now = timezone.now()
        scheduled_release = datetime.combine(
            booking.check_in, time.min, tzinfo=hotel_timezone(booking.hotel),
        )
        if release_held or not held_enabled or now >= scheduled_release:
            new_entry = True
            early = release_held or not held_enabled
            reason = "manual" if release_held else "held_fund_disabled"
            payable_entry = OTARevenueLedger.objects.create(
                event_key=f"payment:{payment.id}:held_release",
                hotel=booking.hotel, booking=booking, payment=payment,
                event_type=OTARevenueLedger.EventType.HELD_RELEASE,
                effective_at=now if early else scheduled_release,
                metadata={**held_entry.metadata, **({"release_reason": reason} if early else {})},
                gross_amount=held_entry.gross_amount,
                tax_amount=held_entry.tax_amount,
                service_charge_amount=held_entry.service_charge_amount,
            )

    entries = [entry for entry in (held_entry, payable_entry) if entry]
    if new_entry or booking.settlement_status in {
        Booking.SettlementStatus.UNPAID, Booking.SettlementStatus.HELD, Booking.SettlementStatus.AVAILABLE,
    }:
        settlement_status = (
            Booking.SettlementStatus.HELD
            if unreleased_held_entries().filter(booking=booking).exists()
            else Booking.SettlementStatus.AVAILABLE
        )
        if booking.settlement_status != settlement_status:
            booking.settlement_status = settlement_status
            booking.save(update_fields=["settlement_status", "updated_at"])

    if payment.invoice.coupon_discount_amount:
        source = payable_entry or held_entry
        subsidy = Decimal(str(source.metadata.get("coupon_subsidy_amount", 0)))
        reversal = coupon_subsidy_reversal(payment, subsidy)
        total_refund = payment.refunded_amount + reversal
        prior_refunds = OTARevenueLedger.objects.filter(payment=payment, event_type=OTARevenueLedger.EventType.REFUND)
        already_recorded = prior_refunds.aggregate(total=Sum("refund_amount", default=Decimal("0")))["total"]
        if total_refund > already_recorded:
            prior_reversals = sum((Decimal(row.get("coupon_subsidy_reversal_amount", "0"))
                                   for row in prior_refunds.values_list("metadata", flat=True)), Decimal("0"))
            entries.append(OTARevenueLedger.objects.create(
                event_key=f"payment:{payment.id}:coupon_refund:{total_refund}",
                hotel=booking.hotel, booking=booking, payment=payment,
                event_type=OTARevenueLedger.EventType.REFUND,
                refund_amount=total_refund - already_recorded,
                effective_at=_refund_time(payment),
                metadata={"coupon_subsidy_reversal_amount": str(reversal - prior_reversals)},
            ))
    elif payment.refunded_amount > 0:
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
    return entries


def sync_all_payment_ledgers(hotel=None):
    qs = Payment.objects.filter(
        booking__source=Booking.Source.OTA,
        invoice__charge_scope=Invoice.ChargeScope.OTA,
        invoice__invoice_type=Invoice.Type.ROOM_BOOKING,
        status__in=[Payment.Status.PAID, Payment.Status.PARTIALLY_REFUNDED, Payment.Status.REFUNDED],
        paid_at__isnull=False,
    ).select_related("booking__hotel", "invoice")
    if hotel:
        qs = qs.filter(booking__hotel=hotel)
    count = 0
    for payment in qs.iterator():
        sync_payment_ledger(payment)
        count += 1
    return count


@transaction.atomic
def cleanup_non_ota_revenue_ledgers(hotel=None):
    """Remove PMS-side payments accidentally copied into OTA settlement."""
    eligible_payment_ids = Payment.objects.filter(
        invoice__charge_scope=Invoice.ChargeScope.OTA,
        invoice__invoice_type=Invoice.Type.ROOM_BOOKING,
    ).values("id")
    invalid = OTARevenueLedger.objects.exclude(
        payment_id__in=eligible_payment_ids,
    )
    if hotel:
        invalid = invalid.filter(hotel=hotel)
    invalid_ids = list(invalid.values_list("id", flat=True))
    affected_remittance_ids = list(
        OTARemittanceItem.objects.filter(ledger_entry_id__in=invalid_ids)
        .values_list("remittance_id", flat=True)
        .distinct()
    )
    removed_items, _ = OTARemittanceItem.objects.filter(
        ledger_entry_id__in=invalid_ids,
    ).delete()
    removed_ledgers, _ = OTARevenueLedger.objects.filter(id__in=invalid_ids).delete()

    updated_remittances = 0
    deleted_remittances = 0
    for remittance in OTARemittance.objects.select_for_update().select_related("hotel").filter(
        id__in=affected_remittance_ids,
    ):
        items = list(remittance.items.all())
        if not items:
            remittance.delete()
            deleted_remittances += 1
            continue
        collections = sum(
            (item.gross_amount for item in items if item.item_type == OTARevenueLedger.EventType.BOOKING_COLLECTION),
            Decimal("0"),
        )
        releases = sum(
            (item.gross_amount for item in items if item.item_type == OTARevenueLedger.EventType.HELD_RELEASE),
            Decimal("0"),
        )
        commission_types = {
            OTARevenueLedger.EventType.BOOKING_COLLECTION,
            OTARevenueLedger.EventType.HELD_RELEASE,
        }
        tax = sum(
            (item.tax_amount for item in items if item.item_type in commission_types),
            Decimal("0"),
        )
        service_charge = sum(
            (item.service_charge_amount for item in items if item.item_type in commission_types),
            Decimal("0"),
        )
        refunds = sum(
            (item.refund_amount for item in items if item.item_type == OTARevenueLedger.EventType.REFUND),
            Decimal("0"),
        )
        rate = commission_rate_for(remittance.hotel)
        preserve_policy = remittance.status in {OTARemittance.Status.CHECKED, OTARemittance.Status.PAID, OTARemittance.Status.RECEIVED} or any((
            remittance.checked_at, remittance.paid_at, remittance.received_at,
            remittance.checked_by_id, remittance.paid_by_id, remittance.received_by_id,
        ))
        exclude_service_charge = remittance.commission_excludes_service_charge if preserve_policy else False
        if preserve_policy:
            rate = remittance.commission_rate
        commissionable = max(collections + releases - tax - (service_charge if exclude_service_charge else 0), Decimal("0"))
        commission = _money(commissionable * rate / Decimal("100"))
        base_net = _money(collections + releases - commission - refunds)
        remittance.gross_booking_amount = _money(collections)
        remittance.released_held_funds_amount = _money(releases)
        remittance.tax_amount = _money(tax)
        remittance.service_charge_amount = _money(service_charge)
        remittance.refund_amount = _money(refunds)
        remittance.commission_excludes_service_charge = exclude_service_charge
        remittance.commission_rate = rate
        remittance.commission_amount = commission
        remittance.net_remittance_amount = _money(
            round_mmk(base_net + remittance.adjustment_amount, None if preserve_policy else remittance.currency)
        )
        remittance.save(update_fields=[
            "gross_booking_amount", "released_held_funds_amount", "tax_amount",
            "service_charge_amount", "refund_amount", "commission_excludes_service_charge", "commission_rate",
            "commission_amount", "net_remittance_amount", "updated_at",
        ])
        updated_remittances += 1
    return {
        "removed_ledgers": removed_ledgers,
        "removed_items": removed_items,
        "updated_remittances": updated_remittances,
        "deleted_remittances": deleted_remittances,
    }


def _remittance_reference(remittance):
    """Build a globally increasing, human-readable remittance reference."""
    return f"V77-PAY-{remittance.period_to:%y}-{remittance.id:06d}"


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
        and (
            remittance.status in {OTARemittance.Status.CHECKED, OTARemittance.Status.PAID, OTARemittance.Status.RECEIVED}
            or any((remittance.checked_at, remittance.paid_at, remittance.received_at,
                    remittance.checked_by_id, remittance.paid_by_id, remittance.received_by_id))
        )
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
    commissionable = max(collections + releases - tax, Decimal("0"))
    commission = _money(commissionable * rate / Decimal("100"))
    exact_net = collections + releases - commission - refunds
    net = _money(exact_net if historical else round_mmk(exact_net, hotel.base_currency))
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
            "commission_excludes_service_charge": False,
            "commission_rate": rate,
            "commission_amount": commission,
            "net_remittance_amount": net,
            "status": status,
            # The final reference needs the database-generated global ID and is
            # assigned immediately after creation below.
            "reference": "",
            "historical_backfill": historical,
            "checked_at": timezone.now() if completed else None,
            "paid_at": timezone.now() if completed else None,
            "received_at": timezone.now() if completed else None,
        },
    )
    if created:
        remittance.reference = _remittance_reference(remittance)
        remittance.save(update_fields=["reference", "updated_at"])
    else:
        remittance.gross_booking_amount = _money(collections)
        remittance.released_held_funds_amount = _money(releases)
        remittance.tax_amount = _money(tax)
        remittance.service_charge_amount = _money(service_charge)
        remittance.refund_amount = _money(refunds)
        remittance.commission_excludes_service_charge = False
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


def payout_periods_for_week(hotel, monday):
    option = hotel.remittance_payout_option_for(monday)
    if option == Hotel.RemittancePayoutOption.THREE_TIMES_WEEKLY:
        return [(monday, monday + timedelta(days=1)),
                (monday + timedelta(days=2), monday + timedelta(days=3)),
                (monday + timedelta(days=4), monday + timedelta(days=6))]
    if option == Hotel.RemittancePayoutOption.TWICE_WEEKLY:
        return [(monday, monday + timedelta(days=2)),
                (monday + timedelta(days=3), monday + timedelta(days=6))]
    return [(monday, monday + timedelta(days=6))]


def active_payout_period(hotel, today=None):
    local_today = today or timezone.now().astimezone(hotel_timezone(hotel)).date()
    monday, _ = current_week_period(local_today, hotel=hotel)
    return next(period for period in payout_periods_for_week(hotel, monday)
                if period[0] <= local_today <= period[1])


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
    if hotel.remittance_payout_option_for(local_today) == Hotel.RemittancePayoutOption.THREE_TIMES_WEEKLY:
        for period_from, period_to in payout_periods_for_week(hotel, week_from):
            if period_to < local_today:
                generate_weekly_remittance(hotel, period_from, period_to)
    if (
        hotel.remittance_payout_option_for(local_today) == Hotel.RemittancePayoutOption.TWICE_WEEKLY
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
        local_today = today or timezone.now().astimezone(hotel_timezone(hotel)).date()
        current_monday, _ = current_week_period(local_today)
        cutoff = current_monday
        if hotel.remittance_payout_option_for(local_today) == Hotel.RemittancePayoutOption.THREE_TIMES_WEEKLY:
            cutoff, _ = active_payout_period(hotel, local_today)
        start, end = active_payout_period(hotel, cutoff - timedelta(days=1))
        result = generate_weekly_remittance(hotel, start, end)
        if result:
            results.append(result)
    return results


def unpaid_remittance_filter():
    """Exclude payments even if their workflow status was subsequently changed."""
    return (
        ~Q(status__in=[OTARemittance.Status.PAID, OTARemittance.Status.RECEIVED])
        & Q(paid_at__isnull=True, paid_by_id__isnull=True,
            received_at__isnull=True, received_by_id__isnull=True)
    )


@transaction.atomic
def recalculate_unpaid_remittance(remittance_id, *, dry_run=False):
    """Reprice the stored statement under tax-only commission without rebuilding its ledger."""
    record = OTARemittance.objects.select_for_update().get(pk=remittance_id)
    result = {
        "id": record.id, "reference": record.reference or str(record.id),
        "currency": record.currency, "previous_commission": record.commission_amount,
        "previous_net_payout": record.net_remittance_amount,
        "previous_status": record.status,
    }
    if (
        record.status in {OTARemittance.Status.PAID, OTARemittance.Status.RECEIVED}
        or any((record.paid_at, record.paid_by_id, record.received_at, record.received_by_id))
    ):
        return {**result, "outcome": "skipped_paid"}
    gross = record.gross_booking_amount + record.released_held_funds_amount
    base = max(gross - record.tax_amount, Decimal("0"))
    commission = _money(base * record.commission_rate / Decimal("100"))
    payout = _money(round_mmk(gross - commission - record.refund_amount + record.adjustment_amount, record.currency))
    changed = (
        record.commission_excludes_service_charge
        or commission != record.commission_amount
        or payout != record.net_remittance_amount
    )
    was_checked = bool(record.status == OTARemittance.Status.CHECKED or record.checked_at or record.checked_by_id)
    status = OTARemittance.Status.PENDING if changed and was_checked else record.status
    result.update(commission=commission, net_payout=payout, status=status,
                  commission_base=base, commission_rate=record.commission_rate,
                  outcome="changed" if changed else "unchanged")
    if not changed or dry_run:
        return result
    previous_checker_id = record.checked_by_id
    record.commission_amount = commission
    record.net_remittance_amount = payout
    record.commission_excludes_service_charge = False
    fields = ["commission_amount", "net_remittance_amount", "commission_excludes_service_charge", "updated_at"]
    if was_checked:
        record.status = status
        record.checked_at = None
        record.checked_by = None
        fields.extend(["status", "checked_at", "checked_by"])
    record.save(update_fields=fields)
    OTARemittanceAuditLog.objects.create(
        remittance=record, action="commission_recalculated", actor_name="System",
        metadata={
            "command": "recalculate_unpaid_remittances",
            "previous_commission": str(result["previous_commission"]), "commission": str(commission),
            "previous_net_payout": str(result["previous_net_payout"]), "net_payout": str(payout),
            "previous_status": result["previous_status"], "status": status,
            "previous_checker_id": previous_checker_id,
            "commission_base": str(base), "commission_rate": str(record.commission_rate),
            "service_charge_included_in_commission": True,
        },
    )
    return result
