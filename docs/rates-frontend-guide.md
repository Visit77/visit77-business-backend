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

## Separate room price setup and campaigns

Core Room Type creation accepts room metadata without the four normal price
fields. Configure prices in Booking Engine after creation:

```text
GET/PATCH/PUT /api/v1/admin/room-types/{id}/room-price/
GET/POST      /api/v1/admin/room-types/{id}/discounts/
GET/PATCH/PUT /api/v1/admin/room-types/{id}/discounts/{discount_id}/
POST          /api/v1/admin/room-types/{id}/discounts/{discount_id}/end/
```

Use the existing admin authentication and `X-Booking-Business-Id` business scope.
The `{id}` is the Booking Engine RoomType ID. A price setup request accepts
`local_mmk_price`, `local_usd_price`, `foreign_mmk_price`, and
`foreign_usd_price`. At least one positive price must remain configured; null
clears a currency. Omitted fields remain unchanged. Setup returns
`room_price_setup: true` and synchronizes the standard market plans while
preserving custom plans. Core catalog synchronization preserves these prices.
The hotel base currency remains the invoice currency. A standard plan becomes
sellable once its base-currency amount is configured; a USD display-only value
does not publish a zero-price MMK booking.

Room types with `room_price_setup: false` are excluded from availability and
rejected by estimate/booking creation. Migration 0076 imports legacy active
market-plan prices, so already-priced rooms retain their setup state.

Create a campaign by supplying the **payable room price**, not the amount saved:

```json
{
  "timing_mode": "manual",
  "local_mmk_price": "80000.00"
}
```

`manual` starts immediately on the current date and continues until the end
endpoint is called. `scheduled` requires `start_date` and `end_date` (inclusive):

```json
{
  "timing_mode": "scheduled",
  "start_date": "2026-12-01",
  "end_date": "2026-12-31",
  "local_mmk_price": "80000.00",
  "local_usd_price": "20.00",
  "foreign_mmk_price": "120000.00",
  "foreign_usd_price": "30.00"
}
```

At least one discount currency is required. Each amount must be positive and
lower than its configured normal price. The server stores both the submitted
payable amount and the corresponding read-only `*_percentage` value. A normal
price of 100000 and payable price of 80000 stores 20.00 percent.

List results include only `scheduled` or `active` campaigns. Explicit detail
reads may return `expired` or `ended` status. Ending is idempotent and preserves history. Overlapping campaigns resolve independently
for each supplied currency: latest start date wins, then latest ID. Prices
resolve by stay date and guest market. Apply the selected campaign to the
DailyRate/RatePeriod/normal price without increasing an already lower price.

Availability, estimate, booking-night snapshots, invoices, receipts and
confirmation email use the net room price. A room campaign does not populate an
invoice-level discount or add a separate discount deduction in the confirmation
email. Actual invoice-level discounts retain their existing behavior. Existing
bookings are not repriced when a campaign is ended or the normal price changes.

### Guest-facing original and discount prices

Hotel availability and global availability include these explicit fields on
each room type's existing `default_price` object, each `rate_plans` item, and
each `nightly_prices` item. Discount fields are not duplicated at the room type
root:

```json
{
  "has_discount": true,
  "original_price": 100000,
  "discount_percentage": 20,
  "discount_price": 80000
}
```

With no applicable campaign reduction, the shape is:

```json
{
  "has_discount": false,
  "original_price": 100000,
  "discount_percentage": 0,
  "discount_price": null
}
```

Amounts use the requested `display_currency` (default: hotel base currency).
Use `default_price.display_currency` for the room card's currency.
`default_price` and rate-plan summary fields describe the first charged night,
not the average or the entire stay. Use `nightly_prices` to display date-specific
campaigns, and the existing `total`/`display_total` for the stay total. A campaign
starting on a later night may therefore have `has_discount=false` on the card
and `has_discount=true` on that later night's entry.

Unprefixed `has_discount`, `original_price`, `discount_percentage`, and
`discount_price` always describe `base_currency`, regardless of `display_currency`.
The `usd_` versions always describe USD. This applies to `default_price`,
`default_rate_plan`, `rate_plans`, and `nightly_prices` in public responses.
Only `display_price`/`effective_display_price` follow the requested display currency.
`original_price` is the effective rate before applying the room campaign,
including any DailyRate or RatePeriod. `discount_price` is the payable rate only
when the campaign reduces that amount. Percentages are calculated from these
two same-currency amounts and rounded to two decimal places; the saved campaign
percentage remains its creation-time percentage against the configured normal
room price. A cheaper DailyRate without a campaign does not produce a discount
badge, and a campaign never raises a cheaper effective rate.

