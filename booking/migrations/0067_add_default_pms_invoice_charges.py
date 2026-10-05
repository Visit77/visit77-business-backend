from decimal import Decimal

from django.db import migrations, models

import booking.models


DEFAULT_TAX = {
    "title": "Tax",
    "mode": "included",
    "value": "0",
    "calculation_basis": "per_booking_per_night",
    "charge_kind": "tax",
}

DEFAULT_SERVICE_CHARGE = {
    "title": "Service Charge",
    "mode": "percentage",
    "value": "0",
    "calculation_basis": "per_booking_per_night",
    "charge_kind": "service_charge",
}


def add_default_pms_invoice_charges(apps, schema_editor):
    Hotel = apps.get_model("booking", "Hotel")
    PMSInvoiceCharge = apps.get_model("booking", "PMSInvoiceCharge")

    for hotel in Hotel.objects.all().iterator():
        config = dict(hotel.pms_invoice_charges or {})
        taxes = list(config.get("taxes") or [])
        service_charges = list(config.get("service_charges") or [])

        if not any(
            str(rule.get("title") or "").strip().casefold() == "tax"
            for rule in taxes
        ):
            taxes.append(dict(DEFAULT_TAX))

        if not any(
            str(rule.get("title") or "").strip().casefold() == "service charge"
            for rule in service_charges
        ):
            service_charges.append(dict(DEFAULT_SERVICE_CHARGE))

        normalized = {
            "taxes": taxes,
            "service_charges": service_charges,
        }
        if normalized != hotel.pms_invoice_charges:
            hotel.pms_invoice_charges = normalized
            hotel.save(update_fields=["pms_invoice_charges"])

        if not PMSInvoiceCharge.objects.filter(
            hotel_id=hotel.id,
            title__iexact="Tax",
        ).exists():
            PMSInvoiceCharge.objects.create(
                hotel_id=hotel.id,
                charge_type="tax",
                charge_kind="tax",
                title="Tax",
                mode="included",
                value=Decimal("0"),
                calculation_basis="per_booking_per_night",
                sort_order=0,
            )

        if not PMSInvoiceCharge.objects.filter(
            hotel_id=hotel.id,
            title__iexact="Service Charge",
        ).exists():
            PMSInvoiceCharge.objects.create(
                hotel_id=hotel.id,
                charge_type="service",
                charge_kind="service_charge",
                title="Service Charge",
                mode="percentage",
                value=Decimal("0"),
                calculation_basis="per_booking_per_night",
                sort_order=0,
            )


class Migration(migrations.Migration):

    dependencies = [
        ("booking", "0066_booking_inventory_mode"),
    ]

    operations = [
        migrations.AlterField(
            model_name="hotel",
            name="pms_invoice_charges",
            field=models.JSONField(
                blank=True,
                default=booking.models.default_invoice_charges,
            ),
        ),
        migrations.RunPython(
            add_default_pms_invoice_charges,
            migrations.RunPython.noop,
        ),
    ]
