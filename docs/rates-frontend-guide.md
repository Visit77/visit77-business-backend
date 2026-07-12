# Visit77 Booking Rates — Frontend Integration Guide

This guide covers room-price display and hotel-admin rate management in the Visit77 Booking Engine.

## Base URL and security

Local development:

```text
http://127.0.0.1:8080/api/v1
```

Public availability APIs require no authentication. Hotel-admin pricing APIs currently require:

```http
X-Booking-Admin-Key: <service-secret>
```

Never embed this key in a web bundle or mobile application. In production, the frontend must call an authenticated Visit77 admin API/BFF; that trusted server validates the Core user/business permission and injects the service key when calling Booking Engine. Direct use is acceptable only in local Postman development.

## Pricing model

Each sellable `RatePlan` has three possible price sources. The engine picks the first matching source in this order:

```text
DailyRate → RatePeriod → RatePlan.default_price
```

| Source | Use case | Example |
|---|---|---|
| `RatePlan.default_price` | Normal fallback price | MMK 80,000 |
| `RatePeriod` | Season or future effective price | Oct–Dec: MMK 120,000 |
| `DailyRate` | One exceptional stay date | Nov 15: MMK 150,000 |

Price is resolved by **stay date** and requested `guest_market`, not by booking-created date. Each RatePlan already identifies its market and currency, so RatePeriod and DailyRate each store one `price` in that RatePlan's currency. Nights satisfy `check_in <= stay_date < check_out`.

Example:

```text
Default price                         MMK 80,000
High-season period (Oct 1–Dec 31)    MMK 120,000
Festival DailyRate (Nov 15)           MMK 150,000

Sep 30 → 80,000
Oct 10 → 120,000
Nov 15 → 150,000
Jan 01 → 80,000
```

Confirmed bookings keep immutable `BookingRoomNight` snapshots. Editing a future price does not silently reprice an already confirmed booking.

## TypeScript contracts

Amounts are JSON decimal strings in admin model APIs. Public availability currently renders resolved amounts as JSON numbers. Treat both as decimal/money values and avoid binary floating-point arithmetic.

```ts
type GuestMarket = "all" | "local" | "foreign";

interface RatePlan {
  id: number;
  room_type: number;
  core_rate_plan_id: string;
  source: "core" | "booking";
  is_default: boolean;
  code: string;
  name: string;
  guest_market: GuestMarket;
  currency: "MMK" | "USD" | string;
  default_price: string;
  extra_bed_price: string;
  breakfast_included: boolean;
  refundable: boolean;
  cancellation_policy: Record<string, unknown>;
  is_active: boolean;
}

interface RatePeriod {
  id: number;
  rate_plan: number;
  name: string;
  start_date: string;       // YYYY-MM-DD, inclusive
  end_date: string | null;  // inclusive; null = no end date
  price: string;
  min_stay: number;
  closed_to_arrival: boolean;
  closed_to_departure: boolean;
  is_active: boolean;
  created_at: string;
  updated_at: string;
}

interface DailyRate {
  id: number;
  rate_plan: number;
  stay_date: string; // YYYY-MM-DD
  price: string;
  min_stay: number;
  closed_to_arrival: boolean;
  closed_to_departure: boolean;
}
```

## 1. Load Rate Plans

Core does not have a RatePlan model. During provisioning, Core derives a local and a foreign standard plan from each RoomType's price, currency and policy fields. Booking Engine stores those plans as `source: "core"`, sync-managed, read-only defaults. Hotel admins can add `source: "booking"` custom plans.

```http
GET /api/v1/admin/rate-plans/?room_type=BOOKING_ROOM_TYPE_ID
X-Booking-Admin-Key: ...
```

Example response:

```json
[
  {
    "id": 10,
    "room_type": 3,
    "core_rate_plan_id": "room-301-local",
    "source": "core",
    "is_default": true,
    "code": "local-standard",
    "name": "Local Standard Rate",
    "guest_market": "local",
    "currency": "MMK",
    "default_price": "80000.00",
    "extra_bed_price": "30000.00",
    "breakfast_included": true,
    "refundable": true,
    "cancellation_policy": {},
    "is_active": true
  }
]
```

Do not provide edit/delete controls for `source: "core"` plans. Their base values are regenerated from Core RoomType fields during synchronization. Provide CRUD controls for custom plans; DELETE soft-deactivates a custom plan.

### Create a custom plan

```http
POST /api/v1/admin/rate-plans/
X-Booking-Admin-Key: ...
Content-Type: application/json
```

```json
{
  "room_type": 3,
  "code": "local-no-refund",
  "name": "Local Saver - No Refund",
  "guest_market": "local",
  "currency": "MMK",
  "default_price": "70000.00",
  "extra_bed_price": "0.00",
  "breakfast_included": false,
  "refundable": false,
  "cancellation_policy": {"type": "non_refundable"},
  "is_active": true
}
```

`source`, `is_default`, and `core_rate_plan_id` are server-controlled. Custom plans may be updated with `PATCH /api/v1/admin/rate-plans/{id}/` and soft-deactivated with `DELETE` or `PATCH {"is_active": false}`.

## 2. Manage Effective Rate Periods

### List periods

```http
GET /api/v1/admin/rate-periods/?rate_plan=10
X-Booking-Admin-Key: ...
```

