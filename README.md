# FLASHWEAR

FLASHWEAR is a fashion-tech commerce platform. This repository contains the production-quality
technical foundation built across **Phases 1–6**: foundation, accounts, catalogue, storefront, the
cart/wishlist/checkout bag, and orders, payments, inventory and delivery.

## Vision

FLASHWEAR will evolve into a full fashion ecosystem (storefront, catalog, cart/checkout, closet,
outfits, AI stylist, drops, creator marketplace, resale and more). The codebase is designed to
support that expansion without compromising maintainability.

## Technology Stack

- **Backend:** Python 3.12, Django 5.2, Django REST Framework
- **Database:** PostgreSQL (required in production)
- **Frontend:** Django Templates, Tailwind CSS, HTMX, Alpine.js
- **Caching/Broker:** Redis
- **Background tasks:** Celery
- **Testing:** pytest, pytest-django, pytest-cov
- **Code quality:** Ruff (lint + format), pre-commit, djLint
- **Configuration:** django-environ (`.env`)
- **Infra:** Docker, Docker Compose

## Architecture

- Modular Django apps under `apps/` (`core`, `accounts`, `catalog`, `shop`)
- Settings split: `config/settings/{base,development,production,testing}.py`
- Custom `User` model with email as the login identifier and a dedicated auth backend
- Sign-in / sign-out at `/accounts/login/` and `/accounts/logout/` (logout is POST-only)
- API-first foundation under `/api/v1/` with a versioned namespace and health endpoints
- Global design system compiled by Tailwind; all front-end assets are self-hosted
- Celery application (`config/celery.py`) ready for asynchronous work
- Structured logging with credential redaction and separated security stream
- Production security defaults: strict CSP, secure cookies, HSTS, no SQLite, no dev secret

## Prerequisites

- Python 3.12+
- Node.js 18+ (to rebuild the stylesheet)
- PostgreSQL and Redis, or Docker Compose to run them

## Local Setup (Virtualenv)

1. Create and activate a virtual environment:

   ```powershell
   python -m venv .venv
   .\.venv\Scripts\Activate.ps1
   ```

2. Install dependencies:

   ```powershell
   pip install -e ".[dev]"
   ```

3. Install front-end build tooling and compile the stylesheet:

   ```powershell
   npm install
   npm run build
   ```

4. Copy environment variables:

   ```powershell
   Copy-Item .env.example .env
   ```

5. Apply migrations:

   ```powershell
   python manage.py migrate
   ```

6. Create a superuser:

   ```powershell
   python manage.py createsuperuser
   ```

7. Run the development server:

   ```powershell
   python manage.py runserver
   ```

8. Access:

   | Endpoint | URL |
   | --- | --- |
   | Storefront | http://localhost:8000/ |
   | Admin | http://localhost:8000/admin/ |
   | API root | http://localhost:8000/api/v1/ |
   | Health (Django) | http://localhost:8000/health/ |
   | Health (API) | http://localhost:8000/api/v1/health/ |

   Without PostgreSQL or Redis running, development falls back to SQLite and an in-process cache
   so the server still boots. Production never accepts that fallback.

### Front-end build

`static/css/app.css` is a build artifact. Edit `frontend/css/tailwind.css` and
`tailwind.config.js`, then run:

```powershell
npm run build      # one-off production build
npm run watch:css  # rebuild on template/CSS changes
```

Tailwind scans `templates/**/*.html`, so utility classes are generated from the markup that
actually uses them. `htmx.min.js` and `alpine.min.js` are vendored under `static/vendor/`; no
asset is loaded from a third-party CDN, which is what lets production run `script-src 'self'`.

## Docker Setup (Recommended)

> Docker was not available in the environment where this project was scaffolded, so the Compose
> stack is prepared but unverified. Install Docker Desktop to run it.

```bash
cp .env.example .env      # values already point at the compose services
docker compose up --build
```

The `web` service applies migrations before starting the server, and the `frontend` service keeps
the stylesheet in sync. Create a superuser once the stack is up:

```bash
docker compose exec web python manage.py createsuperuser
```

| Service | Purpose |
| --- | --- |
| `web` | Django dev server on port 8000 |
| `db` | PostgreSQL 16 |
| `redis` | Redis 7 |
| `celery_worker` | Celery worker |
| `frontend` | Tailwind watcher writing `static/css/app.css` |

The `Dockerfile` builds the stylesheet in a Node stage and copies the result into the Python
runtime image, so a production image contains no Node toolchain. Its default command is Gunicorn
against `config.settings.production`.

## Environment Variables