Frontend: for base currency use `original_price`, `discount_percentage`,
`discount_price`, and `has_discount`. For USD use their `usd_` versions.

Room card example (existing fields retain their payable values):

```json
{
  "default_price": {
    "rate_plan_id": 10,
    "guest_market": "local",
    "base_price": 80000,
    "base_currency": "MMK",
    "usd_display_price": 20,
    "display_price": 80000,
    "display_currency": "MMK",
    "has_discount": true,
    "original_price": 100000,
    "discount_percentage": 20,
    "discount_price": 80000
  }
}
```


### Admin Room Price screen: embedded campaigns and Edit

`GET /api/v1/admin/room-types/`, `GET /api/v1/admin/room-types/{id}/`, and
`GET/PATCH/PUT /api/v1/admin/room-types/{id}/room-price/` include a read-only
`discounts` array alongside the four normal price fields. Each entry includes
`id`, all four payable amounts, calculated percentage fields, `timing_mode`,
`start_date`, `end_date`, `status`, and `ended_at`. An unconfigured/no-campaign
room returns `discounts: []`. The array includes only active and scheduled campaigns, ordered by latest start
date and ID. Ended/expired campaigns are omitted from room list/detail,
room-price and discount-list responses; their database records remain for audit. Show active campaigns in the
current Discount Room Price section, scheduled campaigns in the upcoming
section, and use each entry's ID for Edit/End.

Edit an active or scheduled campaign:

```http
PATCH /api/v1/admin/room-types/{id}/discounts/{discount_id}/
Content-Type: application/json
```

```json
{"local_mmk_price": "75000.00", "local_usd_price": null}
```

Omitted amounts/dates remain unchanged. Null clears an amount and its stored
percentage; at least one amount must remain. Percentages are recalculated
against the current normal room prices. Editing a manual campaign preserves
its original start date. Ended or expired campaigns are read-only; create a new
campaign. Edit/End does not reprice existing booking-night snapshots. Campaigns
are prefetched once for the room list rather than fetched separately per room.


## Core directory: today's minimum room rate from Booking Engine

Core's hotel-directory `biz.minimum_room` now comes from Booking Engine rather
than Core RoomType price columns. Core calls the internal bulk endpoint:

```http
POST /api/v1/admin/directory/minimum-room-prices/
X-Booking-Admin-Key: <internal service key>
Content-Type: application/json
```

```json
{"business_ids": [77, 78], "guest_market": "local", "display_currency": "MMK"}
```

The server chooses today's date independently in each hotel's configured timezone.
It resolves DailyRate → RatePeriod → normal RatePlan and applies today's matching
campaign using the same calculation as availability. Only active, booking-enabled,
price-configured rooms and active plans are eligible. When today's eligible
room rates include campaigns, select the greatest actual discount percentage;
ties select the lowest positive payable price. If no rate is discounted, select
the lowest positive payable price. This selection uses the requested guest
market/currency and any supplied price range, including custom plans. No currency is
converted or substituted for a missing requested-currency price.

Core directory requests can add `"has_discount": true` alongside the hotel
`category` to include only hotels with a selected discounted rate today.
`false` or omission leaves discounted and undiscounted hotels eligible.
Filtering happens before pagination, so counts and pages reflect the matching
hotels. Core reuses the same bulk price result for filtering and rendering
`biz.minimum_room`. Existing market/currency parameters (`price_guest_market`,
`price_currency`) control which currency's discount is considered.

This is an advertised daily rate, independent of inventory, occupancy and stay
restrictions; it is not a guarantee that a room is currently bookable. A full
availability search remains necessary before booking.

The response is `data.minimum_rooms`, keyed by Core business ID. Missing hotels
are omitted. Optional `min_price`/`max_price` select the cheapest room *within*
the price range, preserving directory range-filter behavior. If a business-scope
header is supplied, results are limited to that business. A batch accepts at most
5000 IDs.

Core preserves the existing `biz.minimum_room` location:

```json
{
  "minimum_room": {
    "room_type_id": 1913,
    "booking_room_type_id": 66,
    "room_type_name": "Standard Double Room",
    "rate_plan_id": 10,
    "price_date": "2026-11-03",
    "guest_market": "local",
    "display_currency": "MMK",
    "display_price": 80000,
    "has_discount": true,
    "original_price": 100000,
    "discount_percentage": 20,
    "discount_price": 80000
  }
}
```

