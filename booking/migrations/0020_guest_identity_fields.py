from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("booking", "0019_payment_provider_aya"),
    ]

    operations = [
        migrations.AddField(
            model_name="guest",
            name="identity_type",
            field=models.CharField(blank=True, max_length=50),
        ),
        migrations.AddField(
            model_name="guest",
            name="identity_number",
            field=models.CharField(blank=True, max_length=100),
        ),
    ]
