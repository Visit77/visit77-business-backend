# Visit77 Booking Engine

A standalone Django/DRF service for the hotel admin and customer booking flows in the supplied designs. Visit77 Core remains the source of truth for businesses and descriptive room-type data. This service owns rates, daily inventory, reservations, guests, add-on services, payments, invoices/receipts, room assignment, check-in and check-out.

## Ownership boundary

| Data | Source of truth |
|---|---|
| Business identity, profile, owner access | Visit77 Core |
| Room type name, photos, beds, amenities, capacity | Visit77 Core |
| Booking enablement, rate plans, daily prices | Booking Engine |
| Physical-room operating status | Booking Engine (Core rooms can seed it) |
| Daily inventory, holds and reservations | Booking Engine |
| Guests, add-ons, payment snapshots, invoice and receipt | Booking Engine |

Core objects are linked by `core_business_id` and `core_room_type_id`; there are no cross-database foreign keys. Booking records keep immutable room/rate/policy snapshots.

## Supported screen flows

- Customer availability search by date, occupancy and local/foreign market
- Multi-room booking with a 15-minute inventory hold and idempotent checkout
- Guest/NRC/passport data and configurable add-on services
- Booking summary, invoice/payment and receipt data
- Hotel room board by building, floor, type and operational status
- Physical-room assignment, check-in, check-out and cancellation
- Daily inventory, stop-sell, rate plans and date-level pricing

## Run locally

```bash
cd booking_engine_service
cp .env.example .env
../venv/bin/python manage.py migrate
../venv/bin/python manage.py runserver 8080
```

## Seed a complete demo flow

Use an unused Core business id. The default creates demo hotels `990001` through `990003` and is safe to run repeatedly:

```bash
../venv/bin/python manage.py seed_demo_data
```

To choose another unused id:

```bash
../venv/bin/python manage.py seed_demo_data --business-id 990100
```

The primary hotel includes 10 physical rooms (5 available, 2 reserved, 1 occupied, 1 cleaning, 1 out-of-service), default and custom RatePlans, a high-season RatePeriod, a festival DailyRate, 128 days of inventory, add-ons, paid/confirmed/checked-in/pending bookings, assignments, and one unassigned room request. Two additional hotels exercise global search. Point the Postman environments at the printed business id and dates.

SQLite is the zero-configuration default for direct local Python runs. Docker Compose expects PostgreSQL and Redis to already be available on the host/server; set `DB_ENGINE=postgres`, the `DB_*` values, `CELERY_BROKER_URL`, and `CELERY_RESULT_BACKEND`.

## Docker

The Docker setup runs only application processes: Gunicorn for HTTP, Celery worker, Celery beat for scheduled tasks, and optional Daphne for ASGI. PostgreSQL and Redis are external server services, not Compose containers.

Development:

```bash
cp .env.example .env
docker compose -f docker-compose.dev.yml up --build
```

Debug with attach ports bound to localhost:

```bash
docker compose -f docker-compose.debug.yml up --build
```

Production-style:

```bash
docker compose -f docker-compose.prod.yml up --build -d
```

Run optional Daphne only when ASGI serving is needed:

```bash
docker compose -f docker-compose.prod.yml --profile asgi up --build -d
```

## Settings environments

Settings are standalone from Visit77 Core and split by deployment environment:

```text
config/settings/base.py
config/settings/development.py
config/settings/production.py
config/settings/test.py
```

`manage.py` defaults to development. Gunicorn/ASGI default to production. Production refuses to start with the development secret, default admin key, empty allowed hosts, or SQLite. CI can use `DJANGO_SETTINGS_MODULE=config.settings.test`.

## Synchronize a Core hotel

Set `CORE_BASE_URL` and a service token that can read the existing Core endpoints:

- `GET /business/{core_business_id}/`
- `GET /room_types/?business_id={core_business_id}`
- `GET /physical_rooms/?business_id={core_business_id}` (optional seed)
- `GET /booking-integrations/businesses/{id}/access/` for hotel-user authorization at the API gateway/admin client

Provisioning now uses the subscription-gated service endpoint `GET /booking-integrations/businesses/{id}/provisioning/` with `X-Booking-Service-Key`. Core returns HTTP 403 unless the Direct Booking subscription is active.

Run a manual sync:

```bash
../venv/bin/python manage.py sync_core_business 123
```

Or call:

```bash
curl -X POST http://localhost:8080/api/v1/admin/core-sync/businesses/123/ \
  -H 'X-Booking-Admin-Key: change-me'
```

Repeated syncs refresh Core-owned business and room projections. The provisioning payload derives local and foreign standard RatePlans from each Core RoomType's price fields; Booking Engine stores those as sync-managed default plans. Hotel admins can create custom RatePlans, and subsequent Core syncs never overwrite them.