See `.env.example` for every supported variable (DEBUG, SECRET_KEY, ALLOWED_HOSTS, CSRF,
DATABASE_URL, REDIS_URL, CELERY_BROKER_URL, CELERY_RESULT_BACKEND, CORS, email, storage, the
Phase 3 `CATALOG_*` settings, the Phase 5 cart/checkout settings, the Phase 6 `INVENTORY_*`
and `PAYMENT_*` settings, and the Phase 7 `LOYALTY_*`/`REVIEWS_PER_PAGE` settings). Never
commit `.env`.

## Running Tests

```powershell
pytest
pytest --cov=apps --cov=config --cov-report=term-missing
```

The suite runs against in-memory SQLite, locmem cache, in-memory storage and eager Celery, so no
external services are required. Point `TEST_DATABASE_URL` at a throwaway PostgreSQL database to
exercise PostgreSQL-specific behaviour.

## Linting, Formatting & Pre-commit

```powershell
ruff check .
ruff format .
djlint templates --lint
pre-commit install
```

Pre-commit runs Ruff, standard hygiene checks, djLint, `manage.py check`, a `makemigrations
--check` guard, the pytest suite, and the Tailwind build when templates change.

djLint runs in **lint** mode (template syntax and structure) rather than format mode. Formatting is
deliberately left alone so block spacing and single-line Tailwind class attributes stay readable;
`.djlintrc` holds the shared settings. `H006` and `H031` are not enforced — the latter asks for
`meta keywords`, which every search engine has ignored since 2009.

## Accounts

- Sign in at `/accounts/login/` with an email address and password.
- Sign out by POSTing to `/accounts/logout/` (the navbar renders the CSRF-protected form); a GET
  returns 405 so a stray link or image tag cannot end a session.
- Create a user:

  ```powershell
  python manage.py createsuperuser
  ```

- Emails are stored lowercase and matched case-insensitively, so `USER@Example.com` and
  `user@example.com` are the same account.

### Customer account area (Phase 2)

Everything a customer does to their own account lives under `/account/`, one screen per job:

| Screen | Path | Notes |
| --- | --- | --- |
| Register | `/account/register/` | No marketing opt-in; consent is asked in the profile, where it can be withdrawn |
| Verify email | `/account/verify-email/<uid>/<token>/` | Signed, expiring link; replays and tampering are no-ops |
| Resend verification | `/account/verify-email/resend/` | Signed-in customers only, POST-only with a confirm box |
| Password reset | `/account/password/reset/` | Same response whether or not the address exists |
| Overview | `/account/` | Defaults, verification state, and what is coming |
| Profile | `/account/profile/` | Names, avatar, phone, date of birth, language, currency, consent |
| Security | `/account/security/` | Password change and account closure |
| Addresses | `/account/addresses/` | Address book with one default delivery and one default billing address |
| Close account | `/account/delete/` | Password plus an explicit confirmation |

`/account/login/` and `/account/logout/` are aliases of the Phase 1 view objects, so a customer who
bookmarked either path gets identical throttling, CSRF and expiry behaviour.

Rules worth knowing before you change anything here:

- **Avatar uploads are checked by content, not by name.** Pillow opens the file, its real format
  must be in `ACCOUNT_AVATAR_ALLOWED_FORMATS`, and the declared extension must agree. SVG is not on
  the list and never should be without a sanitiser — an SVG is a script container.
- **"Keep me signed in" only changes the session's lifetime.** No second credential is issued, so
  there is nothing extra to revoke, and `ACCOUNT_REMEMBER_ME_DAYS` must stay above
  `SESSION_COOKIE_AGE` or the checkbox does nothing.
- **Address ownership is scoped through the signed-in customer** in both the views and the API. A row
  belonging to somebody else answers `404`, not `403`: a `403` would confirm the id exists.
- **The first address a customer saves is always the default delivery address**, decided server-side,
  because a bound POST ignores a form's `initial`.
- **Account closure deactivates; it does not delete.** Orders, returns and payment records need the
  row. Sessions are cleared, the password is made unusable, and
  `python manage.py anonymise_deactivated_accounts` scrubs personal data once
  `ACCOUNT_DATA_RETENTION_DAYS` has passed.
- **Audit events never store what they are about.** `AccountEvent` keeps digests and field *names*,
  never the values, and a failure to write one never breaks the request that triggered it.

### Account API

Session-authenticated JSON under `/api/v1/accounts/`, for the web client's own front end:

| Endpoint | Method | Purpose |
| --- | --- | --- |
| `/api/v1/accounts/` | `POST` | Register; signs the caller in on success |
| `/api/v1/accounts/me/` | `GET` | The signed-in customer's own record |
| `/api/v1/accounts/password/` | `POST` | Change password, re-keying the session |
| `/api/v1/accounts/addresses/` | `GET`, `POST` | List and create addresses |
| `/api/v1/accounts/addresses/<pk>/` | `GET`, `PATCH`, `DELETE` | Read, update and delete one address |

### Sign-in throttling

