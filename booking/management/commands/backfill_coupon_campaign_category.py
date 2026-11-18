from django.core.management.base import BaseCommand, CommandError

from booking.models import CouponCampaign


class Command(BaseCommand):
    help = "Fill a missing category on booking coupon campaigns. Defaults to a dry run."

    def add_arguments(self, parser):
        parser.add_argument("--category-id", type=int, default=4, help="Core Hotel category ID (default: 4).")
        parser.add_argument("--apply", action="store_true", help="Save the changes.")

    def handle(self, *args, **options):
        category_id = options["category_id"]
        if not 1 <= category_id <= 2**63 - 1:
            raise CommandError("--category-id must be a positive Core category ID.")

        campaigns = CouponCampaign.objects.filter(type="booking", category_id__isnull=True)
        if options["apply"]:
            count = campaigns.update(category_id=category_id)
            self.stdout.write(self.style.SUCCESS(f"Updated {count} booking campaign(s) to category {category_id}."))
        else:
            self.stdout.write(f"Would update {campaigns.count()} booking campaign(s) to category {category_id}.")
