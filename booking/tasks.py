from celery import shared_task

from booking.services import ensure_rolling_daily_inventory, expire_pending_bookings


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
