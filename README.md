# Salon Max Platform

The PostgreSQL-backed hosted provider application. The recovered provider features,
the newer commissioning workflow, Estate Health and Export run in one deployment.

## Run

Install `requirements.txt`, then use the existing `Procfile` (`gunicorn app:app`).
Keep the existing `DATABASE_URL`, `SALONMAX_SECRET_KEY`,
`SALONMAX_PLATFORM_ADMIN_USERNAME` and `SALONMAX_PLATFORM_ADMIN_PASSWORD` settings.
`SALONMAX_DATABASE_URL` can override `DATABASE_URL`.

The default role is `cloud`. No cloud process opens a Pi database path. The small
hosted configuration schema is isolated in PostgreSQL's `salon_config` schema;
provider records and commissioning metadata remain in `public`.

## Screens

- `/platform`: full provider console, accounts, sites, terminals and onboarding
- `/platform/queries`: customer and transaction queries and CSV exports
- `/platform/customer-insights`: customer activity and value
- `/platform/licences`, `/platform/diagnostics`, `/platform/updates`: device operations
- `/platform/business/<id>`: customer imports, ledgers, support notes and lifecycle controls
- `/platform/estate-health`: retained estate health view
- `/platform/export.json`: complete authenticated export, including ledgers and media
- `/commissioning/platform`: retained newer commissioning and remote setup workflow

Owner sessions are shared between provider and commissioning pages. Existing
credentials continue to work. Salon trading and local settings pages are not
exposed by the hosted role. Draft remote setup does not overwrite Pi settings.

## Pi routes

`POST /v1/devices/pair` and `/v1/pairing/claim`, `POST /v1/licence/check-in`,
`GET /v1/devices/<id>/config`, `GET /v1/devices/<id>/setup-profile`,
`POST /v1/sync/events/push`, `GET /v1/customers/directory`, and
`GET /v1/customers/corrections` are served by this deployment.

Paired-device requests require a stored licence token and matching business/device
identity. Event projection and its receipt commit together; per-business
transaction locks serialize concurrent PostgreSQL projections. Retries acknowledge
an existing receipt without projecting the sale twice.

## Recovery

`recovery.sealed.json` contains the saved platform records plus the newer Pi pairing
identity. It uses authenticated encryption and a PBKDF2-derived key, not readable
customer records or tokens. The existing admin password unlocks it on first boot;
`SALONMAX_RECOVERY_KEY` can supply the original recovery password if necessary.

Startup uses a PostgreSQL advisory lock for schema upgrades and restoration. A
checksum-keyed receipt in `platform_recovery_runs` makes restore repeatable without
overwriting later edits. Original ledger IDs are retained and serial sequences
are advanced. No Pi services, files, or trading databases are changed by startup.

The retained newer commissioning code is in `commissioning.py`; the recovered
feature implementation is in `provider.py`; `entrypoint.py` consolidates their
routes and device contract. `bridge.py` shares registry identity and lifecycle
with commissioning while keeping its checklist and remote setup fields.

## Validation

Integration tests require a disposable local PostgreSQL database; they refuse to
run against a non-local database. Configure `DATABASE_URL`, the owner password
used for the sealed restore, and an arbitrary test `SALONMAX_SECRET_KEY`, install
`pytest`, then run `python -m pytest tests -q`.

Before deploying, validate recovery idempotence, protected routes, all console
screens, duplicate/concurrent sync delivery, rollback, tenant boundaries, imports,
corrections, media and export. After deploying, check `/healthz` for
`release=salon-web-v3` and `recovery_applied=true`, then verify authenticated
pages and the public /salon/ portal.

## Hosted salon portal

### Hosted back office

Business onboarding records a stable back office address such as
`https://www.salonmax.co.uk/salonmax-ultraviolet-backoffice` and prepares the
owner account from the onboarding email. The address appears on the business
record in the provider console. Open **Salon owner web access** to issue a
one-time password setup link. The owner chooses a password privately. The
provider console lists the login email and setup status.

The hosted back office shows synced customers, recent till transactions and
device check-ins, and links to staff access, online packages and marketing.
Sunbed, pricing and store records managed here are hosted records. Hardware
settings, local inventory, and till sessions are still managed on the till.

Set `SALONMAX_TRANSACTIONAL_EMAIL_KEY` to a Resend API key and
`SALONMAX_TRANSACTIONAL_EMAIL_FROM` to a verified sending address to enable
forgotten password email. A salon owner can issue staff reset links, and the
platform owner can issue salon owner reset links from **Web access**.

Open a salon in the provider console and choose **Salon owner web access** to issue
an owner invite. The salon owner can invite staff and customers. Staff can invite
customers in their own salon. Send the activation link privately after checking the
recipient's identity. Links are single use and expire after 24 hours. Customers
sign in with the email address from their existing salon customer record.

Share the customer portal link shown under **Web access** or in the salon
workspace. It opens the named salon directly at /salon/<business-public-id>/customer.
Customers with accounts at multiple salons sign in separately at each salon.
Each salon has its own installable manifest and launch URL. The service worker
caches only the generic offline page.

### Payments and marketing

For Stripe Connect direct charges, configure SALONMAX_STRIPE_SECRET_KEY,
SALONMAX_STRIPE_WEBHOOK_SECRET, and SALONMAX_STRIPE_ACCOUNTS (a JSON object mapping
salon business public IDs to connected Stripe account IDs). Create a connected
accounts webhook for /salon/payments/webhook covering checkout.session.completed
and checkout.session.async_payment_succeeded. Start with Stripe test mode.
Confirmed purchases credit the hosted minute ledger. The portal does not publish
purchase credits through the existing device corrections feed.

Customers opt in separately for email and SMS. The worker checks current consent
and the destination immediately before sending. Legacy marketing flags do not
grant permission. To process the outbox, run flask --app app marketing dispatch
from the hosted environment with SMTP SSL settings (SALONMAX_MARKETING_SMTP_HOST,
SALONMAX_MARKETING_SMTP_USER, SALONMAX_MARKETING_SMTP_PASSWORD,
SALONMAX_MARKETING_EMAIL_FROM) and/or Twilio settings (SALONMAX_TWILIO_SID,
SALONMAX_TWILIO_TOKEN, SALONMAX_TWILIO_FROM). Inspect needs_review and sending rows
with the provider before requeueing, since delivery may have succeeded.

### iOS wrapper

On macOS with Xcode and XcodeGen installed, run xcodegen generate in ios/ and
open SalonMax.xcodeproj. Set the signing team and validate account activation,
sign-in, Stripe handoff, and return to the portal on an iPhone. Before submission,
supply the final app icon and privacy details and assess App Review minimum
functionality and account deletion requirements. The wrapper source is unsigned.