Failed sign-ins are counted in the cache (`apps/accounts/throttling.py`), per account and per client
address; either limit blocks the attempt with `429` and a "Too many failed sign-in attempts" notice
plus a `Retry-After` header. Only failures count, so nobody can lock a real customer out by spamming
the form, and a successful sign-in clears both counters. Keys are hashed, so raw email addresses and
IP addresses never reach Redis.

| Setting | Default | Meaning |
| --- | --- | --- |
| `LOGIN_MAX_FAILURES_PER_ACCOUNT` | `5` | Failures against one account before it locks |
| `LOGIN_MAX_FAILURES_PER_IP` | `20` | Failures from one address across all accounts |
| `LOGIN_FAILURE_WINDOW_SECONDS` | `300` | How long a counter lives |

The counts live in the cache, which is Redis in production and therefore shared by every worker.
Set `USE_X_FORWARDED_FOR=True` **only** behind a proxy that overwrites `X-Forwarded-For` on every
request. With it off (the default) the proxy's own address is the "client", so a busy proxy turns
the per-IP limit into a limit for the whole site.

## Celery

Start a worker locally (requires Redis):

```powershell
celery -A config worker -l info
```

A `debug_task` is registered for smoke tests. Serialization is JSON-only and time limits are set
in settings. Retries use bounded exponential backoff (`CELERY_TASK_RETRY_BACKOFF`,
`CELERY_TASK_MAX_RETRIES`); results expire after `CELERY_RESULT_EXPIRES` (one hour) because
nothing in the codebase polls the result backend.

`celery -A config beat` runs the periodic schedule (`CELERY_BEAT_SCHEDULE` in
`config/settings/base.py`):

| Entry | Task | Cadence setting | Default |
| --- | --- | --- | --- |
| `inventory-sweep-expired-reservations` | `inventory.sweep_expired_reservations` | `INVENTORY_SWEEP_INTERVAL_SECONDS` | 60s |
| `engagement-sweep-loyalty` | `engagement.sweep_loyalty` | `LOYALTY_SWEEP_INTERVAL_SECONDS` | 60s |
| `quests-sweep-progress` | `quests.sweep_progress` | `QUESTS_SWEEP_INTERVAL_SECONDS` | 300s |
| `notifications-sweep-queue` | `notifications.sweep_queue` | `NOTIFICATIONS_QUEUE_SWEEP_INTERVAL_SECONDS` | 300s |
| `notifications-sweep-retention` | `notifications.sweep_retention` | `NOTIFICATIONS_RETENTION_SWEEP_INTERVAL_SECONDS` | 6h |
| `notifications-sweep-drop-events` | `notifications.sweep_drop_events` | `NOTIFICATIONS_DROP_SWEEP_INTERVAL_SECONDS` | 300s |
| `notifications-sweep-points-expiring` | `notifications.sweep_points_expiring` | `NOTIFICATIONS_POINTS_SWEEP_INTERVAL_SECONDS` | 24h |
| `support-close-abandoned` | `support.close_abandoned_tickets` | `SUPPORT_SWEEP_INTERVAL_SECONDS` | 1h |
| `support-remind-pending` | `support.remind_pending_tickets` | `SUPPORT_SWEEP_INTERVAL_SECONDS` | 1h |
| `loop-expire-stale-listings` | `loop.expire_stale_listings` | `LOOP_SWEEP_INTERVAL_SECONDS` | 1h |
| `loop-expire-credits` | `loop.expire_loop_credits` | `LOOP_SWEEP_INTERVAL_SECONDS` | 1h |

Every sweep is safe to skip or run twice: database state is authoritative, and each task
resolves the same rows on the next pass -- a worker that is down delays housekeeping, it never
corrupts anything. Event-driven tasks (`notifications.send_email`, `notifications.broadcast_batch`,
`increment_post_views`) are never scheduled; they fire from `transaction.on_commit`. In
production, `celery -A config worker` and `celery -A config beat` run as separate services.

## Catalogue (Phase 3)

The catalogue is the first domain app after accounts. It holds what a garment *is* and what it
costs, and nothing about what happens when somebody buys it — that is Phase 4.

### The one decision everything follows from

**A Product is a garment; a ProductVariant is a SKU.**

A black oversized tee in five sizes is one Product and five ProductVariants. That split is what makes
colour/size selection, per-size pricing, per-SKU inventory and per-SKU analytics possible later,
without unpicking a flat catalogue in a migration.

### Data model