`room_type_id` remains the Core room ID for compatibility; `booking_room_type_id`
is the Booking Engine ID. The existing four normal-price fields remain in this
object, sourced from Booking setup. **Use `display_price` for the displayed
minimum payable price**, and the explicit discount fields for strike-through/badge
UI. With no campaign reduction, `has_discount=false`, `discount_percentage=0`,
and `discount_price=null`. With no eligible room, Core returns `minimum_room=null`.

Core directory inputs retain `price_guest_market` (default local) and
`price_currency` (MMK for local, USD for foreign by default). Price ranges and
price sorting now use Booking payable prices; decimal amounts are supported.
Booking results are reused within the request between sorting, pagination and
serialization, with bulk calls in chunks of 5000 business IDs. No per-hotel HTTP
requests or stale Core-price fallback are used. Service failure returns 503.

Deploy Booking Engine's endpoint first, then Core. Core must set
`BOOKING_ENGINE_BASE_URL` to the canonical HTTPS Booking API URL (no `/api/v1`
suffix or HTTP redirects) and `BOOKING_ENGINE_ADMIN_KEY` to the shared internal
key. Existing migration 0076 must already be applied; this change adds no new
migration. Directory clients call Core as before and never receive the key.

## Public room catalogue: discounts for local and foreign default prices

Both `/api/v1/public/room-types/?business_id=...` and the hotel-scoped room/OTA
catalogues now include today's campaign fields in `default_price` and each
`rate_plans` entry. Today is determined in the hotel's timezone. Catalogues still
return `availability_calculated=false`; a price is not a live availability check.

`default_prices.local` and `default_prices.foreign` in both catalogues and
availability now include:

```json
{
  "base_price": 100000,
  "base_currency": "MMK",
  "usd_display_price": 25,
  "price_date": "2026-11-03",
  "has_discount": true,
  "original_price": 100000,
  "discount_percentage": 20,
  "discount_price": 80000,
  "usd_has_discount": true,
  "usd_original_price": 25,
  "usd_discount_percentage": 10,
  "usd_discount_price": 22.5
}
```

Each market has its own independent values. Unprefixed discount fields refer to
`base_currency`; `usd_*` fields refer to USD. No applicable reduction means
`has_discount=false`, percentage zero, and discount price null; the same rule
applies independently to `usd_*`. An absent USD price stays null, never a Core
snapshot's zero placeholder.

The existing `base_price` and `usd_display_price` in this plural `default_prices`
matrix remain **normal configured Booking prices**, with campaign prices added
alongside them. Core snapshot prices are no longer used here. The selected
singular `default_price` keeps its existing payable-price semantics and uses the
requested display currency for display amounts only; discount metadata remains
separated into base and USD fields. Catalogues apply today's campaign to normal rate
plans; availability's singular price additionally resolves daily/period rules.

Availability's plural matrix applies campaigns on the requested check-in date;
catalogues use today. The matrix shows configured normal prices/campaigns for
both markets even when `guest_market` selects only one set of rate plans. Use
`nightly_prices` for per-night changes across a stay and the existing total fields
for payable stay totals.

Admin discount arrays omit ended and expired campaigns. End acknowledgement and
explicit discount-detail reads still return the campaign status/history by ID.

### Room-type extra bed prices

Apply migration `0078_room_type_extra_bed_prices`. It copies existing active default
rate-plan extra bed prices into RoomType; stored bookings and invoices stay unchanged.
New calculations use RoomType prices, including custom rate plans and PMS bookings.
Core sync and room discounts do not change these prices.

`GET /api/v1/admin/room-types/{booking_room_type_id}/extra-bed-price/` reads setup.
`PUT` replaces all four fields (`null` for omitted fields); `PATCH` retains omitted fields.
Requires normal room price setup and existing admin business scope headers.

```json
{
  "extra_bed_local_mmk_price": "20000.00",
  "extra_bed_local_usd_price": "10.00",
  "extra_bed_foreign_mmk_price": "40000.00",
  "extra_bed_foreign_usd_price": "20.00"
}
```

All values are optional, nonnegative, per bed per night. `null` clears a field;
missing foreign currency inherits the corresponding Local currency. Explicit zero
means free (does not inherit). All absent means free. Normal room price responses
and admin room type responses also expose the four setup values.

Public catalog rate plans and availability expose `extra_bed_display_currency`
alongside `extra_bed_display_price`. If USD room display exists but only MMK extra
bed exists, show Room USD + Extra Bed MMK separately; never label MMK as USD or add
the two display amounts. Availability `extra_bed_price` also contains its own
`display_currency`. Estimate extra bed summary rows include `display_amount` and
`display_currency`. Invoice accounting remains in hotel base currency; a positive
alternate-only extra bed rate needs a base-currency price before booking and
returns validation error instead of silently charging zero. No FX rate is inferred.