In the subscription-gated integration, Core sends an outbox event after activation. Booking Engine then pulls the authorized provisioning bundle. Revoked/expired events disable new bookings and release pending holds. See [Core integration documentation](../docs/direct-booking-integration.md).

## Main APIs

Public:

```text
GET  /api/v1/public/search/availability/
GET  /api/v1/public/hotels/{core_business_id}/availability/
GET  /api/v1/public/hotels/{core_business_id}/add-ons/
POST /api/v1/public/bookings/
GET  /api/v1/public/bookings/{public_token}/
POST /api/v1/public/bookings/{public_token}/demo-payment/  # development only
```

## Postman user flow

Import both files below, select the `Visit77 Booking - Local` environment, update `core_business_id` and stay dates, then run requests `00` through `06` in order. Request `07` is optional and requires an active add-on.

- `postman/Visit77-Booking-User.postman_collection.json`
- `postman/Visit77-Booking-User.postman_environment.json`

Hotel Admin UI-only collection (dashboard, synced rooms, inventory, rates, booking operations and add-ons; no public/Core/Superadmin APIs):

- `postman/Visit77-Booking-Hotel-Admin-UI.postman_collection.json`
- `postman/Visit77-Booking-Hotel-Admin-UI.postman_environment.json`

Booking Engine Superadmin collection using a Visit77 Core-issued JWT:

- `postman/Visit77-Direct-Booking-AddOn-Superadmin.postman_collection.json`

Hotel-admin RatePlan, RatePeriod and DailyRate collection:

- `postman/Visit77-Booking-Rates.postman_collection.json`
- `postman/Visit77-Booking-Rates.postman_environment.json`

Hotel operations, inventory and room-board collection:

- `postman/Visit77-Booking-Operations.postman_collection.json`
- `postman/Visit77-Booking-Operations.postman_environment.json`

Frontend rate-management contract and UI guidance: [`docs/rates-frontend-guide.md`](docs/rates-frontend-guide.md).

Hotel admin (`X-Booking-Admin-Key` required):

```text
/api/v1/admin/hotels/
/api/v1/admin/room-types/
/api/v1/admin/physical-rooms/
/api/v1/admin/rate-plans/
/api/v1/admin/rate-periods/
/api/v1/admin/rates/
POST /api/v1/admin/rates/bulk-upsert/
/api/v1/admin/inventory/
GET /api/v1/admin/add-on-templates/
GET/POST /api/v1/admin/add-on-template-requests/
/api/v1/admin/add-ons/
/api/v1/admin/bookings/
GET  /api/v1/admin/integration-status/
GET  /api/v1/admin/room-board/
POST /api/v1/admin/inventory/bulk-upsert/
POST /api/v1/admin/bookings/{id}/payment/
POST /api/v1/admin/bookings/{id}/refund/
POST /api/v1/admin/bookings/{id}/assign-room/
POST /api/v1/admin/bookings/{id}/unassign-room/
POST /api/v1/admin/bookings/{id}/change-room/
POST /api/v1/admin/bookings/{id}/check-in/
POST /api/v1/admin/bookings/{id}/check-out/
POST /api/v1/admin/bookings/{id}/cancel/
```

Core-generated default RatePlans are read-only. Booking-created custom RatePlans support POST/PATCH and soft-deactivation through DELETE.

Production hotel-admin requests require both service headers. Core/BFF must verify the logged-in user's business permission and Direct Booking entitlement before forwarding them:

```http
X-Booking-Admin-Key: <service-secret>
X-Booking-Business-ID: <core-business-id>
```

Never ship the service secret in frontend or mobile builds.

Room board supports two purpose-built response shapes:

```text
GET /api/v1/admin/room-board/?date=2026-08-06&building_id=7001&view=compact
GET /api/v1/admin/room-board/?date=2026-08-06&building_id=7001&view=detail
```

- `view=compact` (default) is for the small-icon grid and returns only room identity and status.
- `view=detail` is for large cards and adds the room type, one display price, timeline and minimal assignment indicators.
- `include_flat_rooms=true` adds the legacy flat `rooms` list; otherwise rooms only appear inside `floors[].rooms`.
- `include_unassigned=true` adds unassigned confirmed bookings. Keep it off unless that panel is visible.

Hotels manage prices in their own `base_currency` (currently synced/stored on `Hotel.base_currency`). USD is a display amount only. RatePlans should be split by guest market and policy, not by currency:

- Local / Foreigner / All market
- refundable / non-refundable / breakfast included policy
- base amount used for booking/payment totals
- optional USD display amount used for customer display

Seasonal or one-day pricing can be created or replaced for an inclusive stay-date range in one transaction:

```http
POST /api/v1/admin/rates/bulk-upsert/
X-Booking-Admin-Key: ...
Content-Type: application/json
```