| Model | Answers | Notes |
| --- | --- | --- |
| `Product` | What is this? | Name, slug, descriptions, category, optional brand/fit, lifecycle, merchandising flags, SEO overrides. **No price, no stock.** |
| `ProductVariant` | What can I buy? | SKU, optional colour and size, price, optional compare-at and cost price, active flag |
| `ProductImage` | What does it look like? | File, mandatory alt text, position, primary flag, optional colour scope |
| `Category` | What kind of thing? | Self-referencing tree; a product has exactly one leaf category |
| `Brand` | Who made it? | Flat, **nullable** on Product — "no external brand" is a normal state |
| `Collection` | What is it in right now? | Many-to-many, optionally time-boxed (`starts_at` / `ends_at`) |
| `Color`, `Size`, `Material`, `Fit`, `ProductTag` | Controlled vocabulary | Rows, not free text, so filters and swatches stay consistent |

### Lifecycle

`DRAFT` → `ACTIVE` → `ARCHIVED`, and there is deliberately **no** `OUT_OF_STOCK`:

- **Draft** is invisible everywhere.
- **Active** appears on the storefront and in the public API — but only once `published_at` has
  arrived, so a drop can be dated forward.
- **Archived** keeps the record (for orders and analytics) and removes it from both.

Availability is *derived* from inventory, which arrives with the Phase 4 stock app. A product whose
variants are all sold out stays `ACTIVE` with a "sold out" badge, so restocking is a stock edit
rather than a re-publish, and `ACTIVE` keeps meaning one thing: "this is a live product".

### Invariants, and where each one is enforced

| Rule | Enforced by |
| --- | --- |
| A slug is unique | Unique index; `apps.catalog.slugs` resolves collisions for a *blank* slug, never rewrites a typed one |
| A live product is dated | `Product.save()` stamps it; check constraint `catalog_product_live_has_published_at` is the backstop |
| One variant per (product, colour, size) | Four partial unique indexes covering every `NULL` combination |
| SKU unique across the catalogue | Unique index; `create_variant()` retries on a race, and raises for a hand-typed duplicate |
| Price ≥ 0, compare-at ≥ price | Check constraints |
| A brand website is `http(s)` or blank | Check constraint — blank is a legal state, so the constraint allows it explicitly |
| A colour is a `#RRGGBB` swatch | Check constraint |
| A category is never its own ancestor | Check constraint plus `Category.check_tree_integrity()`, which walks the parent chain with a visited set |
| Exactly one primary image per product | `ProductImage.save()`, promoted automatically when the primary is deleted |
| An image's colour belongs to its product | `ProductImage.clean()` and the admin form |

### Money

`DecimalField` everywhere, never `FloatField`: a float cannot represent `1490.10` exactly, and a
rounding error that small still lands in an order total. Phase 3 has no per-product currency column
— one storefront trades in one currency until multi-currency pricing arrives with checkout — so
`CATALOG_CURRENCY_CODE` names it and the `money` template filter renders it.

### Seed data

```powershell
python manage.py seed_catalog
python manage.py seed_catalog --with-images   # also draw placeholder PNGs with Pillow
```

Twelve products across two departments, with real colour/size variants, deterministic prices and
three collections. The command is **idempotent by natural key** — every object is looked up by slug
( taxonomy ) or by `(product, colour, size)` ( variants ) — so running it twice updates in place and
deletes nothing you added. There is no `--flush`.

### Public URLs

| Page | Path |
| --- | --- |
| Product list | `/products/` (`?sort=`, `?page=`) |
| Product detail | `/products/<slug>/` (`?color=<slug>&size=<code>`) |
| Category index | `/categories/` |
| Category detail | `/categories/<slug>/` (includes the whole subtree) |
| Collection index | `/collections/` |
| Collection detail | `/collections/<slug>/` (404 when inactive or out of window) |
| Brand detail | `/brands/<slug>/` |

Variant selection is a **link, not a widget**: `?color=black&size=XL` is resolved server-side by
`services.build_variant_matrix`, so the page works without JavaScript, the selection is shareable,
and the browser never gets to invent a combination that does not exist.

### Catalogue API (read-only)

All `GET`, all public, all paginated under `/api/v1/`:

```
/api/v1/products/          ?category= ?brand= ?collection= ?sort= ?page= ?page_size=
/api/v1/products/<slug>/
/api/v1/categories/
/api/v1/categories/<slug>/
/api/v1/collections/
/api/v1/collections/<slug>/
/api/v1/brands/
/api/v1/brands/<slug>/
```

- Visibility is identical to the storefront: a draft or archived product is a **404** here too, so
  the catalogue can never be half-public.
- `page_size` is accepted but clamped to `CATALOG_API_MAX_PAGE_SIZE`.
- Money is serialised as a **string** (`"49.00"`). JSON numbers are doubles; a client doing money
  arithmetic on a float loses the cents.
- `cost_price` and `barcode` are **never** serialised. The first is staff information, the second is
  fulfilment data; the SKU already identifies a variant.
- Responses are `never_cache`: prices, sale flags and collection windows change without a deploy.

### SEO

