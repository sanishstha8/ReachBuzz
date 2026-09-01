# WhatsApp Bulk Messaging

A Django web application for managing opted-in WhatsApp contacts and sending
message campaigns through the **official Meta WhatsApp Business Platform Cloud API**.

> **Scope and compliance.** This project uses only Meta's official Cloud API. It does
> not automate WhatsApp Web, does not use unofficial WhatsApp libraries, does not scrape
> WhatsApp users, and contains no feature intended to evade rate limits, template approval,
> or any other platform restriction. Messages are sent only to recipients whose consent is
> recorded in the system. Throughput is bounded by the messaging limits and quality rating
> of the connected WhatsApp Business Account.

---

## Table of contents

1. [Project overview](#1-project-overview)
2. [Features](#2-features)
3. [Technology stack](#3-technology-stack)
4. [Architecture](#4-architecture)
5. [Installation](#5-installation)
6. [PostgreSQL setup](#6-postgresql-setup)
7. [Environment variables](#7-environment-variables)
8. [Redis setup](#8-redis-setup)
9. [Celery setup](#9-celery-setup)
10. [Running Django](#10-running-django)
11. [Running the Celery worker](#11-running-the-celery-worker)
12. [Mock provider usage](#12-mock-provider-usage)
13. [Meta API integration setup](#13-meta-api-integration-setup)
14. [Webhook setup](#14-webhook-setup)
15. [Testing](#15-testing)
16. [Production deployment considerations](#16-production-deployment-considerations)
17. [Build status by phase](#17-build-status-by-phase)
18. [Consent model](#18-consent-model)
19. [Campaigns and templates](#19-campaigns-and-templates)
20. [Sending pipeline](#20-sending-pipeline)

---

## 1. Project overview

An authorized business operator signs in, manages a contact list with recorded consent,
groups contacts into audiences, builds a campaign around an **approved WhatsApp message
template**, previews it, and launches it. Sending happens in a background queue — one job
per recipient — so a 1,000-recipient campaign never runs inside an HTTP request. Meta's
webhooks stream delivery, read and failure events back, and the campaign page shows live,
recipient-level status.

Designed for campaigns of roughly **100–1,000 recipients**, subject to the limits,
permissions and policies of the connected Meta WhatsApp Business account.

## 2. Features

| Area | Capability |
|---|---|
| Authentication | Email sign-in, session security, role-based authorization (Administrator / Operator / Viewer), CSRF protection, sign-in audit trail |
| Contacts | CRUD, search, filter, pagination, E.164 normalization, duplicate detection, consent tracking, per-contact message history |
| CSV import | Validated upload, per-row error reporting, duplicate detection, explicit-consent-only opt-in |
| Groups | Many-to-many contact lists used as campaign audiences |
| Campaigns | Wizard (name → audience → template → preview → confirm), recipient count, draft/schedule/launch/pause/cancel |
| Templates | Mirror of the WABA's approved templates, variable substitution, safe preview |
| Sending | Celery + Redis, one job per recipient, retries with backoff, self-imposed rate ceiling, idempotent dispatch |
| Webhooks | Signed Meta webhook endpoint, idempotent event handling, background processing |
| Monitoring | Live campaign progress, status breakdown, failed-message detail |
| Compliance | Opt-in/opt-out state, inbound STOP handling, append-only audit log |
| API | REST endpoints for every resource with OpenAPI docs at `/api/docs/` |

## 3. Technology stack

- **Python** 3.12+ (developed and tested on 3.13)
- **Django** 5.2 · **Django REST Framework** · **django-filter** · **drf-spectacular**
- **PostgreSQL** 14+ (developed against 18)
- **Celery** 5 · **Redis** 6+
- **Bootstrap 5** with vanilla JavaScript (`fetch`) — no frontend build step
- **phonenumbers** for E.164 parsing, **django-environ** for configuration
- **pytest** + **pytest-django** for tests, **ruff** for linting

## 4. Architecture

### Request and send path

```
User ──▶ Django (thin views)
             │
             ▼
         Services (business logic, state machines)
             │
             ├──▶ PostgreSQL      one Message row per recipient, created up front
             │
             └──▶ Redis queue ──▶ Celery worker ──▶ WhatsAppProvider
                                                        │
                                            ┌───────────┴───────────┐
                                            ▼                       ▼
                                  MockWhatsAppProvider    MetaWhatsAppProvider
                                     (development)         (Cloud API, HTTPS)
                                                                    │
                                                                    ▼
                                                                WhatsApp
Meta webhook ──▶ signature check ──▶ persist raw event ──▶ 200 OK
                                              │
                                              └──▶ Celery ──▶ update Message status
```

### Django apps

| App | Responsibility |
|---|---|
| `config` | Split settings, root URLs, Celery app, WSGI/ASGI |
| `core` | Base models, phone normalization, audit log, DRF pagination and error envelope, permissions, mixins |
| `accounts` | Custom `User` model, authentication, roles |
| `contacts` | Contacts, groups, CSV import |
| `campaigns` | Campaigns, audience resolution, lifecycle state machine |
| `messaging` | Per-recipient `Message` records, status events, statistics |
| `whatsapp` | Provider abstraction, templates, Celery send tasks, webhook endpoint |
| `dashboard` | Aggregate statistics and monitoring pages |

The app is named `messaging`, not `messages`, to avoid shadowing `django.contrib.messages`.

### Design rules

- **Thin views, fat services.** Views validate and serialize; all business logic lives in
  `services.py`. Provider-specific code exists only under `whatsapp/services/`.
- **Provider abstraction.** `WhatsAppProvider` is an abstract base class; the concrete
  implementation is chosen by the `WHATSAPP_PROVIDER` setting. Swapping mock → Meta changes
  one environment variable and no application code.
- **Idempotency by database claim.** Each `Message` is claimed with a conditional `UPDATE`
  before sending, so a duplicated Celery job cannot send twice.
- **Secrets only in the environment.** No credential is hardcoded, logged, rendered in a
  template, or returned by the API.

## 5. Installation

```bash
git clone <your-repository-url>
cd whatsapp-bulk-messaging

# Create and activate a virtual environment
python -m venv .venv
source .venv/Scripts/activate     # Windows (Git Bash)
# .venv\Scripts\Activate.ps1      # Windows (PowerShell)
# source .venv/bin/activate       # macOS / Linux

pip install -r requirements/dev.txt
```

Then create your `.env`:

```bash
cp .env.example .env
python -c "from django.core.management.utils import get_random_secret_key as k; print(k())"
# paste the output into SECRET_KEY= in .env
```

## 6. PostgreSQL setup

Create the database and a dedicated role:

```bash
psql -U postgres -c "CREATE DATABASE whatsapp_bulk_messaging;"
psql -U postgres -c "CREATE USER wbm_user WITH PASSWORD 'change-me';"
psql -U postgres -c "ALTER ROLE wbm_user SET client_encoding TO 'utf8';"
psql -U postgres -c "ALTER ROLE wbm_user SET default_transaction_isolation TO 'read committed';"
psql -U postgres -c "ALTER ROLE wbm_user SET timezone TO 'UTC';"
psql -U postgres -c "GRANT ALL PRIVILEGES ON DATABASE whatsapp_bulk_messaging TO wbm_user;"
psql -U postgres -d whatsapp_bulk_messaging -c "GRANT ALL ON SCHEMA public TO wbm_user;"
```

Point `DATABASE_URL` at it:

```
DATABASE_URL=postgres://wbm_user:change-me@localhost:5432/whatsapp_bulk_messaging
```

Then apply the migrations:

```bash
python manage.py migrate
python manage.py createsuperuser
```

## 7. Environment variables

Every setting is documented in [`.env.example`](.env.example). The essentials:

| Variable | Purpose |
|---|---|
| `SECRET_KEY` | Django cryptographic signing key. Generate a unique value per environment. |
| `DEBUG` | `True` locally, always `False` in production. |
| `ALLOWED_HOSTS` | Comma-separated hostnames. No default in production — an unset value fails loudly. |
| `DATABASE_URL` | `postgres://USER:PASSWORD@HOST:PORT/NAME` |
| `REDIS_URL`, `CELERY_BROKER_URL` | Queue broker connection |
| `WHATSAPP_PROVIDER` | `mock` or `meta` |
| `META_ACCESS_TOKEN` | Meta system-user or app access token |
| `META_PHONE_NUMBER_ID` | The sending phone number's ID |
| `META_WABA_ID` | WhatsApp Business Account ID (used for template sync) |
| `META_APP_ID`, `META_APP_SECRET` | App credentials; the secret verifies webhook signatures |
| `META_API_VERSION` | Graph API version — set it from Meta's current documentation |
| `META_WEBHOOK_VERIFY_TOKEN` | Value you choose, entered in both `.env` and the Meta webhook screen |
| `WHATSAPP_SEND_RATE_PER_SECOND` | Self-imposed send ceiling; keep at or below your WABA tier |
| `DEFAULT_COUNTRY_CODE` | ISO region used to read numbers typed without a `+` |

`.env` is git-ignored. **Never commit real credentials, and never put them in `.env.example`.**

## 8. Redis setup

**Linux / macOS**

```bash
# Debian/Ubuntu: sudo apt install redis-server
# macOS:         brew install redis && brew services start redis
redis-cli ping   # → PONG
```

**Windows** — Redis has no official Windows build. Pick one:

| Option | Notes |
|---|---|
| **Memurai** (recommended) | Redis-compatible Windows service. Install, then it listens on `localhost:6379`. |
| **WSL2** | `wsl --install`, then install `redis-server` inside the Linux distribution. |
| **Docker Desktop** | `docker run -d --name redis -p 6379:6379 redis:7-alpine` |

## 9. Celery setup

Celery reads its configuration from Django settings under the `CELERY_` prefix
(`config/celery.py`). Two queues are declared so a burst of outbound sends never delays
inbound webhook processing:

| Queue | Work |
|---|---|
| `whatsapp_send` | One task per recipient |
| `whatsapp_webhook` | Meta status callbacks |
| `default` | Everything else |

## 10. Running Django

```bash
python manage.py runserver
```

- Dashboard: <http://127.0.0.1:8000/>
- Sign-in: <http://127.0.0.1:8000/accounts/login/>
- Django admin: <http://127.0.0.1:8000/admin/>
- API documentation: <http://127.0.0.1:8000/api/docs/>

`manage.py` defaults to `config.settings.local`. Override with `DJANGO_SETTINGS_MODULE`.

## 11. Running the Celery worker

**Linux / macOS**

```bash
celery -A config worker -l info -Q default,whatsapp_send,whatsapp_webhook
```

**Windows** — Celery's default prefork pool does not support Windows. Use the solo pool
(or `gevent` for concurrency):

```bash
celery -A config worker -l info --pool=solo -Q default,whatsapp_send,whatsapp_webhook
```

Check the whole pipeline in one command:

```bash
python manage.py pipeline_status
```

```
Sending pipeline
  Provider                 mock
  Delivery                 simulated (nothing is sent)
  Dispatcher               registered
  Queue broker             reachable
  Rate limiter             RedisTokenBucket
  Send ceiling             10/second

Campaigns can be launched.
```

### Running a worker without Redis (development)

Celery's filesystem transport runs a real worker with no broker to install —
useful on Windows machines where Redis is awkward. It polls a directory, so it is
for development only; production uses Redis.

```bash
# in .env  (see .env.example for the commented block)
CELERY_BROKER_URL=filesystem://
CELERY_BROKER_TRANSPORT_OPTIONS={"data_folder_in": "C:/temp/wbm-queue", "data_folder_out": "C:/temp/wbm-queue", "processed_folder": "C:/temp/wbm-processed", "store_processed": true}
WHATSAPP_RATE_LIMIT_BACKEND=null
```

`pywin32` (already in `requirements/dev.txt`) provides the file locking this needs
on Windows.

## 12. Mock provider usage

With `WHATSAPP_PROVIDER=mock` the application is fully usable without any Meta credentials:
sends are simulated, synthetic provider message IDs are generated, and simulated status
callbacks can be emitted. The dashboard shows a prominent **Mock provider** banner so a
simulated send can never be mistaken for a real one.

Simulate adverse conditions during development:

```
MOCK_PROVIDER_FAILURE_RATE=0.1        # 10% of sends fail, exercising retry logic
MOCK_PROVIDER_LATENCY_SECONDS=0.25    # adds latency to each simulated call
```

The test suite forces the mock provider, so **tests never require real credentials**.

## 13. Meta API integration setup

*Implemented in Phase 7 against Meta's official documentation at implementation time. No
endpoint or payload is guessed.* Preparation on Meta's side:

1. Create a Meta app of type **Business** and add the **WhatsApp** product.
2. Connect a WhatsApp Business Account (WABA) and a phone number, and complete business
   verification.
3. Create a **System User** with access to the WABA and generate a long-lived access token
   with the `whatsapp_business_messaging` and `whatsapp_business_management` permissions.
4. Submit your message templates in **WhatsApp Manager** and wait for approval. Templates
   are created and approved on Meta's side only; this application mirrors them read-only
   and never attempts to bypass review.
5. Fill in `META_*` in `.env`, set `META_API_VERSION` to a Graph API version Meta currently
   supports, and switch `WHATSAPP_PROVIDER=meta`.

Business-initiated messages require an **approved template**. Free-form text is permitted
only inside Meta's customer-service window, and the UI enforces that rather than letting a
send fail at the API.

## 14. Webhook setup

*Implemented in Phase 7.*

1. Expose your development server publicly (for example with an HTTPS tunnel).
2. In the Meta app dashboard → WhatsApp → Configuration, set the callback URL to
   `https://<your-host>/api/whatsapp/webhook/` and the verify token to the value of
   `META_WEBHOOK_VERIFY_TOKEN`.
3. Subscribe to the `messages` field.

The endpoint answers Meta's `GET` verification handshake, validates the
`X-Hub-Signature-256` HMAC on every `POST` using `META_APP_SECRET`, deduplicates events,
returns `200` immediately, and processes the payload in a background task.

## 15. Testing

```bash
pytest                                   # full suite
pytest --cov                             # with coverage
pytest accounts -v                       # one app
pytest -m "not integration"              # skip anything that needs a live service
ruff check .                             # lint
```

The suite runs against `config.settings.test`, which forces `WHATSAPP_PROVIDER=mock`,
runs Celery tasks inline, and disables API throttles. It needs a reachable PostgreSQL
server but **no WhatsApp credentials and no Redis**.

Override the test database if needed:

```bash
DATABASE_URL=postgres://user:pass@localhost:5432/wbm_test pytest
```

## 16. Production deployment considerations

- **Settings**: `DJANGO_SETTINGS_MODULE=config.settings.production`, `DEBUG=False`,
  explicit `ALLOWED_HOSTS` and `CSRF_TRUSTED_ORIGINS`.
- **Secrets**: inject real environment variables (or a secrets manager). Do not ship `.env`.
  Rotate the Meta access token on a schedule.
- **TLS**: terminate HTTPS at the proxy; the production settings enable HSTS, secure
  cookies, and SSL redirect. Meta requires HTTPS for webhooks.
- **Processes**: `gunicorn config.wsgi:application` behind Nginx, plus separate Celery
  workers per queue so sends and webhooks scale independently.
- **Static files**: `python manage.py collectstatic` (WhiteNoise serves them with
  compression and hashing). Consider vendoring Bootstrap locally and adding Subresource
  Integrity instead of the CDN.
- **Database**: connection pooling, regular backups, and a read-consistent maintenance
  window before schema migrations.
- **Monitoring**: run `python manage.py check --deploy` in CI; ship logs to a central
  system (the redaction filter keeps credentials out of them); alert on Celery queue depth,
  failed-message rate, and webhook error rate.
- **Rate limits**: keep `WHATSAPP_SEND_RATE_PER_SECOND` at or below the throughput your
  WABA tier permits, and watch your quality rating in WhatsApp Manager.

## 17. Build status by phase

| Phase | Scope | Status |
|---|---|---|
| 1 | Architecture, data model, API design | ✅ Complete |
| 2 | Project scaffold, settings, PostgreSQL, authentication, base UI | ✅ Complete |
| 3 | Contacts, groups, CSV import | ✅ Complete |
| 4 | Templates, campaigns, message records | ✅ Complete |
| 5 | Celery, Redis, queue, mock provider | ✅ Complete |
| 6 | Dashboard, campaign monitoring, reports | ⏳ Next |
| 7 | Meta Cloud API integration, webhooks | ⏳ Planned |
| 8 | Testing, security review, optimization, documentation | ⏳ Planned |

Navigation items for modules that have not shipped yet are visible but disabled, and the
dashboard shows an em dash rather than a fabricated number for statistics it cannot yet
compute.

---

## 18. Consent model

Consent is the constraint the rest of the application is built around, so it is worth
stating precisely.

**A contact can be messaged only when `opted_in = True` **and** `status = active`.**
That rule lives in exactly one place — `Contact.objects.eligible()` — and campaign
audience resolution calls it with no override flag. There is no "send anyway" path.

| Situation | Result |
|---|---|
| CSV row with `opted_in` = `true`/`yes`/`y`/`1`/`consented`/`subscribed` | Imported, opted in, source `csv_import` |
| CSV row with any other value, a blank cell, or no `opted_in` column at all | Imported **opted out**, counted under "Not opted in" |
| CSV row for a number already on file | Reported as a duplicate and skipped (unless "update existing" is ticked) |
| Re-importing with `opted_in=false` for someone who already consented | Consent is **kept**; an import can grant consent but never revoke it |
| Operator ticks the consent box when creating a contact | Opted in, source `manual`, timestamped and audited |

Every consent change goes through `contacts.services.set_consent()`, which records a
source, a timestamp and an `AuditLog` entry. `Contact.opted_in` is read-only in both the
API serializer and the edit form precisely so no second, unaudited path can appear.

### API endpoints added in Phase 3

```
GET    /api/contacts/                        list, search, filter, paginate
POST   /api/contacts/                        create (opted_in defaults to false)
GET    /api/contacts/{id}/                   retrieve
PATCH  /api/contacts/{id}/                   update (cannot change consent)
DELETE /api/contacts/{id}/                   delete
POST   /api/contacts/{id}/opt-in/            record consent (audited)
POST   /api/contacts/{id}/opt-out/           withdraw consent (audited)
GET    /api/contacts/{id}/messages/          message history
POST   /api/contacts/import/                 upload a CSV, returns the report
GET    /api/contacts/stats/                  aggregate counts
GET    /api/contact-imports/                 import history and per-row errors
GET    /api/contact-groups/                  groups with member + eligible counts
POST   /api/contact-groups/{id}/add-members/
POST   /api/contact-groups/{id}/remove-members/
GET    /api/contact-groups/{id}/members/
```

Filters on `/api/contacts/`: `search`, `group`, `status`, `opted_in`, `eligible`,
`created_after`, `created_before`, plus `ordering` and `page_size`.


---

## 19. Campaigns and templates

### Template approval is Meta's, not ours

WhatsApp requires an **approved template** for any message a business initiates.
`MessageTemplate` mirrors that registry; it cannot submit a template for review or mark
one approved. Each template carries a `source`:

| Source | Meaning | Usable with `mock` | Usable with `meta` |
|---|---|---|---|
| `synced`, status `approved` | Meta approved it | ✅ | ✅ |
| `synced`, any other status | Meta has not approved it | ❌ | ❌ |
| `local` | Created here for development | ✅ | ❌ |

Local templates exist so the application is buildable before credentials arrive. They are
labelled as local throughout the UI, and `campaigns.services.validation_blockers()` refuses
them the moment `WHATSAPP_PROVIDER=meta`, with an explanation pointing at WhatsApp Manager.
Creating one is administrator-only and blocked entirely under the live provider.

### Template variables

Placeholders — `{{name}}` or Meta's positional `{{1}}` — are **derived from the template
text on save**, never hand-maintained, so a preview cannot disagree with what is sent.
A campaign maps each placeholder to either fixed text or a contact field drawn from an
explicit allow-list (`name`, `phone_number`, `email`, `country_code`). An operator-supplied
field name can never reach an arbitrary attribute.

Unresolved placeholders are left **visible** in the preview rather than blanked, so a
missing value shows up as `Hello {{name}}` instead of silently producing `Hello ,`.

### The campaign wizard

Five addressable URLs rather than a session-backed form, so a draft survives leaving the
page:

```
1. /campaigns/new/               name the campaign  → creates a DRAFT row
2. /campaigns/{id}/audience/     pick groups, see eligible vs excluded counts
3. /campaigns/{id}/message/      choose the template, map its variables
4. /campaigns/{id}/preview/      recipient count, rendered sample, blockers
5. /campaigns/{id}/confirm/      explicit checkbox, then launch
```

### Campaign state machine

```
DRAFT ──launch──▶ PROCESSING ──all terminal──▶ COMPLETED
  │                    │
  │                    ├──▶ PAUSED ──resume──▶ PROCESSING
  └──schedule─▶ SCHEDULED
(any non-terminal) ──▶ CANCELLED        COMPLETED / CANCELLED are final
```

Every move goes through `campaigns.services.transition()`. Anything not in the table raises
`InvalidStateTransition` (HTTP 409) — relaunching a completed campaign, which would message
everyone a second time, is simply not reachable.

### What launching does

```
validate  → blockers?      → 400 listing every problem at once
sender?   → none           → 503, campaign stays DRAFT, nothing materialized
resolve   → eligible only  → Contact.objects.eligible(), no override parameter
materialize                → one Message row per recipient, status PENDING
transition                 → PROCESSING, recipient count snapshotted
on_commit                  → hand to the dispatcher
```

`unique(campaign, contact)` makes materialization idempotent: a retried launch tops up
missing rows rather than duplicating the audience. Dispatch fires on
`transaction.on_commit` so a worker can never see a row that is not yet committed.

### The dispatcher seam

Phase 4 builds the plan; Phase 5 supplies the worker that drains it. Rather than wiring
Celery into the campaign services (which would make them untestable without a broker), the
sender registers itself:

```python
from campaigns import dispatch
dispatch.register_dispatcher(queue_campaign_messages)
```

With nothing registered, `launch_campaign()` raises `SendingUnavailable` **before** any
state change, the UI disables the launch button and explains why, and the dashboard reports
the sender as "not running". No campaign is ever left in PROCESSING with nothing able to
process it.

### Message status

`Message.status` moves `PENDING → QUEUED → SENDING → SENT → DELIVERED → READ`, with `FAILED`
terminal. `messaging.services.apply_status_update()` is:

* **idempotent** — `unique(message, status, provider_timestamp)` means a redelivered
  webhook is recorded once and changes nothing twice;
* **monotonic** — a late `sent` arriving after `read` is kept in the event log but never
  drags the message backwards.

`claim_for_sending()` claims a row with a conditional `UPDATE`. If two workers get the same
job, exactly one sees `rowcount == 1` and proceeds.

### API endpoints added in Phase 4

```
GET    /api/campaigns/                       list, search, filter
POST   /api/campaigns/                       create a draft
PUT    /api/campaigns/{id}/audience/         set audience, returns the breakdown
PUT    /api/campaigns/{id}/message/          set template + variable mapping
GET    /api/campaigns/{id}/preview/          counts, sample, blockers
POST   /api/campaigns/{id}/launch/           requires {"confirm": true}
POST   /api/campaigns/{id}/pause|resume|cancel/
GET    /api/campaigns/{id}/stats/            live status breakdown (polled)
GET    /api/campaigns/{id}/messages/         recipient-level status
GET    /api/templates/                       ?usable=true filters by provider
POST   /api/templates/                       local template (administrators, mock only)
POST   /api/templates/{id}/render/           safe preview with supplied values
POST   /api/templates/sync/                  pull from provider (Phase 7)
GET    /api/messages/  ·  /api/messages/{id}/  ·  /api/messages/stats/
```


---

## 20. Sending pipeline

### One task per recipient

```
launch  →  Message rows (PENDING)  →  transaction.on_commit
                                            │
                                            ▼
                             whatsapp.tasks.dispatch_campaign
                                            │  one .delay() per row
                                            ▼
                             whatsapp.tasks.send_message ×N
                                            │
              ┌─────────────────────────────┼─────────────────────────────┐
              ▼                             ▼                             ▼
        claim the row              token bucket (Redis)            provider.send_*
     conditional UPDATE          shared across all workers      Mock or Meta Cloud API
```

That granularity is what makes a 1,000-message campaign safe: one failure retries on
its own, a pause takes effect at the next message rather than mid-batch, and a
duplicated job is a no-op.

### The three correctness properties

| Property | How |
|---|---|
| **Idempotent** | Every send begins by claiming its row: `filter(pk=…, status__in=(PENDING, QUEUED)).update(status=SENDING)`. Two workers, one winner — the loser returns `already-handled`. |
| **Ordered** | Dispatch runs on `transaction.on_commit`, so a worker can never see a row that is not yet committed. |
| **Bounded** | A Redis token bucket (one Lua script: refill and take are atomic) throttles every worker to a shared ceiling. Per-process limiting would let N workers send N times the rate. |

### Retries

Transient failures (rate limit, timeout, provider 5xx) retry with **exponential backoff
plus jitter**, capped at an hour, up to `WHATSAPP_MAX_RETRIES`. The jitter matters: without
it, a thousand messages that failed together would retry at the same instant and fail
together again. A provider's `Retry-After` always wins over our own backoff.

Permanent failures (not a WhatsApp user, template rejected) are recorded immediately —
retrying them would just burn quota. The last error is also copied onto the contact so an
operator can see why a number keeps failing.

**This is not rate-limit evasion.** The bucket throttles *our own* sending to stay inside
the WABA's limits; when the provider pushes back we wait longer, never harder.

### Preflight: never strand a campaign

A registered dispatcher is not a reachable queue — the Celery sender registers at startup
whether or not Redis is running. `campaigns.dispatch.preflight()` asks the sender to prove
it can accept work (the Celery one pings the broker) **before** any state changes. If it
cannot, the launch raises `SendingUnavailable`, the campaign stays `DRAFT`, and no message
rows are created. Without this, a campaign would sit in `PROCESSING` with every message
stuck at `PENDING` and nothing able to move it.

### Mock provider

`WHATSAPP_PROVIDER=mock` simulates the whole path. It generates synthetic `wamid`s and,
at a configurable rate, the failure modes that actually matter when integrating:

| Simulated failure | Retryable |
|---|---|
| `mock_rate_limited` (with a `Retry-After`) | yes |
| `mock_upstream_error` | yes |
| `mock_undeliverable` | no |
| `mock_invalid_number` | no |

Being able to exercise the retry path locally is the point — error handling that has never
run is error handling that does not work.

With `MOCK_PROVIDER_SIMULATE_CALLBACKS=True` a follow-up task stands in for the provider's
delivery webhooks so the full lifecycle can be demonstrated. Those events are recorded with
`source=simulated`, **never** `webhook`, so a simulated delivery can never be mistaken for
a real one.

### Scheduled campaigns

Celery beat runs `whatsapp.tasks.run_due_campaigns` every minute. It launches campaigns
whose `scheduled_at` has passed through the same `launch_campaign()` as a manual send, so
validation, consent filtering and auditing are identical. A scheduled campaign that fails
validation is marked `FAILED` with the reason rather than retrying silently every minute.

```bash
celery -A config beat -l info
```

### Queues

| Queue | Work |
|---|---|
| `whatsapp_send` | per-recipient sends, campaign dispatch, simulated callbacks |
| `whatsapp_webhook` | inbound Meta status callbacks (Phase 7) |
| `default` | scheduled-campaign sweep |

Separate queues so a burst of outbound sends never delays inbound status processing.
