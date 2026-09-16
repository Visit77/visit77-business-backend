from decimal import Decimal

from django.test import SimpleTestCase

from config.response_formatter import normalize_monetary_response, success


class MonetaryResponseNormalizationTests(SimpleTestCase):
    def test_normalizes_nested_monetary_values_to_float(self):
        payload = {
            "rate_plan": {
                "id": 10,
                "base_price": 80000,
                "usd_display_price": "40.00",
                "extra_bed_price": Decimal("30000.00"),
            },
            "nightly_prices": [80000, "90000.50"],
            "grand_total": Decimal("170000.50"),
            "amount_due": "70000.50",
            "deposit": "50000.00",
            "formatted_total": "MMK 170,000.50",
            "quantity": 2,
            "refund_percent": 50,
        }

        normalized = normalize_monetary_response(payload)

        self.assertEqual(normalized["rate_plan"]["base_price"], 80000.0)
        self.assertEqual(normalized["rate_plan"]["usd_display_price"], 40.0)
        self.assertEqual(normalized["rate_plan"]["extra_bed_price"], 30000.0)
        self.assertEqual(normalized["nightly_prices"], [80000.0, 90000.5])
        self.assertEqual(normalized["grand_total"], 170000.5)
        self.assertEqual(normalized["amount_due"], 70000.5)
        self.assertEqual(normalized["deposit"], 50000.0)
        self.assertEqual(normalized["formatted_total"], "MMK 170,000.50")
        self.assertEqual(normalized["quantity"], 2)
        self.assertEqual(normalized["refund_percent"], 50)

    def test_success_exposes_float_monetary_values(self):
        response = success({
            "price": "100.00",
            "invoice": {"subtotal": Decimal("80.00"), "tax_total": 20},
        })

        self.assertEqual(response.data["data"]["price"], 100.0)
        self.assertEqual(response.data["data"]["invoice"]["subtotal"], 80.0)
        self.assertEqual(response.data["data"]["invoice"]["tax_total"], 20.0)

    def test_total_is_float_only_in_monetary_context(self):
        normalized = normalize_monetary_response({
            "pagination": {"total": 42, "limit": 20},
            "invoice": {"total": "100.00", "currency": "MMK"},
        })

        self.assertEqual(normalized["pagination"]["total"], 42)
        self.assertIsInstance(normalized["pagination"]["total"], int)
        self.assertEqual(normalized["invoice"]["total"], 100.0)