Every catalogue page emits a description, robots, an absolute canonical URL and Open Graph tags. A
product page also emits schema.org `Product` (with an `AggregateOffer` spanning the real variant
price range) and `BreadcrumbList`. Two things are deliberately **absent**: an `availability` claim,
because stock does not exist yet, and any per-query canonical, because `?color=black` is the same
product as `?color=sand`.

### What is *not* here

No orders, payments, stock quantities, reviews, coupons, loyalty, search, recommendations, drops,
marketplace or resale. The catalogue stops at price and availability; the bag, wishlist and
checkout that use it live in the Phase 5 `shop` app below.

## Cart, wishlist & checkout (Phase 5)

`apps/shop/` is the bag and checkout app. It stops exactly at the validated handoff: no payment
instrument, no order number, no stock reservation anywhere in it — those belong to Phase 6, and
the checkout session model is built to be the thing that phase reads.

### The bag

- One ACTIVE cart per signed-in customer and one per guest session, enforced in the schema. The
  guest constraint is scoped to carts that actually have a session key, because authenticated
  carts all carry an empty one — a naive unique index would let only one signed-in customer in
  the whole database.
- Anonymous visitors get a cart keyed on the session, and the cart's id is also recorded in the
  session data. Signing in merges the guest bag into the customer's bag: Django cycles the
  session key *before* `user_logged_in` fires, so looking the guest cart up by its old key would
  always miss. Quantities sum when both bags hold the same variant; a merge that would break the
  per-item quantity cap is rolled back and skipped (logged, never fatal — a cart conflict must
  not fail a login).
- One row per (cart, variant): adding twice stacks the quantity, never a second row.
- Quantities are 1..`CART_MAX_QUANTITY_PER_ITEM` (default 99). Every mutation is POST + CSRF;
  HTMX requests get JSON, classic form posts get a redirect (PRG).

### Prices

Prices are `Decimal` end to end and nothing a browser posts can influence one — the server always
resolves the variant's price itself. `CartItem.price_snapshot` exists only to *detect* change:
totals shown to the customer are recomputed from the live variant price on every render, and the
bag and checkout pages show an explicit banner when the snapshot and the live price disagree.
Checkout validation refreshes the snapshots inside the transaction before freezing anything, so
the frozen total and the displayed total are the same number.

### Wishlist

Authenticated only; anonymous visitors are bounced to sign-in with `next` preserved so they come
back where they were. A wishlist row is either a bare product (I like this) or a specific variant
(I like *this* colour/size), and uniqueness is enforced per wishlist in the schema.

### Shipping

`apps/shop/shipping.py` answers one question: given this subtotal, what are the options and what
do they cost? Rates are settings (`CHECKOUT_SHIPPING_*`), not rows, and the amount a customer
sees is always resolved server-side from their subtotal — a posted price or method name is
simply ignored. Standard delivery is free over `CHECKOUT_SHIPPING_FREE_OVER`. A carrier
integration is later work behind this same interface.

### Checkout

Checkout is a state machine on `CheckoutSession`: OPEN → VALIDATED (→ ABANDONED). One session per
customer is reused across the whole visit — including after validation — so reopening a stale
session is a status flip rather than a second orphan row.

- Address selection is scoped to the customer's own address book *in the query*; someone else's
  address id resolves to 404, and the service refuses it again on the way past.
- Validation runs inside a transaction (`select_for_update` — a no-op on SQLite, effective on
  PostgreSQL), re-reads every line against the live catalogue, refuses ineligible lines, and
  freezes a JSON snapshot of lines, address, shipping and totals.
- Editing the bag after validating reopens the session; the snapshot is compared against the
  cart on every checkout page view, because charging a stale total is the worst bug this phase
  could ship.
- Validation is the boundary: the cart stays ACTIVE afterwards. Converting it into an order is
  Phase 6's job.

### URLs

| Screen | Path |
| --- | --- |
| Bag | `/shop/cart/` |
| Wishlist | `/shop/wishlist/` |
| Checkout | `/shop/checkout/` |

Badge/HTMX endpoints: `/shop/cart/count/`, `/shop/wishlist/count/`, `/shop/cart/totals/`. GET
never mutates: the count endpoint reads without creating a cart, and repeated checkout page views
create exactly one session.

## Orders, payments & delivery (Phase 6)

Three apps turn a validated checkout into money and a parcel: `apps/orders` (the order and its
lifecycle), `apps/inventory` (stock counters, holds, ledger) and `apps/payments` (provider
abstraction, webhooks, attempts).

### The handoff

