from celery import shared_task

from booking.services import (
    auto_cancel_no_show_reservations,
    ensure_rolling_daily_inventory,
    expire_pending_bookings,
)


@shared_task
def expire_booking_holds_task():
    count = expire_pending_bookings()
    return f"Expired {count} booking hold(s)."


@shared_task
def ensure_rolling_daily_inventory_task():
    summary = ensure_rolling_daily_inventory()
    return (
        f"Ensured inventory for {summary['room_types']} room type(s): "
        f"{summary['created']} created, {summary['updated']} updated."
    )


@shared_task
def auto_cancel_no_show_reservations_task():
    count = auto_cancel_no_show_reservations()
    return f"Auto-canceled {count} no-show reservation(s)."