```json
{
  "rate_plan_id": 10,
  "start_date": "2026-10-01",
  "end_date": "2026-12-31",
  "base_price": "120000.00",
  "usd_display_price": "35.00",
  "min_stay": 1,
  "closed_to_arrival": false,
  "closed_to_departure": false
}
```

Both dates are included. Existing DailyRate rows in the range are updated; missing dates are created. `price` is still accepted as a backwards-compatible alias for `base_price`. Confirmed booking-night snapshots are never repriced.

For effective-dated pricing, create a RatePeriod. An omitted/null `end_date` means the new price remains effective indefinitely:

```http
POST /api/v1/admin/rate-periods/
X-Booking-Admin-Key: ...
Content-Type: application/json
```

```json
{
  "rate_plan": 10,
  "name": "Price from October 2026",
  "start_date": "2026-10-01",
  "end_date": null,
  "base_price": "120000.00",
  "usd_display_price": "35.00",
  "min_stay": 1,
  "closed_to_arrival": false,
  "closed_to_departure": false,
  "is_active": true
}
```

Active periods for one rate plan cannot overlap. A RatePlan already belongs to the local, foreign or all market. Do not create separate MMK/USD copies of the same plan; store `base_price` and optional `usd_display_price` on the same plan/period/daily override. Effective price priority is `DailyRate → RatePeriod → RatePlan.base_price`. Use DailyRate for an exceptional day and RatePeriod for seasons or permanent future changes.

Core RoomType pricing syncs into two default plans:

```text
local_base_price + local_usd_display_price
    -> Local Standard RatePlan(base_price, usd_display_price)

foreign_base_price + foreign_usd_display_price
    -> Foreign Standard RatePlan(base_price, usd_display_price)
```

Both plans use the hotel's base currency. USD amounts are display-only; booking totals and payment snapshots remain in the hotel base currency.

Example global availability request (all active subscribed hotels):

```bash
curl 'http://localhost:8080/api/v1/public/search/availability/?check_in=2026-07-10&check_out=2026-07-12&adults=2&children=1&guest_market=local&display_currency=USD&q=Yangon&page=1&page_size=20'
```

`q` is optional and matches hotel name, slug or address. `display_currency=USD` returns USD display amounts when configured; booking/payment totals remain in the hotel's base currency. The response contains only hotels with at least one available room and includes pagination metadata. The hotel-specific availability endpoint remains available for a selected hotel's detail page.

Example booking body:

```json
{
  "core_business_id": 123,
  "check_in": "2026-07-10",
  "check_out": "2026-07-12",
  "contact_name": "Myo Myo",
  "contact_phone": "09112233445",
  "guest_market": "local",
  "rooms": [
    {"core_room_type_id": 301, "rate_plan_id": 10, "quantity": 2, "adults": 4, "children": 0, "extra_beds": 1}
  ],
  "guests": [
    {"name": "Myo Myo", "phone": "09112233445", "nrc_number": "1/MaGaNa(N)112233", "is_primary": true}
  ],
  "add_ons": [
    {
      "add_on_id": 5,
      "quantity": 1,
      "configuration": {
        "airport_name": "Yangon International Airport",
        "flight_number": "8M-385",
        "arrival_date": "2026-07-10",
        "arrival_time": "10:30"
      }
    }
  ]
}
```

The hotel-admin UI reads `/api/v1/admin/add-on-templates/` to show published service types and render `configuration_schema.fields`. A selected template supplies its schema automatically. If the needed service is missing, the hotel submits a template request with its proposed version-1 fields instead of bypassing approval with arbitrary JSON. Booking creation validates required fields, field types, select options, and unknown keys before holding inventory.

Add-on templates are stored and versioned in Booking Engine. Hotels submit missing-service requests through `/api/v1/admin/add-on-template-requests/`. Visit77 Superadmins call `/api/v1/superadmin/add-on-template-requests/` directly with their Core-issued JWT; Booking verifies the signature and `is_superuser` claim without copying Core user rows. Approval publishes a new template version; the previously published version is archived, while existing Add-ons and booking snapshots retain their original template/schema references.

Send a unique `Idempotency-Key` header on booking creation. Repeating the same request/key returns the original booking and does not consume inventory twice.

## Production jobs and authorization

Run `python manage.py expire_booking_holds` every minute (Celery beat or cron) to release unpaid inventory. Public booking detail uses an unguessable UUID token. Hotel-admin traffic should first be authorized with the existing Core access endpoint; the shared admin key is intended for trusted gateway/service traffic, not mobile clients.

Inventory changes use database transactions and row locks. Use PostgreSQL in production; SQLite is only for development and tests.

Guest-payment provider calls are intentionally not connected yet. A future hotel payment-account integration must settle each booking directly to the hotel's merchant/bank account; it is separate from Visit77 subscription billing.

## Tests

```bash
../venv/bin/python manage.py test
```