`POST /shop/checkout/place/` calls `create_order_from_checkout`, the single door between Phase 5
and Phase 6. It is **idempotent** — the checkout row is locked first, and a converted session
returns the order it already built, so a double-clicked button cannot create two orders
(`Order.checkout` is a one-to-one, which makes a second order structurally impossible). It
**revalidates** the frozen snapshot against the live cart *and* the live catalogue: an edited bag
aborts with "please review", a price rise aborts with "changed to", and neither can be charged.
It **reserves stock** inside the same transaction — if the units are gone, `InsufficientStock`
rolls everything back and the customer keeps their validated checkout.

Order numbers are `FW-<yyyymmdd>-<8 random alphanumerics>`: date-stamped for humans, random-suffixed
so a number is not a counter anyone can walk.

### State machines

```
Order:     PENDING_PAYMENT -> PAID -> PROCESSING -> SHIPPED -> DELIVERED
                    |           \________________________________
                    |           PAID/PROCESSING/SHIPPED/DELIVERED -> REFUNDED (later phase)
                    v
               CANCELLED

Shipment:  PENDING -> PROCESSING -> SHIPPED -> DELIVERED
                    |               |
                    +-> CANCELLED <-+   (before shipping only)

Payment:   CREATED -> PENDING -> SUCCEEDED | FAILED | CANCELLED
                              SUCCEEDED -> REFUNDED (declared, not reachable in Phase 6)
```

Cancellation is only reachable while the order is unpaid: cancelling a paid order implies
returning money, and there is no refund workflow yet. `REFUNDED` exists so that workflow arrives
without a breaking change, but nothing in Phase 6 can enter it. The shipment keeps the order's
headline status in step, and every transition writes an append-only event row with actor and note.

### Stock

- `Stock` holds `on_hand` and `reserved`; `available` is derived, with a check constraint that
  `reserved` can never exceed `on_hand`.
- A hold (`Reservation`) moves `ACTIVE -> RELEASED | EXPIRED | CONSUMED` through a conditional
  `UPDATE ... WHERE status = 'active'`: only the caller that wins the row (rowcount 1) touches the
  counters, so a replayed webhook or a double sweeper pass cannot double-count.
- Holds live `INVENTORY_RESERVATION_MINUTES` (default 30). The sweeper
  (`inventory.sweep_expired_reservations`, one beat entry every `INVENTORY_SWEEP_INTERVAL_SECONDS`)
  releases *abandoned checkout* holds; holds attached to a placed order are never swept — an unpaid
  order quietly losing its stock would be worse than one that keeps it.
- Every counter change writes an `InventoryMovement` ledger row: signed delta, kind
  (received/reserved/released/expired/sold/adjustment) and a reference (`order:<number>`,
  `sweeper`, ...). Manual counts go through the admin's adjust workflow, which previews the delta
  and refuses to go below the held amount.
- **Lock order:** payment → order → reservation → stock rows (ascending variant id, stock always
  last), documented in `apps/inventory/services.py`. No transaction ever holds a stock lock while
  waiting on another row, so the graph has no cycles.

### Payments

- Providers are pluggable through `PAYMENT_PROVIDER` (default `development`). The registry raises
  `ImproperlyConfigured` for an unknown name, and production settings refuse the development
  provider or an empty `PAYMENT_WEBHOOK_SECRET`.
- `POST /payments/webhook/<provider>/` requires `X-Flashwear-Signature: sha256=<hex>` (HMAC-SHA256
  of `"<timestamp>." + raw body`) plus `X-Flashwear-Timestamp`, refused outside
  `PAYMENT_WEBHOOK_TOLERANCE_SECONDS` (300). Idempotency is the `(provider, event_id)` unique
  insert: a redelivery finds the stored event and changes nothing — no caches, no "have we seen
  this?" query a race could slip past.
- No raw card data is ever stored, no provider call runs inside a database transaction, and a
  late success on a cancelled order lands as a "manual refund required" note rather than silent
  stock movement.
- The development provider is walkable end to end without an external processor: the payment page
  offers simulate buttons (`POST /payments/<pk>/simulate/`), both the simulate and cancel endpoints
  are dev-only and owner-only, and settled payments redirect to the confirmation page instead of
  reprocessing.

### URLs

| Screen | Path |
| --- | --- |
| Place order | `POST /shop/checkout/place/` |
| Pay | `/shop/checkout/payment/<number>/` |
| Confirmation | `/shop/checkout/done/<number>/` |
| Order history | `/account/orders/` |
| Order detail | `/account/orders/<number>/` |
| Cancel (unpaid) | `POST /account/orders/<number>/cancel/` |
| Webhook | `POST /payments/webhook/<provider>/` |
| Simulate / cancel attempt | `POST /payments/<pk>/simulate/`, `POST /payments/<pk>/cancel/` |

### API

`GET /api/v1/orders/` (paginated, newest first) and `GET /api/v1/orders/<number>/`,
session-authenticated and scoped to the signed-in customer in the query itself — someone else's
number is a 404, never a 403 that confirms it exists. The detail payload is assembled from the
order's frozen snapshot; payment appears only as a status summary with no provider reference, and
both endpoints are `no-cache`.

