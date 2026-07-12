from django.core.management.base import BaseCommand

from booking.integrations.core import sync_business_from_core


class Command(BaseCommand):
    help = "Synchronize one Visit77 Core business and its room catalog."

    def add_arguments(self, parser):
        parser.add_argument("core_business_id", type=int)

    def handle(self, *args, **options):
        result = sync_business_from_core(options["core_business_id"])
        self.stdout.write(self.style.SUCCESS(str(result)))
