from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("booking", "0037_mealplan_components_mealplan_package_pricing_mode_and_more"),
    ]

    operations = [
        migrations.AlterField(
            model_name="hotel",
            name="phone",
            field=models.TextField(blank=True),
        ),
    ]
