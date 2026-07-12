from django.core.management.base import BaseCommand

from booking.services import expire_pending_bookings


class Command(BaseCommand):
    help = "Expire unpaid booking holds and return inventory."

    def handle(self, *args, **options):
        count = expire_pending_bookings()
        self.stdout.write(self.style.SUCCESS(f"Expired {count} booking hold(s)."))