### What is *not* here

Refunds and returns, split shipments, partial captures, tax, multi-currency, and any payment
provider other than the development simulator. The enum values and state graphs those features
need exist; the workflows deliberately do not.

## Reviews, loyalty & promotions (Phase 7)

### The one decision everything follows from

Moderated reviews, FLASH Points and promotion codes share one app (`apps/engagement`) because
they share one rule: nothing that touches money or public trust is written from the browser.
Checkout remains the only writer of orders — engagement contributes a *discount* which checkout
re-validates under locks before it becomes an order, and reviews are only ever born from a real
purchase.

### Reviews

- Purchase-gated: you can only review a product from your own order in
  `paid/processing/shipped/delivered` that contains it; everyone else sees the published list
  plus a reason ("Sign in to review", "purchase required", "you already reviewed this").
- Born `PENDING`; only operators publish, and moderation never deletes. Editing a published
  review returns it to `PENDING` (audit over convenience). Deletion is POST-only.
- The product page carries the full section: histogram (string keys `"1".."5"`), allow-listed
  sorts (`newest/highest/lowest`), verified-purchase pill, pagination at `REVIEWS_PER_PAGE`.
- API: `GET/POST /api/v1/products/<slug>/reviews/`, `GET/PATCH/DELETE /api/v1/reviews/<pk>/`
  under session authentication — anonymous writes get 403 (no credential challenge exists).

### FLASH Points

- Append-only integer ledger (`PointsTransaction`); the balance is the sum of credits minus
  debits, and admin corrections append a row rather than edit one.
- Rates: `LOYALTY_EARN_RATE=10` points per 1.00 spent on `subtotal − promotion` (floored),
  `LOYALTY_REDEEM_RATE=100` points = 1.00 off, redemptions in steps of
  `LOYALTY_REDEEM_INCREMENT`, capped at `LOYALTY_MAX_REDEEM_PERCENT` of the pre-shipping total.
- Earning fires on `order_paid` (`ref=order:<number>`), cancel and expiry are ledger events
  too; the beat task `engagement.sweep_loyalty` expires rows older than
  `LOYALTY_EXPIRY_DAYS` while active holds defer their own expiry.
- Checkout never mutates balance: validation freezes a `PointsReservation`, placement converts
  it, any handoff disagreement or cancel releases it. The dashboard (`/accounts/loyalty/`,
  nav "Loyalty") shows balance, expiring-soon points and the paginated ledger.

### Promotions

- `Promotion` campaigns: percentage or fixed, start/end window, global and per-user usage
  limits, minimum order amount, and a toggle for whether points may stack on top.
- One central engine decides every verdict. Unknown or malformed codes get the same generic
  "not valid" message (no enumeration); a known code that is inactive, out of window or
  limit-reached gets its specific reason.
- `used_count` increments only at order placement, under row locks, and is never released on
  cancel — a code that sold out stays sold out. `PromotionUsage` is unique per
  `(promotion, order)` so a replay cannot double-charge the budget.

### Checkout math and the handoff

`total = subtotal − promotion − loyalty + shipping`, with shipping always computed from the
pre-discount subtotal. Validation freezes both discounts into the snapshot as strings;
placement re-runs the engine (`_revalidate_discounts`) and re-attaches points conditionally
(`status=ACTIVE AND points=frozen`) after the order row exists — any disagreement raises
`StaleCheckout`, rolls everything back and says "please review your order again". Lock order:
checkout → payment → order → reservation → stock rows (asc) → promotion → the customer's
`auth_user` row as a leaf.

### URLs

- `/products/<slug>/` — review section; POST `/products/<slug>/reviews/create/`,
  `/reviews/<pk>/edit/`, `/reviews/<pk>/delete/`
- `/accounts/loyalty/` — points dashboard
- `/checkout/` — "Offers & FLASH Points" panel; POST `/checkout/promotion/` and
  `/checkout/loyalty/` apply or clear before validation
- `/api/v1/products/<slug>/reviews/`, `/api/v1/reviews/<pk>/`,
  `/api/v1/checkout/promotion/`

### Settings

`LOYALTY_EARN_RATE`, `LOYALTY_REDEEM_RATE`, `LOYALTY_REDEEM_INCREMENT`,
`LOYALTY_MAX_REDEEM_PERCENT`, `LOYALTY_MIN_ORDER_AMOUNT`, `LOYALTY_EXPIRY_DAYS`,
`LOYALTY_RESERVATION_MINUTES`, `LOYALTY_SWEEP_INTERVAL_SECONDS`, `REVIEWS_PER_PAGE` — all
documented in `.env.example`.

### What is *not* here

