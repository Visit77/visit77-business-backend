from celery import shared_task

from booking.services import expire_pending_bookings


@shared_task
def expire_booking_holds_task():
    count = expire_pending_bookings()
    return f"Expired {count} booking hold(s)."
