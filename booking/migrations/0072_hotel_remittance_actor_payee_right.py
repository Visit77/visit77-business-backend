from django.db import migrations


def replace_hotel_checker_with_payee(apps, schema_editor):
    Actor = apps.get_model("booking", "OTARemittanceActor")
    for actor in Actor.objects.filter(scope="hotel").iterator():
        rights = list(actor.access_rights or [])
        updated = ["payee" if right == "checker" else right for right in rights]
        updated = list(dict.fromkeys(updated))
        if updated != rights:
            actor.access_rights = updated
            actor.save(update_fields=["access_rights"])


def replace_hotel_payee_with_checker(apps, schema_editor):
    Actor = apps.get_model("booking", "OTARemittanceActor")
    for actor in Actor.objects.filter(scope="hotel").iterator():
        rights = list(actor.access_rights or [])
        updated = ["checker" if right == "payee" else right for right in rights]
        updated = list(dict.fromkeys(updated))
        if updated != rights:
            actor.access_rights = updated
            actor.save(update_fields=["access_rights"])


class Migration(migrations.Migration):
    dependencies = [
        ("booking", "0071_remittance_service_charge_amounts"),
    ]

    operations = [
        migrations.RunPython(
            replace_hotel_checker_with_payee,
            replace_hotel_payee_with_checker,
        ),
    ]