Review photos, helpfulness voting, point transfers or gifting, tiered VIP status, refunds of
redeemed points beyond cancel reversal, BOGO or multi-item promotions, stacking more than one
code (one code at a time by design), and automatic coupon email campaigns.

## Project Structure

```
flashwear/
├── manage.py
├── config/                     # urls, asgi/wsgi, celery, settings split, api/v1
│   ├── api/v1/                 # versioned API routing + root view
│   └── settings/               # base, development, production, testing
├── apps/
│   ├── core/                   # health, SiteConfiguration, context processor, tags, redaction
│   ├── accounts/               # custom User, manager, email auth backend, login/logout, admin
│   ├── catalog/                # products, variants, images, taxonomy, selectors, services, API
│   ├── shop/                   # cart, wishlist, shipping, checkout session, merge signals
│   ├── inventory/              # stock counters, holds, movement ledger, sweeper task
│   ├── orders/                 # order, items, address snapshot, events, shipments, admin, API
│   ├── payments/               # attempts, events, provider registry, signed webhooks
│   └── engagement/             # reviews, FLASH Points ledger, promotion engine, sweep task
├── frontend/css/tailwind.css   # Tailwind entry point (design tokens live in tailwind.config.js)
├── templates/                  # base, components, pages, accounts, catalog, shop, error pages
├── static/                     # built css, vendored js, app js
├── media/                      # user uploads (development)
├── logs/                       # rotating application logs
├── tests/                      # project-level test suite
├── conftest.py                 # registers the catalogue fixture plugin
├── .env.example
├── .djlintrc
├── .pre-commit-config.yaml
├── .gitignore
├── docker-compose.yml
├── Dockerfile
├── package.json
├── pyproject.toml
├── tailwind.config.js
└── README.md
```

Inside `apps/catalog/`, the split is the one the rest of the project uses:

```
models/          # base, attributes, taxonomy, product — the schema and its invariants
services.py      # writes with rules: variant matrix, publishing, gallery, category tree
selectors.py     # reads with intent: every listing query, eagerly loaded and totally ordered
forms.py         # admin validation that turns database constraints into readable errors
serializers.py   # the public API payload
api.py           # DRF views, pagination, allow-listed filters
views.py         # the storefront pages
urls.py          # slug-based public routes
seo.py           # metadata, canonical URLs, Open Graph, JSON-LD
templatetags/    # money formatting, sort controls, query-preserving links
management/commands/seed_catalog.py
```

## Future Phases

- **Phase 8+:** FLASH DNA, closet, outfits, AI stylist, drops, creators, resale, gamification —
  plus the Phase 6 follow-ons the state graphs already reserve room for: refunds/returns and
  real payment providers.

Phases 1 (foundation), 2 (accounts), 3 (catalogue), 4 (storefront, discovery, API, SEO), 5
(cart, wishlist, checkout), 6 (orders, payments, inventory, delivery) and 7 (reviews, FLASH
Points, promotions) are built. Phase 7 picks up exactly where Phase 6 stops: the order
placement transaction gains two handoff checks — the promotion discount is re-validated and
the frozen points reservation is re-attached — so a stale review step can never produce a
wrong total, and the navigation's Loyalty entry is live while the remaining not-yet-built
surfaces are still listed as "coming soon".

The catalogue deliberately stops at the edge of selling. `ProductVariant` has a price but no
quantity — inventory is the `inventory` app's job (Phase 6), and the API, templates and admin were
all written with that boundary in mind rather than being retrofitted for it. Cart presence is
never presented as availability: every bag and checkout screen says availability is confirmed
before order placement.

## Security Notes

- No secrets are committed; `.env` is git-ignored and `.env.example` holds placeholders only.
- Production refuses to start with the development `SECRET_KEY`, an empty `ALLOWED_HOSTS`,
  missing `CSRF_TRUSTED_ORIGINS`, a SQLite database, or a missing `REDIS_URL`.
- Log output is passed through `RedactingFormatter`, which removes passwords, tokens, API keys,
  session cookies and email addresses after interpolation.
- CSP is `script-src 'self'` with no third-party origins; `'unsafe-inline'` remains only for
  `style-src` and can be dropped once inline styles are eliminated.
- Logout is POST-only and CSRF-protected; storefront sign-in is throttled per account and per client
  address (see [Sign-in throttling](#sign-in-throttling)).
- Passwords, reset tokens and verification tokens are never logged or stored in plaintext; account
  audit records keep digests rather than the values they describe.
- Avatar uploads are validated by content with Pillow and served from a non-executable path; SVG is
  rejected outright.
- Catalogue image uploads follow the same rules, and are stored under a random UUID name rather than
  the filename the client sent — a name is attacker-controlled text that would otherwise end up in a
  public URL.
- The catalogue API is public and read-only, and never serialises `cost_price` or `barcode`.