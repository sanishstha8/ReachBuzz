# ReachBuzz — Business Messaging & Automation Platform

[![CI](https://github.com/sanishstha8/ReachBuzz/actions/workflows/ci.yml/badge.svg)](https://github.com/sanishstha8/ReachBuzz/actions/workflows/ci.yml)

A WhatsApp Business messaging and campaign management platform for businesses to
send, manage, and track customer communications.

Built on Django, sending exclusively through the **official Meta WhatsApp
Business Platform Cloud API**.

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
21. [Dashboard, monitoring and reports](#21-dashboard-monitoring-and-reports)
22. [Meta Cloud API integration](#22-meta-cloud-api-integration)
23. [Security posture](#23-security-posture)
24. [The landing page](#24-the-landing-page)

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
| Landing page | Public marketing page at `/` — features, how it works, pricing, FAQ and contact |
| Authentication | Email sign-in, session security, role-based authorization (Administrator / Operator / Viewer), CSRF protection, sign-in audit trail |
| Contacts | CRUD, search, filter, pagination, E.164 normalization, duplicate detection, consent tracking, per-contact message history |
| CSV import | Validated upload, per-row error reporting, duplicate detection, explicit-consent-only opt-in |
| Groups | Many-to-many contact lists used as campaign audiences |
| Campaigns | Wizard (name → audience → template → preview → confirm), recipient count, draft/schedule/launch/pause/cancel |
| Templates | Mirror of the WABA's approved templates, variable substitution, safe preview |
| Sending | Celery + Redis, one job per recipient, retries with backoff, self-imposed rate ceiling, idempotent dispatch |
| Webhooks | Signed Meta webhook endpoint, idempotent event handling, background processing |
| Monitoring | Live campaign progress, status breakdown, grouped failure reasons, failed-message detail |
| Dashboard | Activity chart, live "sending now" panel, recent failures, pipeline health |
| Reports | Date-ranged overview, per-campaign performance, failure analysis, consent register, streamed CSV exports |
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
| `dashboard` | Aggregate statistics, monitoring pages, reporting API and CSV exports |
| `pages` | The public landing page — the only unauthenticated HTML |

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

## 4a. Contributing and branch workflow

Branch model, commit conventions, the pre-commit hooks, and the rules this
project does not bend (consent, credentials, template approval, rate limiting)
are in [CONTRIBUTING.md](CONTRIBUTING.md).

```bash
pip install pre-commit && pre-commit install
```

---

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
psql -U postgres -c "CREATE DATABASE rebuzz;"
psql -U postgres -c "CREATE USER rebuzz WITH PASSWORD 'change-me';"
psql -U postgres -c "ALTER ROLE rebuzz SET client_encoding TO 'utf8';"
psql -U postgres -c "ALTER ROLE rebuzz SET default_transaction_isolation TO 'read committed';"
psql -U postgres -c "ALTER ROLE rebuzz SET timezone TO 'UTC';"
psql -U postgres -c "GRANT ALL PRIVILEGES ON DATABASE rebuzz TO rebuzz;"
psql -U postgres -d rebuzz -c "GRANT ALL ON SCHEMA public TO rebuzz;"
```

Point `DATABASE_URL` at it:

```
DATABASE_URL=postgres://rebuzz:change-me@localhost:5432/rebuzz
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

- Landing page: <http://127.0.0.1:8000/> (public)
- Dashboard: <http://127.0.0.1:8000/dashboard/>
- Sign-in: <http://127.0.0.1:8000/accounts/login/>
- Django admin: <http://127.0.0.1:8000/admin/>
- API documentation: <http://127.0.0.1:8000/api/docs/>

`manage.py` defaults to `config.settings.local`. Override with `DJANGO_SETTINGS_MODULE`.

### Demonstration data

An empty database is a poor way to judge the dashboard and the reports page: an
honest empty state is exactly what they are built to show, so there is nothing to look
at. This fills in a few weeks of plausible sending history.

```bash
python manage.py seed_demo                                  # 120 contacts, 6 campaigns, 45 days
python manage.py seed_demo --contacts 300 --days 90         # a bigger sample
python manage.py seed_demo --clear                          # remove exactly what it created
```

It goes through the real services — `create_contact`, `set_consent`,
`set_audience`, `materialize_messages`, `transition` — so consent is audited and the
state machine is respected, and the audience it produces exercises the cases that
matter: contacts who consented but are not active, and so still may not be messaged.
The one thing it does directly is backdate timestamps at the end, because there is no
honest way to ask the system to have sent something last Tuesday.

It **refuses to run** unless `WHATSAPP_PROVIDER=mock` and `DEBUG=True`. These are
fabricated people, and this system sends real messages. The last campaign is left as a
draft so there is something to launch by hand and watch move through the real pipeline.

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

Implemented against Meta's official documentation. Preparation on Meta's side:

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

1. Expose your development server publicly (for example with an HTTPS tunnel).
2. In the Meta app dashboard → WhatsApp → Configuration, set the callback URL to
   `https://<your-host>/api/whatsapp/webhook/` and the verify token to the value of
   `META_WEBHOOK_VERIFY_TOKEN`.
3. Subscribe to the `messages` field.

`GET` answers Meta's verification handshake, echoing `hub.challenge` verbatim as plain
text when `hub.verify_token` matches. `POST` validates the `X-Hub-Signature-256` HMAC over
the **raw request body** using `META_APP_SECRET`, stores the payload, returns `200`, and
processes it in a background task on the `whatsapp_webhook` queue.

This is the only unauthenticated, CSRF-exempt route in the application, and everything
about it follows from that:

- **The signature is the authentication.** Nothing is parsed, stored or queued until the
  HMAC verifies. An unverified body is not persisted at all — the endpoint is public, so
  anything that writes on unverified input is a way for a stranger to fill the database.
- **The hash is over the raw bytes.** Re-serialising the parsed JSON changes whitespace
  and key order and would never match, which is why the view verifies before it parses.
- **It answers 200 fast.** Meta retries a non-200 with decreasing frequency for up to
  seven days, so an endpoint that does its work inline turns one slow query into a week
  of duplicate deliveries.
- **200 is not a claim the payload was understood** — only that it arrived intact and is
  stored. Processing failures are recorded on the event, because asking Meta to redeliver
  would not fix a bug on our side. A periodic sweep retries anything left unprocessed.

Redelivery is safe rather than merely tolerated: `apply_status_update()` is idempotent (a
repeated event is recorded once) and monotonic (a late `sent` arriving after `read` is
logged but never drags the message backwards). Meta makes no ordering promise, so both
properties are load-bearing.

Raw payloads are kept as `WebhookEvent` rows and visible in the Django admin, read-only,
with a reprocess action. The event is evidence; our reading of it is not.

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

Three project-wide fixtures in `conftest.py` keep that true rather than merely intended:

| Fixture | What it enforces |
|---|---|
| `http` | Intercepts `requests`; an unregistered outbound call is an error, not a phone call to Meta |
| `_clean_cache` | Empties the cache between tests, so the sign-in throttle and broker probe cannot leak across them |
| `_no_dispatcher_by_default` | No message sender is registered unless a test asks, keeping the "sending unavailable" path honest |

Some suites are about a property rather than a feature, and are worth knowing about
before changing code near them:

- **`core/tests/test_query_counts.py`** asserts that no page or endpoint issues more
  queries as the data grows. It compares two dataset sizes rather than pinning exact
  counts — an N+1 is invisible locally and a page-load per row in production, and a test
  that pins numbers gets bumped without being read.
- **`core/tests/test_settings_documentation.py`** fails if a setting is read but absent
  from `.env.example`, or advertised there and read by nothing.
- **`core/tests/test_seed_demo.py`** covers the seeder's refusal to run outside
  development, which is the only thing standing between fabricated contacts and a system
  holding live credentials.

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
| 6 | Dashboard, campaign monitoring, reports | ✅ Complete |
| 7 | Meta Cloud API integration, webhooks | ✅ Complete |
| 8 | Testing, security review, optimization, documentation | ✅ Complete |

Navigation items for modules that have not shipped yet are visible but disabled, and the
dashboard shows an em dash rather than a fabricated number for statistics it cannot yet
compute.

All eight phases are complete. The one thing no amount of testing substitutes for is a
send through a real WhatsApp Business Account — see the verification checklist at the end
of section 22.

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
POST   /api/templates/sync/                  pull from provider (administrators only)
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
| `whatsapp_webhook` | inbound Meta status callbacks and the pending-event sweep |
| `default` | scheduled-campaign sweep |

Separate queues so a burst of outbound sends never delays inbound status processing.

---

## 21. Dashboard, monitoring and reports

Three pages answer three different questions, which is why they are three pages.

| Page | Question | Scope |
|---|---|---|
| **Dashboard** (`/`) | What is happening right now? | Live |
| **Campaign detail** (`/campaigns/{id}/`) | How is this one send going? | One campaign |
| **Reports** (`/reports/`) | What happened over a period I choose? | A date range |

Every figure on all three comes from `dashboard/services.py`, and the reporting API
returns the same objects, so the HTML and the JSON cannot drift apart.

### Two conventions worth knowing before reading a number

**The status buckets are disjoint.** `pending`, `sent`, `delivered`, `read` and `failed`
partition the messages in a period — `pending` gathers the three in-flight statuses, and
the five sum to the total with no double counting. Where a cumulative figure is meant
instead, it is named `reached` (everything the provider accepted, whatever happened next).

**A message belongs to the day it was created**, which is the day its campaign was
launched. Grouping by `sent_at` would move rows between days as retries land, and a report
whose past changes underneath the reader is worse than one with a stated convention.

One consequence worth stating plainly: **delivery rate is measured against `reached`, not
against every message.** A message still queued has not failed to arrive — it has not been
sent yet — so dividing by everything would let a large backlog drag a healthy campaign's
delivery rate towards zero.

### Live monitoring

The dashboard's "Sending now" panel lists every campaign in `processing` or `paused` with
its progress, and polls `/api/monitor/active-campaigns/` every ten seconds. Paused
campaigns stay in the panel deliberately: an operator who has just stopped a send should
not watch it vanish from the page they stopped it on. When the *set* of active campaigns
changes the page reloads rather than being patched, because the panel is then the wrong
shape. The campaign detail page polls its own stats endpoint on the same principle.

### Charts

Charts are inline SVG with no charting library and no build step. All geometry is computed
in `dashboard/charts.py` and handed to the template as plain data, which is what makes it
testable — a bar drawn at the wrong height still looks like a bar.

The message lifecycle is an ordered sequence, so it takes a single-hue **ordinal ramp** in
the product teal (pending is the lightest step, read the darkest) rather than five
unrelated hues: the reader sees the order in the colour. Failure is not a stage of that
sequence but a state, so it takes the reserved critical red and sits apart from the ramp.
The ramp was validated against the white card surface — monotone lightness, adjacent ΔL
≥ 0.06, light end 2.23:1 — and the one place the ramp meets the status colour clears the
colour-vision separation floor at ΔE 18.0.

Because the lightest step is below 3:1 against the card, every chart ships a **"Show the
numbers" table**. That is a requirement, not a nicety: no value is ever available only by
looking at a colour, or only by hovering. Days with no activity are drawn as gaps rather
than closed up, and a period with nothing in it says so instead of drawing a flat line at
zero.

### CSV exports

| Report | Contents | Scope |
|---|---|---|
| Campaign performance | one row per campaign launched, with delivery and failure rates | period |
| Message detail | one row per recipient message, with its status timeline | period |
| Failure reasons | distinct provider errors and how often each occurred | period |
| Consent register | every contact and the recorded basis for messaging them | **current state** |
| Campaign recipients | one campaign's recipients, from its monitoring page | one campaign |

The consent register ignores the selected period on purpose. Consent is a state, not an
event: "who may we message, and how do we know" is only ever answerable as of now.

Four things are true of every export:

- **It streams.** Rows are generated lazily, so a 20,000-recipient file costs one row of
  memory rather than twenty thousand.
- **It is escaped against spreadsheet formula injection.** Contact names arrive from CSV
  imports and web forms, and Excel and Sheets execute a cell beginning `=`, `+`, `-` or
  `@`. Text cells are prefixed so they stay text — including phone numbers, which begin
  with `+`.
- **It contains no credential.** Nothing in `dashboard/reports.py` reads a token, and
  nothing should be added that does.
- **It is audited.** An export puts personal data on someone's laptop, so it is recorded
  as `report_exported` in the audit log with the report, the period and the filename.

Times in a file are written in the project's configured timezone, matching what the
reports page displays, so a figure on screen and a row in the file cannot disagree.

### API endpoints added in Phase 6

```
GET /api/reports/overview/            headline figures for a period
GET /api/reports/activity/            message outcomes per day (quiet days included)
GET /api/reports/campaigns/           campaigns launched in the period, with their rates
GET /api/reports/failures/            distinct provider errors, most frequent first
GET /api/reports/consent/             current consent state (takes no period)
GET /api/monitor/active-campaigns/    campaigns sending right now
```

Every one takes `?days=N`, or `?start=YYYY-MM-DD&end=YYYY-MM-DD`; the default is the last
30 days and the cap is 366. A malformed value falls back to the default rather than
raising — a report page must not fail because someone edited the address bar.

All six are read-only. A report is derived from message and consent state, and the only
way to change one is to change that state through the endpoints that own it.

---

## 22. Meta Cloud API integration

Selecting the provider is one environment variable — `WHATSAPP_PROVIDER=meta` — and no
application code changes. What follows is what that switch turns on.

### Retryability is decided by Meta's error code, not the HTTP status

Meta's own guidance is to build error handling around the `code` and `details` properties
rather than message titles or HTTP status codes, and the two genuinely disagree: a
throughput limit and a permanently dead number both arrive as an HTTP 400. One frozen set
in `whatsapp/services/meta_cloud_api.py` is the whole policy.

| Treated as | Codes |
|---|---|
| Retryable | `4`, `80007`, `130429`, `131000`, `131016`, `131056`, `133004`, `133016`, `2494100`, plus timeouts, connection failures and a 5xx with no code |
| Permanent | everything else — `100`, `190`, `368`, `131026`, `131047`, `131051`, `132000`, `133010`, … |

Two codes are deliberately **not** retried despite looking transient. Meta documents that
retrying `131049` and `131048` "artificially lowers your perceived delivery rate, as the
same per-user limit may still be in effect" — so a retry costs the metric and changes
nothing. Backing off is also the only reading consistent with the rule that the rate
limiter throttles us and never pushes against a limit.

A `200` carrying no `wamid` is treated as a *retryable failure*, not a success: without an
id no webhook can ever be matched back to that message, so recording it as sent would
strand it permanently at "sent".

### Template sync mirrors Meta and decides nothing

`POST /api/templates/sync/` (administrators only) pulls
`GET /{waba-id}/message_templates`, following Meta's cursor paging, and writes what Meta
reports. This is the only place in the application that writes an approval status, and all
it may do is copy one. Nothing submits a template for review, and nothing marks one
approved on Meta's behalf.

An approval state we do not recognise becomes `disabled`, never anything usable — Meta
adds states over time, and the safe reading of an unfamiliar one is "do not send with
this". A local development template that collides with a real one is converted to the
synced version rather than skipped: once Meta has a template by that name, Meta's is the
truth, and leaving a local stub shadowing it would let someone send a draft believing it
had been approved.

### Inbound STOP

An inbound message whose entire text is a stop keyword (`stop`, `unsubscribe`, `cancel`,
`quit`, `end`, `opt out`, …) opts the sender out through
`contacts.services.set_consent()`, so the withdrawal is timestamped, sourced
`inbound_stop` and audited exactly like one an operator makes by hand.

The match is against the **whole message**, never a search inside it. "Please don't stop
sending these" must not opt somebody out, and a false positive here silently ends a
conversation the customer wanted.

**"START" does not opt anyone back in.** Consent is never inferred, and a keyword is a
weaker basis than this system is willing to record as consent. Opting back in stays a
deliberate act by an operator, with a source and an audit entry behind it.

### Campaign completion versus message delivery

These are two lifecycles and it is worth not confusing them. A **campaign** is complete
once nothing is still in flight — our sending work is finished, and every message has been
handed to Meta. A **message** keeps moving afterwards, as Meta reports what happened to it.
That is why delivery rate on the reports page is measured against messages the provider
accepted rather than against campaign status.

### Testing it without credentials

The whole integration is covered with stubbed HTTP, and the suite blocks the network
outright: an autouse fixture in `conftest.py` intercepts `requests` and makes an
unregistered call an error rather than a phone call to Meta. That is what keeps the
project's rule — the suite passes with no credentials and no network — true now that a
provider makes real HTTP calls, instead of merely being true by accident.

```python
def test_send(http):
    http.add(responses.POST, MESSAGES_URL, json={...}, status=200)
```

### Verifying against the real API

Everything above is exercised against recorded responses, which proves the code does what
Meta's documentation says — not that Meta agrees. One command closes that gap:

```bash
python manage.py verify_live                        # read-only: nothing leaves the machine
python manage.py verify_live --sync                 # ...and mirror the templates locally
python manage.py verify_live --to +9779800000000    # ...and send one real message
```

| Check | What a failure tells you |
|---|---|
| Provider is `meta` | You are pointed at the mock and would only be testing yourself |
| Credentials | Which `META_*` setting is missing — never what any of them contain |
| Sending pipeline | No worker, or an unreachable broker: a campaign would strand at `pending` |
| Templates | The access token is rejected, or nothing on the WABA is approved |
| Send | With `--to`, a real one-recipient campaign, watched until the webhooks land |

The send goes through the **ordinary campaign path** — the same `launch_campaign` an
operator's click uses — so what it verifies is what actually runs, rather than a special
case that only exists in the checker. Reaching `sent` proves the send path; reaching
`delivered` proves the webhook path too, which is the half that cannot be checked any
other way because it depends on Meta being able to reach *you*.

**The recipient must be a contact who has consented.** It is resolved through
`Contact.objects.eligible()` with no override, so verifying with your own number means
adding yourself as a contact and recording consent first — which is a true statement and
an audited one. There is deliberately no flag to skip this: a command that took a phone
number on the command line and messaged it regardless would be a hole in the one rule the
whole system is built on.

Two things the command cannot check for you, so check them by hand once:

1. Reply `STOP` from that number, and confirm the contact shows as opted out with source
   "Recipient replied STOP" and an audit entry against it.
2. Read the worker log and confirm no credential appears in it.

---

## 23. Security posture

What the application does to protect itself, and — more usefully — the reasoning, so a
future change can tell which parts are load-bearing.

### Authentication

Sign-in is by email with Django's session framework; the session key rotates on login.
Sessions expire after `SESSION_COOKIE_AGE` of inactivity, cookies are `HttpOnly` and
`SameSite=Lax`, and `Secure` in production. Logout is POST-only, so a prefetched link
cannot sign someone out.

**Both login doors are rate limited.** The REST login has a DRF throttle
(`THROTTLE_LOGIN`); the HTML form has its own counter (`LOGIN_ATTEMPT_LIMIT`). Having
only the first meant the limit could be walked around by posting where a browser posts.
Attempts are counted **per client address, never per account**: locking an account after
N failures would let anyone who knows an operator's email address lock them out, trading
a brute-force risk for a denial-of-service one. The block expires on its own.

Failed sign-ins are audited with the attempted identifier and never the password. The
wrong-password and unknown-email responses are identical, and so is the lockout message —
a message that varied would turn the throttle into a user-enumeration oracle.

### Authorization

Every HTML view carries an auth mixin except the sign-in and sign-out pages; every API
view names an explicit permission class. Views ask for a *capability*
(`can_manage_contacts`, `can_launch_campaigns`) rather than comparing role strings, so the
matrix lives in one place — `accounts.models.User`.

Two actions are deliberately narrower than the rest. Launching a campaign is its own
capability, because it is the only irreversible action in the system. Template sync is
administrator-only, because one call rewrites the approval status of every template.

### The webhook endpoint

The only unauthenticated, CSRF-exempt route. Its signature check is the authentication,
and nothing is parsed, stored or queued until the HMAC over the raw body verifies — see
section 14 for why each of those words matters.

Both HMAC comparisons — the webhook signature and the verification token — use
`compare_digest` **on bytes**. The string form raises `TypeError` on non-ASCII input, and
both values come from a stranger; comparing as bytes is what keeps the answer a 403
instead of a 500.

### Data handling

- **No uploaded file is ever written to disk.** CSV imports are validated for extension,
  size and encoding, then parsed in memory; only the filename is stored, as text. There is
  no `FileField` in the project and nothing is served from `MEDIA_ROOT` in production.
- **Exports are escaped against spreadsheet formula injection** and audited. See
  section 21.
- **No credential is logged, rendered, or returned.** `core.logging_filters` redacts as a
  last line of defence, and tests assert that no token appears in any page, API response
  or export.
- **Queries are parameterised throughout.** There is no raw SQL, no `eval`, no `pickle`,
  and no template autoescape bypass on user-controlled data anywhere in the project.

### What has *not* been done

- **No live penetration test**, and no run against a real WhatsApp Business Account.
- **Any active user can export the full consent register**, including phone numbers.
  That matches the UI — a viewer can already page through every contact — and every export
  is audited, but bulk export and paged reading are different in practice. If your threat
  model cares, narrow `ReportDownloadView` to `CapabilityRequiredMixin`.
- **`LoginView.redirect_authenticated_user` is on**, which Django notes lets another site
  detect whether a visitor is signed in here by requesting an image URL. Harmless for a
  private internal tool; worth knowing if this is ever exposed more widely.

---

## 24. The landing page

A public marketing page lives at `/`; the operator dashboard moved to
`/dashboard/`. Nothing else changed — every internal link resolves by URL name, so
the move touched no other template.

It is the only unauthenticated HTML in the project, and it is held to the same
standard as the rest of the application: **it may not claim a capability the system
does not have.**

- **Every link resolves.** The reference design linked to a blog, a help centre and a
  refund policy. None of those exist here, so none of them are linked. A test walks
  every anchor on the page and fails on a 404.
- **No price is invented.** `PRICING_TIERS` in `pages/views.py` carries `price = None`
  until someone sets a real figure, and a tier without one renders "Pricing on request".
  Set the `price` field to publish real numbers; the layout is already built for them.
- **The primary call to action is "Request access", not "Sign up".** There is no
  self-service registration in this system — accounts are created by an administrator,
  and the sign-in page says "Authorized operators only". A "Get started free" button
  would be promising a flow that does not exist.
- **The dashboard preview is labelled as sample data.** It is a mock built in markup
  rather than a screenshot, so it stays sharp and cannot go stale against a redesign.
- **`SUPPORT_EMAIL` gates the contact card.** With no address configured the card is
  omitted entirely rather than showing configuration advice to a visitor.

Copy lives as data at the top of `pages/views.py` — features, steps, tiers and FAQ are
plain dataclasses, so editing the page is editing a list, not hunting through markup.
