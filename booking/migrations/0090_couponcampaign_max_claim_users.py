from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("booking", "0089_bookingcoupon_deleted_at")]

    operations = [
        migrations.AddField(
            model_name="couponcampaign",
            name="max_claim_users",
            field=models.PositiveIntegerField(blank=True, null=True),
        ),
    ]
