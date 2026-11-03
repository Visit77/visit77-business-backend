# Remittance commission: tax-only exclusion

New calculation:

```text
commission_base = max(booking_collection + released_held_funds - tax, 0)
commission = round_half_up(commission_base * commission_rate / 100, 2)
net_payout = booking_collection + released_held_funds - commission - refund + adjustment
```

Service charges remain in the commission base. Tax and service charges remain in
the hotel's payout; neither is an additional payout deduction.

Examples at 5%:

| Collection | Released funds | Tax | Service charge | Commission | Payout (no refund/adjustment) |
|---:|---:|---:|---:|---:|---:|
| 256 | 0 | 6 | 50 | 12.50 | 243.50 |
| 1230 | 500 | 30 | 100 | 85.00 | 1645.00 |

Apply migration `0077_commission_includes_service_charge` with the deployment.
It recalculates pending/accruing records with no approval/payment/receipt markers,
retains adjustments, and records an audit entry. Checked, paid, received and other
historical records retain their stored policy and amounts. The migration is not
reversible because silently reversing financial adjustments would be unsafe.

API responses expose `service_charge_amount` (actual service charges),
`commission_excludes_service_charge` (stored statement policy), and the existing
`service_charge_excluded_from_commission` field (zero for the new policy; actual
service charge for legacy statements). PDF labels reflect the stored policy.

## Recalculate all unpaid statements

From the Booking Engine directory, apply migrations first, then preview:

```bash
python manage.py migrate
python manage.py recalculate_unpaid_remittances --dry-run
```

Apply the new tax-only commission formula to all unpaid statements:

```bash
python manage.py recalculate_unpaid_remittances
```

Optional scopes (can be combined):

```bash
python manage.py recalculate_unpaid_remittances --business-id 9424 --dry-run
python manage.py recalculate_unpaid_remittances --remittance-id 14
python manage.py recalculate_unpaid_remittances --remittance-id 14 --remittance-id 15
```

The command recalculates stored statement totals using the statement's existing
commission rate. It preserves collections, released funds, tax, service-charge
amount, refunds, adjustments, period, currency, reference and ledger items.
Pending, accruing, failed and checked statements are eligible only if no payment
or receipt markers exist. Paid/received statuses and payment/receipt markers are
always excluded, even when a status has been reset. Each statement is locked and
rechecked immediately before writing to avoid modifying a concurrent payment.

Unlike the automatic migration, this explicitly requested command also processes
checked-but-unpaid statements. If amounts/policy change on a checked statement,
it clears the checker/checked timestamp and returns the statement to pending for
recheck. Every changed statement gets a `commission_recalculated` audit entry
with old/new amounts and approval state. Repeated runs do not create duplicate
audit entries for unchanged statements. Dry-run writes neither changes nor audit
logs and prints the same proposed amounts/statuses. Skipped, unchanged and
updated counts are reported at the end.