### Create a seasonal period

```http
POST /api/v1/admin/rate-periods/
X-Booking-Admin-Key: ...
Content-Type: application/json
```

```json
{
  "rate_plan": 10,
  "name": "2026 High Season",
  "start_date": "2026-10-01",
  "end_date": "2026-12-31",
  "price": "120000.00",
  "min_stay": 1,
  "closed_to_arrival": false,
  "closed_to_departure": false,
  "is_active": true
}
```

Both dates are inclusive.

### Create a permanent future price

Use `end_date: null`:

```json
{
  "rate_plan": 10,
  "name": "Price from October 2026",
  "start_date": "2026-10-01",
  "end_date": null,
  "price": "120000.00",
  "min_stay": 1,
  "closed_to_arrival": false,
  "closed_to_departure": false,
  "is_active": true
}
```

### Update or deactivate

```http
PATCH /api/v1/admin/rate-periods/{id}/
X-Booking-Admin-Key: ...
Content-Type: application/json
```

```json
{
  "price": "130000.00"
}
```

Deactivate without deleting history:

```json
{
  "is_active": false
}
```

Active periods belonging to the same rate plan cannot overlap. Before adding a new period after an open-ended period, first close the previous period.

```text
Existing: Oct 1, 2026 → open-ended
New:      Jan 1, 2027 → open-ended

Step 1: PATCH existing end_date to 2026-12-31
Step 2: POST new period starting 2027-01-01
```

## 3. Manage One-Day Overrides

Use DailyRate for a festival, event, or other exceptional stay date.

```http
POST /api/v1/admin/rates/
X-Booking-Admin-Key: ...
Content-Type: application/json
```

```json
{
  "rate_plan": 10,
  "stay_date": "2026-11-15",
  "price": "150000.00",
  "min_stay": 1,
  "closed_to_arrival": false,
  "closed_to_departure": false
}
```

List and update:

```http
GET   /api/v1/admin/rates/?rate_plan=10&stay_date=2026-11-15
PATCH /api/v1/admin/rates/{id}/
```

Only one DailyRate can exist for `(rate_plan, stay_date)`.

## 4. Bulk Daily Overrides

Use this when many individual dates need the same exceptional value. It creates/updates DailyRate rows; it does not create a RatePeriod.

```http
POST /api/v1/admin/rates/bulk-upsert/
X-Booking-Admin-Key: ...
Content-Type: application/json
```

```json
{
  "rate_plan_id": 10,
  "start_date": "2026-04-13",
  "end_date": "2026-04-17",
  "price": "150000.00",
  "min_stay": 3,
  "closed_to_arrival": false,
  "closed_to_departure": false
}
```

Response:

```json
{
  "success": true,
  "data": {
    "rate_plan_id": 10,
    "start_date": "2026-04-13",
    "end_date": "2026-04-17",
    "created": 5,
    "updated": 0,
    "rates": []
  }
}
```

The maximum range per request is 731 inclusive days.

## 5. Display User Availability and Prices

```http
GET /api/v1/public/search/availability/
GET /api/v1/public/hotels/{core_business_id}/availability/
    ?check_in=2026-11-14
    &check_out=2026-11-17
    &adults=2
    &children=0
    &guest_market=local
```

Relevant response shape:

```json
{
  "success": true,
  "data": {
    "room_types": [
      {
        "core_room_type_id": 301,
        "name": "Deluxe Room",
        "available_rooms": 3,
        "rate_plans": [
          {
            "id": 10,
            "name": "Local Standard Rate",
            "currency": "MMK",
            "nightly_prices": [
              {"date": "2026-11-14", "price": 120000.0},
              {"date": "2026-11-15", "price": 150000.0},
              {"date": "2026-11-16", "price": 120000.0}
            ],
            "total": 390000.0,
            "breakfast_included": true,
            "refundable": true,
            "cancellation_policy": {}
          }
        ]
      }
    ]
  }
}
```

The frontend must display server-returned `nightly_prices` and `total`; do not reproduce pricing precedence in JavaScript. Availability is a quote, not an inventory lock. The booking-create endpoint validates and prices the stay again under a database transaction.

## Restrictions

- `min_stay`: selected stay must contain at least this many nights.
- `closed_to_arrival`: check-in is not allowed under the effective rule.
- `closed_to_departure`: check-out is not allowed under the effective rule. The current engine evaluates this flag on the final charged stay date.
- Inventory `stop_sell` is managed separately through DailyInventory; it is not a price field.

## Error handling

Validation error example:

```json
{
  "success": false,
  "error": [
    "An active rate period overlaps this date range."
  ]
}
```

Recommended UI behavior:

- HTTP `400`: show field/overlap validation and keep form values.
- HTTP `403`: session/business permission or gateway configuration problem.
- HTTP `404`: selected rate plan/period no longer exists; refresh the page.
- HTTP `5xx`: show retry state; do not assume the write failed without refetching.

## Suggested admin UI

For each room type and guest market:

1. Show RatePlan default price as read-only.
2. Show effective periods on a date-range timeline.
3. Provide “Add season/future price” using RatePeriod.
4. Provide calendar-cell editing using DailyRate.
5. Mark DailyRate cells as overrides so users understand why they differ from the season.
6. Preview nightly breakdown through the public availability endpoint before publishing operational changes.
