from django.db import migrations


class Migration(migrations.Migration):
    # Production already introduces the commission field and policy in 0076.
    # Join the room-pricing branch here so downstream migration names stay stable.
    dependencies = [
        ("booking", "0076_commission_includes_service_charge"),
        ("booking", "0076_room_price_setup_and_discounts"),
    ]

    operations = []
