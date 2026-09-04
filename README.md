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
25. [Organizations and tenant isolation](#25-organizations-and-tenant-isolation)
26. [Sign-up, email confirmation and password reset](#26-sign-up-email-confirmation-and-password-reset)
27. [Plans, subscriptions and usage](#27-plans-subscriptions-and-usage)
28. [Invoices and payments](#28-invoices-and-payments)
29. [Per-organization messaging credentials](#29-per-organization-messaging-credentials)
30. [The billing area](#30-the-billing-area)
31. [The backoffice](#31-the-backoffice)
32. [Channels and SMS](#32-channels-and-sms)

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

### SaaS upgrade

Turning the finished single-tenant product into something several businesses can
sign up for and use. Each stage lands on its own branch and is verified against the
full suite before the next one starts.

| Stage | Scope | Status |
|---|---|---|
| 1 | Organizations, membership, tenant isolation | ✅ Complete — [§25](#25-organizations-and-tenant-isolation) |
| 2 | Registration, email confirmation, password reset | ✅ Complete — [§26](#26-sign-up-email-confirmation-and-password-reset) |
| 3 | Plans, subscriptions, usage metering | ✅ Complete — [§27](#27-plans-subscriptions-and-usage) |
| 4 | Payments and invoices | ✅ Complete — [§28](#28-invoices-and-payments) |
| 5 | Per-organization messaging credentials | ✅ Complete — [§29](#29-per-organization-messaging-credentials) |
| 6 | Customer billing dashboard | ✅ Complete — [§30](#30-the-billing-area) |
| 7 | Platform admin dashboard | ✅ Complete — [§31](#31-the-backoffice) |
| 8 | Versioned API, notifications, hardening | Not started |
| 9 | SMS channel | ✅ Complete — [§32](#32-channels-and-sms) |

Stage 5 is the one that changes how sending works: credentials are a single set in
the environment today, and every organization currently shares them.

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

**So is every other door a stranger can push.** Stage 2 opened three endpoints that need
no account, and all three do work on request:

| Endpoint | Limit | Why it needs one |
|---|---|---|
| Registration | `SIGNUP_LIMIT` (5/hour) | Writes rows *and* mails an address the caller chose — a way to fill the database and a way to point our mail server at somebody who never asked for it |
| Password reset | `OUTBOUND_EMAIL_LIMIT` (5/15min) | Mails any registered address on demand |
| Resend confirmation | `OUTBOUND_EMAIL_LIMIT` | Same, for the signed-in user's own address |

Two details carried over from the sign-in throttle, for the same reasons. The reset
counter advances on **every** submission, not only the ones that find an account — a
counter that moved only for real addresses would make the block itself the enumeration
oracle the identical response exists to prevent. And registration counts accounts
*created*, not forms *submitted*: a rejected form writes nothing and sends nothing, so it
costs nothing. All three share one `RateLimit` class in `accounts/throttling.py`, and all
three are set to 0 to disable.

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
- **No price is invented.** Tiers are `Plan` rows since Stage 3, and a plan carries
  `price = None` until someone sets a real figure — a tier without one renders
  "Pricing on request". Set the price in the admin to publish real numbers; the layout
  is already built for them.
- **The primary call to action is "Get started", and it now leads somewhere.** It read
  "Request access" until Stage 2, because self-service registration did not exist and the
  page may not promise a flow that does not. It points at `accounts:register` now.
- **The dashboard preview is labelled as sample data.** It is a mock built in markup
  rather than a screenshot, so it stays sharp and cannot go stale against a redesign.
- **`SUPPORT_EMAIL` gates the contact card.** With no address configured the card is
  omitted entirely rather than showing configuration advice to a visitor.

Copy lives as data at the top of `pages/views.py` — features, steps and FAQ are plain
dataclasses, so editing the page is editing a list, not hunting through markup. Tiers
are the exception: they come from the plan catalogue, so what the page advertises is
what the system enforces.

---

## 25. Organizations and tenant isolation

Until Stage 1 this was a single-tenant application: one business, one set of
contacts, no notion of who owned a row. A grep for `organization` across 24,000
lines returned nothing. Everything below is a retrofit, and the constraint that
shaped it is that a missing tenant filter is invisible — it does not raise, does
not slow anything down and does not look wrong in review. It just returns
somebody else's contacts.

### The two models

`Organization` is a customer business. `OrganizationMember` is a user's seat in
one, with its own role (`OWNER`, `ADMIN`, `MEMBER`).

Membership is a separate row rather than a foreign key on `User` because an
agency running campaigns for several clients is the obvious next case, and
retrofitting that later would mean moving every scoped query a second time.

**Organization roles are deliberately separate from `User.role`.** One says what
you may do inside a business; the other says what you are to the platform.
Conflating them is how a customer's own administrator ends up seeing another
customer's data.

### How isolation is enforced

Not by remembering to filter. Every customer-owned model inherits
`OrganizationOwnedModel`, and every one of their querysets inherits
`OrganizationScopedQuerySet`, which adds:

```python
Campaign.objects.get(pk=pk)                        # any customer's campaign
Campaign.objects.for_organization(org).get(pk=pk)  # only this one's
```

`for_organization(None)` returns `.none()`, not everything. An unresolved
organization is a bug either way, and the safe failure is an empty page.

Views never call `Model.objects` directly. `ActiveUserRequiredMixin` resolves
`self.organization` in `dispatch()` and offers two paths:

| | |
|---|---|
| `self.scoped(Model)` | for views that build their own queryset — the filter is visible in the method you are reading |
| `get_queryset()` | a safety net for views that only declare `model = X` and never override it |

The safety net is not theoretical. A parametrised test that walks every detail,
update and delete route as the wrong tenant found **thirteen views** in exactly
that shape — `contacts:group-detail` among them, returning another customer's
group to anyone who knew its id. DRF has the same pair in
`OrganizationScopedViewSetMixin` (which also stamps `perform_create` from the
request, never the payload) and `OrganizationAwareMixin` for plain `APIView`s —
the CSV import and the statistics endpoints, where an aggregate that counts
every tenant's rows leaks just as surely as a list that returns them.

Detail views raise **404, not 403**, for another tenant's id. Telling somebody
an object exists but is not theirs confirms that it exists.

### The three-step migration

A single migration adding a non-null foreign key to a populated table cannot
work, so:

1. `organizations/0001_initial` — the models, plus the column nullable on all six owned tables.
2. `organizations/0002_backfill_default_organization` — creates "Default Organization", seats every existing user, fills every row.
3. `contacts/0003`, `campaigns/0003`, `whatsapp/0004`, `messaging/0004` — tighten to non-null.

The backfill is idempotent, so re-running it against a restored snapshot is
safe, and it reverses by *detaching* rows rather than deleting the organization
— deleting would cascade to every contact whose ownership the reversal was
meant to be undoing.

Step 3 is hand-written. `makemigrations` prompts interactively for a default
when a nullable field becomes non-null, and the generated `related_name` must
be the literal `"%(class)ss"`, unresolved, to match what Django emits.

### Adding a model later

Inherit `OrganizationOwnedModel`, and make its queryset inherit
`OrganizationScopedQuerySet`. A parametrised test asserts that every owned model
has both, so a model added without them fails there rather than in production.

---

## 26. Sign-up, email confirmation and password reset

Stage 2. Before it, accounts were created by an administrator or `createsuperuser`;
the landing page's call to action had to read "Request access" because there was
nothing to send anyone to.

### Registering creates three things atomically

An account, a business, and that account's ownership of it. A user with no
organization can sign in and see nothing; an organization with no owner cannot be
administered at all. Neither half is a state worth being able to reach, so
`accounts.registration.register()` is `@transaction.atomic` — all three, or the
address stays free to try again.

The new owner is signed in immediately. Making somebody sign in again seconds
after choosing a password is friction for nothing.

### Confirmation uses signed tokens, not a table

`EmailVerificationTokenGenerator` subclasses Django's `PasswordResetTokenGenerator`.
That machinery already gives one-time, time-limited links tied to the user's
current state; a `VerificationToken` model would be a second implementation of it
with its own expiry bugs and its own rows to clean up.

Its hash includes `email_verified`, which has two consequences worth stating:
the link stops working the moment it is used, and a password-reset token cannot
confirm an address (nor a verification token reset a password).

`verify()` returns `None` for every failure — malformed id, unknown user, expired
token, already used — and the page says the same sentence for all of them.
Distinguishing "no such account" from "bad token" tells a stranger which
addresses are registered.

### What being unconfirmed actually blocks

**Sending, and only sending.** An unverified user signs in, browses, imports
contacts, and builds a campaign. `launch_campaign()` is where it stops:

```python
if user is not None and not getattr(user, "email_verified", True):
    raise ValidationFailed("Confirm your email address before sending a campaign.")
```

Locking somebody out of an empty dashboard helps nobody, and would mean a mail
outage locks out every new customer. But sending to real people from an address
that may not exist means the bounce notices, the failure reports and the replies
all go nowhere. A banner in `base.html` shows until the address is confirmed, with
a POST-only resend — a GET that sends mail can be fired by a link prefetcher or a
scanner.

### Password reset

Django's four views with this project's templates. An unknown address gets the
same page and no email, for the same enumeration reason as above.

### All three doors are rate limited

Registration, password reset and resend all do work for anyone who asks, so all
three are capped per client address — see the table in
[§23](#23-security-posture). They extend the sign-in form's existing throttle
rather than adding a second mechanism.

### What is deliberately not here

No email change flow, no invitations, no second organization per user. Stage 2 is
the front door only; team management is a later stage.

---

## 27. Plans, subscriptions and usage

Stage 3. The product could be signed up for after Stage 2, but every account got
everything, forever. This is what a customer is entitled to and how it is counted.

### Three models

| Model | What it is |
|---|---|
| `Plan` | A tier: what it costs, and what it permits. Platform-level, not customer-owned — every customer sees one catalogue |
| `Subscription` | One organization's place on one plan, for one period. `OneToOne`, so two rows cannot disagree about a customer's limits |
| `UsageSnapshot` | A closed period's totals, frozen. What an invoice will be written against |

### Limits: empty means unlimited, zero means none

They look alike in a database row and behave in opposite ways. A nullable integer
is the only spelling that expresses both a self-hosted plan and a suspended one
without a sentinel like `-1` that every call site has to remember.

```python
plan.allows("max_messages_per_month", current=800, additional=200)   # True
plan.allows("max_messages_per_month", current=800, additional=201)   # False
```

The size of the work is passed in on purpose. A campaign to 900 recipients against
200 remaining is refused **whole**, before anything is written — a send stopped at
the ceiling leaves the customer billed for a partial delivery they cannot identify.

### Usage is derived, never accumulated

Every figure is a `COUNT` over the rows that actually exist. The tempting
alternative — a counter incremented on each send — is faster and wrong: it drifts
on a retry, on a crash between the send and the increment, and on any bulk
correction. A billing number that quietly disagrees with the message log is the
worst kind of wrong.

Messages are metered **when sent, not when queued**. A message that never left the
building cost the customer nothing.

Snapshots exist for the opposite reason. Once a period closes its total must stop
moving, even though the messages it was derived from are still subject to
retention. `billing.tasks.roll_billing_periods` runs hourly, is idempotent under a
unique constraint on `(organization, period_start)`, and catches a long gap up one
period at a time rather than jumping to now and losing the periods in between.

### Where it is enforced

| Seam | Limit |
|---|---|
| `contacts.services.create_contact` | `max_contacts` |
| `campaigns.services.launch_campaign` | `max_messages_per_month`, plus whether the subscription is entitled at all |

`QuotaExceeded` subclasses the project's existing `ValidationFailed`, so the campaign
wizard and the REST error handler render it — blockers list and all — without being
taught anything.

**`PAST_DUE` still sends.** A failed card should start a dunning conversation, not
sever a business's messaging mid-campaign. A lapsed subscription and a spent quota
raise different messages, because they need different remedies and collapsing them
would send half the customers to the wrong page.

### The pricing page is the plan catalogue

The landing page advertised "Up to 1,000 contacts" as a hard-coded string in
`pages/views.py` while nothing counted contacts. Those tiers are `Plan` rows now and
the page renders from them, so a promise on the page and a ceiling in `billing.usage`
can no longer drift apart. A test asserts the advertised number *is* the enforced one.

### What the seed migration decided, and what it refused to

Seeded: the three tiers already on the page, with **the contact limits they already
advertised** — 1,000, 10,000, unlimited. Enforcing a number this project has been
publishing is honest.

Not seeded: prices, monthly message caps, team-member caps. A cap that was never
advertised is a commercial decision, and a migration is not where those get made —
the same reason `price` stays `NULL` and renders "Pricing on request". The
enforcement is built and tested; publishing a figure is a number in the admin.

### Existing organizations were not downgraded

The one decision in this stage that could have broken a working system, so it was
made in the safe direction: the backfill puts every pre-existing organization on
**Self-hosted**, which has no limits. A business already running this software must
not discover one morning that it has been retroactively placed on a tier it never
chose, with a ceiling it never agreed to, halfway through a campaign.

New signups are a different case — they choose by signing up, and
`billing.services.subscribe()` starts them on the cheapest public plan, trialing.

A missing subscription resolves to the cheapest public plan and logs a warning.
Unlimited would give the product away to anyone whose signup half-failed; blocked
would take a working customer offline over a data problem they did not cause.

### Not in this stage

No payment provider, no invoices, no customer-facing billing page, no upgrade
button. `subscribe()` records an entitlement; it does not charge for one. Keeping
that seam clean is what lets Stage 4 put a provider behind these calls without
touching entitlement logic, and Stage 6 build the page that shows it.

---

## 28. Invoices and payments

Stage 4. Stage 3 decided what a customer owes; this collects it.

Everything in this section is written around one assumption: **it will run
twice.** Celery retries, providers redeliver webhooks for days, operators re-run
jobs after an outage, and people double-click. So every operation is idempotent,
and wherever idempotency rests on a check-then-act, a database constraint sits
underneath — because the check can be wrong and the constraint cannot.

### The provider is abstract, and only the mock is real

`billing/providers/` mirrors `whatsapp/services/` exactly: a `PaymentProvider`
ABC, a settings-driven factory, a mock implementation. Nothing outside that
package imports a concrete provider, so `PAYMENT_PROVIDER` is the only thing that
changes when a real gateway arrives.

Only `mock` is registered. That is not an oversight — a real gateway means
merchant credentials and a live account, and a half-written integration against
one is worse than an honest absence. The same position §22 takes on Meta.

The mock **honours idempotency keys**: a key it has seen returns the first
result rather than charging again. A mock that did not would let a double-charge
bug pass every test and appear in production. It has no concept of a card, which
is also deliberate — a mock that accepted card numbers could leak them.

### Three rules in the types

| Rule | Why |
|---|---|
| Money is `Decimal`, quantized to 2dp with `ROUND_HALF_UP` | `0.1 + 0.2` is a curiosity elsewhere and a discrepancy on an invoice. Banker's rounding is defensible statistically and indefensible to a customer reading a total |
| Every charge carries an idempotency key — required, not optional | A charge without one is a charge that can happen twice, and the second one is somebody's money |
| Providers report; this application decides | A provider that could mark its own charges settled would make a redelivered webhook indistinguishable from a second payment |

### Invoice numbers are gapless

`INV-2026-000123`, from a counter row taken under `select_for_update`. Not
`max(number) + 1`, which races two workers into the same number; and not a
database sequence, which does not roll back with its transaction. Several tax
authorities require gaplessness, and "invoice 41 does not exist" is a question no
finance team enjoys.

Which is also why a cancelled invoice is **voided, not deleted** — the number
stays taken, and "invoice 41 was cancelled" beats "invoice 41 never existed" for
anyone holding a copy of it.

### An issued invoice is immutable

`DRAFT` → `OPEN` freezes the totals; `recalculate()` refuses afterwards. An
invoice is a statement of what was owed at a moment. A system that can rewrite one
cannot be reconciled against anything, and a customer holding a copy that no
longer matches the database has reason to distrust both. The admin enforces the
same rule: every field goes read-only once issued, and invoices cannot be deleted.

Lines store what was charged, not what today's price list would charge — a plan
whose price changes next month must not silently rewrite last month's invoice.

### No invoice for an unpriced plan

Every seeded plan currently has `price = None`, so **no invoices are generated
today**. `generate_invoice()` returns `None` and logs it.

The alternative is worse than nothing. A 0.00 invoice tells a customer they owe
nothing this month, which is a claim this system cannot make when the page says
"Pricing on request". Set a price and invoicing starts on the next period close;
the machinery is built and tested either way.

### The webhook endpoint

`POST /api/billing/webhook/` — the second unauthenticated, CSRF-exempt route, and
the same design as the first, because a provider retries a non-200 for days.

- **The signature is the authentication.** Nothing is stored, parsed or queued until the HMAC over the *raw body* verifies. Compared as **bytes**, in constant time — `hmac.compare_digest` raises `TypeError` on non-ASCII `str`, which is how a hostile header becomes a 500 instead of a 403. This project already had that bug once, in the WhatsApp webhook.
- **A replay credits once.** `event_id` is unique, so a redelivery is stored zero times. Enforced by the constraint, caught in its own savepoint — a failed statement poisons the transaction it ran in, so without the savepoint a second event in the same delivery could not be stored after the first was rejected.
- **200 means stored, not understood.** Processing failures are recorded on the event. Asking the provider to redeliver would not fix a bug on our side, and each redelivery is another chance to double-credit.

### Dunning

A failed payment makes a subscription `PAST_DUE`, which **still sends** — cutting
a business off the moment a card expires is how a customer learns about a billing
problem from their own customers. Paying clears it automatically.

`collect_due_invoices` runs daily, not hourly: retrying a declined card every hour
annoys the customer and, on some networks, counts against the merchant.

### Not in this stage

No customer-facing billing page, no upgrade button, no PDF, no tax calculation,
no refund initiation from our side (a refund arriving from the provider is
handled; asking for one is not). Stage 6 builds the page.

---

## 29. Per-organization messaging credentials

Stage 5, and the one that changes how sending works. Until now there was one
WhatsApp Business Account for the whole installation, read from the environment.
That is right for a single business running its own copy and wrong for a
platform: a customer's messages must go out from **their** number, count against
**their** messaging limit, and stop when **they** disconnect.

### The split, and why it is not arbitrary

| Where | What | Why |
|---|---|---|
| Environment | App id, app secret, webhook verify token, API version | One Meta App serves every tenant |
| Database, encrypted | Access token, phone number id, WABA id | The customer's own |

The webhook forces this split. Meta delivers every tenant's events to one URL,
signed with the **app** secret — so verification has to happen before we know
which organization an event belongs to. You cannot look up a per-tenant secret
using a payload you have not yet authenticated. Routing happens afterwards, by
the `phone_number_id` in the payload, which is why that column is unique.

### Tokens are encrypted at rest

`core/encryption.py`, Fernet, keyed by `FIELD_ENCRYPTION_KEY`. The project's rule
was "credentials live in the environment"; multi-tenancy breaks that mechanism
without changing its intent, because there is no environment variable for a
thousand customers' tokens. So **the key lives in the environment and the secrets
live encrypted under it** — a database dump alone reveals nothing.

What it does not do: protect against the running application. A process that can
decrypt can read. It protects backups, replicas and dumps, which is most of the
realistic exposure.

`access_token` is a **property, not a field**. It is absent from `_meta.fields`,
so `ModelForm`, `ModelSerializer`, `values()` and the admin's default field list
all skip it unless somebody names it deliberately. The admin shows `…mnop` — the
last four characters, and only for a token long enough that four don't narrow it
down.

Decryption failure **raises**. An empty token handed to a provider looks like a
configuration mistake at the far end, days later; an exception says what actually
happened, now.

### Resolving the sender

`provider_for(organization)` → the organization's default active account, else
the environment. **That fallback is what keeps every pre-Stage-5 installation
working**, and it is also the thing to think hardest about before running this as
a public platform: a customer with no account of their own would send on the
deployment's number, limit and reputation.

`WHATSAPP_REQUIRE_MESSAGING_ACCOUNT=True` turns it off. A platform wants that on;
a business running its own copy wants it off, which is why off is the default —
the alternative breaks every existing deployment on upgrade.

Providers are built fresh every call, never cached. A cached provider holds one
tenant's token and would hand it to the next caller.

### Three cross-tenant defects this stage found and fixed

Writing the isolation tests surfaced these. All three date from Stage 1.

**1. An inbound STOP could withdraw the wrong customer's consent.** Two customers
can hold the same person as a contact. The lookup matched on the sender's number
alone and took whichever row came back first. Inbound messages now carry the
business number they arrived on, and the lookup is scoped to that organization.
This is the project's most sensitive invariant; it is now pinned by a test that
asserts the *other* tenant's contact is untouched.

**2. `Contact.phone_number` was globally unique.** The second business to try to
add a shared customer simply could not. Now unique per organization — a person is
routinely a customer of more than one business. Same for `ContactGroup.name` (one
customer naming a group "VIP" blocked everyone else) and `MessageTemplate`
`(name, language)` (templates belong to a WABA; several businesses register
"order_ready" independently).

**3. The duplicate check leaked a name.** `find_duplicate()` was unscoped, and the
error read "*<their contact's name>* already uses this number" — handing a
stranger a name out of a database they cannot otherwise see, one phone number at
a time. Now scoped, in the service and the form both.

### Deployment checks

`check --deploy` gained `core.W001`: `FIELD_ENCRYPTION_KEY` unset means the key is
derived from `SECRET_KEY`, which silently couples two unrelated rotations —
changing `SECRET_KEY`, otherwise routine, would make every stored token
permanently unreadable. Generate one with `python manage.py generate_encryption_key`.

There is deliberately **no** warning for `PAYMENT_PROVIDER=mock`. It is the only
implementation, so the check would be red on every run forever, and a check that
is always red is one everybody learns to scroll past.

### Not in this stage

No self-service connection flow (Meta Embedded Signup), no per-number routing of
outbound campaigns, no automatic token refresh. Accounts are entered in the admin
and verified with `verify_live`.

---

## 30. The billing area

Stage 6. Stages 3 to 5 built plans, usage, invoices and payments and gave the
customer no way to see any of it. `/billing/` is where the person paying finds
out what they are paying for.

Four pages: an overview, the plan catalogue, an invoice list, and one invoice.

### Reading is not changing

Any member can see the bill. Only an owner or an administrator can change the
plan or cancel. Hiding what the product costs from the people using it helps
nobody; changing it is the part that needs a role — and that split already
existed on `OrganizationMember.can_administer`, so this reuses it rather than
inventing a second notion of who is in charge.

A member who tries anyway is refused and told why, not silently redirected.

### Every mutation is a POST

Changing a plan, cancelling and resuming are all POST with CSRF. A plan-change
link would be followed by every prefetcher and link scanner that saw it. A GET
to the change-plan route returns **405**, not a redirect, so the mistake would be
loud if anyone ever added one.

`is_active=False` plans cannot be chosen by guessing a slug.

### A downgrade that would not fit is refused, with the numbers

> The Tiny plan allows 1 contacts and you have 3. Reduce them first, or choose a
> larger plan.

Checked against **live counts**, not against the old plan's limits — the question
is not "is this smaller?" but "does what they have fit?". Moving from unlimited
to 10,000 contacts is fine for somebody holding 500. Accepting a bad downgrade
would leave a customer instantly over a ceiling they did not know they were
choosing, unable to add a contact and unsure why.

### Cancelling runs to the end of the period

Cutting somebody off the moment they click takes away time they have already
bought. The overview then offers **"Keep my subscription"** until the period ends,
because a cancellation a month away is one people change their minds about.

### Nothing is invented

The rule the landing page has followed since Phase 8, applied to a page about
money:

| Situation | What the page says |
|---|---|
| Plan has no price | "Pricing on request" — never a fabricated figure |
| Metric has no ceiling | "0 of unlimited", and **no progress bar** — a bar against no ceiling is meaningless, and 0% would imply one exists |
| No invoices, unpriced plan | "quoted individually rather than charged automatically" |
| No invoices, priced plan | "raised when the current billing period closes" |
| Over a limit | "Over the limit by 2. You can still read everything; adding more is what is blocked." |
| No subscription row | Says so, and still renders |

An empty table styled like a real one reads as a bug. So does a 500 when a row
is missing — the overview renders for an organization with no subscription and
explains the state.

The overage is computed in Python, not the template: Django's `add` filter cannot
subtract, and the arithmetic that looks like it does is addition in disguise.

### Invoices are scoped, and 404 for anyone else

An invoice carries a business name, an amount and a period — the most sensitive
document this application renders. `Invoice.objects.for_organization(...)` backs
both the list and the detail view, so another tenant's invoice is a **404, not a
403**: telling somebody a document exists but is not theirs confirms it exists.

Failed payment attempts are shown, not hidden. "We tried twice" is a fact a
customer may need explained, and hiding it makes a support conversation harder
than it needs to be.

### Not in this stage

No PDF download, no payment-method entry (there is no gateway to enter one
into), no self-service upgrade *checkout* — `change_plan` records the
entitlement, and Stage 4's `collect()` bills it at the next period close.

---

## 31. The backoffice

Stage 7, at `/backoffice/`. **This is the only part of the application that reads
across the tenant boundary on purpose.** Everywhere else, a query without an
organization filter is a bug; here it is the job. That inversion is what the
whole design of this app is arranged around.

Four pages: a platform overview, an organization list, one organization, and a
health page.

### The gate is `is_staff`, not `User.role`

`role` says what somebody may do inside the product. `is_staff` says they work
for whoever runs it. Stage 1 separated those precisely so a customer's own
administrator could never end up reading somebody else's data, and this is the
stage where that separation earns its keep.

`is_staff` is settable only through Django's admin, which already requires
`is_staff` — so **this app grants no capability its users did not already have.**
It is a better window onto data they can reach anyway, not a wider one. Tests
assert that an organization *owner* and a `UserRole.ADMINISTRATOR` are both
refused.

A signed-in customer who guesses the URL gets **404, not 403**: a 403 confirms
something exists at that address. An anonymous visitor gets the ordinary sign-in
redirect, which leaks nothing the login page does not. The nav link is hidden
rather than disabled, because a greyed-out "Platform" item would tell every
customer a cross-tenant view exists.

### Looking is recorded

Opening a customer's page is a privacy event, not a page view. It writes an
audit entry naming who looked and what they looked at, **before the page
renders**, so a template error does not lose the record. "Who has read this
customer's account?" has to have an answer.

The staff check runs *before* the target is resolved. Otherwise a stranger's
request would run the lookup and write an entry naming a customer they have no
right to see.

The aggregate pages are **not** audited. A count of organizations is not a look
at any particular customer, and auditing every dashboard refresh would bury the
entries that matter. That is only defensible while those pages identify nobody —
so a test asserts the overview names no organization.

### Two things it cannot do

Enforced by never building them, and pinned by tests:

- **No message content.** An operator can see that a campaign ran, how many
  recipients it had and how many failed. They cannot read a line the customer
  wrote, or see a contact's name or number. Support work needs aggregates;
  reading correspondence is a different power and nobody asked for it.
- **No impersonation.** There is no "sign in as this customer" button.

Access tokens show as `…mnop` — the hint identifies a sender without being one.

### Read-only, by omission

No form, no POST route, no action button. Every page returns **405** to a POST,
which is asserted as behaviour rather than checked in markup. Editing belongs in
Django admin, which has its own audit trail and permission model; duplicating it
here would mean two places to get authorization wrong instead of one.

### Every unscoped query lives in one file

`backoffice/services.py`, and nowhere else. Scattered through a views module,
those queries would look exactly like the mistakes they resemble. Collected in
one file whose docstring says what they are, a reviewer knows that an unscoped
query *anywhere else* is almost certainly a bug, and that one *here* needs
checking for a different thing: that it returns aggregates and metadata rather
than anybody's content.

The organization list annotates its counts rather than fetching per row — a
hundred organizations at four lookups each is four hundred queries, and an
operations page that takes ten seconds is one nobody opens.

### The health page is ordered by how quietly things fail

A past-due subscription eventually announces itself to the customer. A webhook
that has been failing for three days announces itself to nobody, and every hour
it keeps failing is another hour of delivery reports going missing. So that
comes first.

An installation with nothing wrong says "Nothing past due" rather than showing
an empty list styled like a real one.

---

## 32. Channels and SMS

Stage 9, taken before Stage 8 deliberately: hardening is best done once the shape
is final, and a second messaging channel is the thing most likely to reveal that
an abstraction needs changing. Finding that after hardening means hardening twice.

It found two things.

### The provider seam was WhatsApp-shaped

`WhatsAppProvider` requires `send_template(name, language, header_variables)` and
`fetch_templates()` — an interface built around Meta's approval registry. SMS has
no such thing: no upstream catalogue to sync, no language variant to select,
nothing to get approved. Making SMS implement that contract would have meant
three methods raising `NotImplementedError` and a fourth pretending a template
name was something a gateway understood.

So SMS gets its own, deliberately smaller contract: **send some text to a number,
and say what happened.** What the two providers share is not an interface but a
*result shape* — `success`, `provider_message_id`, `error_code`, `retryable` —
and sharing the shape is enough for one Celery task to drive either.

`messaging/routing.py` is the only module that knows both exist. The retry logic,
the claim protocol, the rate limiter and the status machine were not touched.

### Consent had to become per channel

This is the part that mattered, and the reason this was not a small stage.

`Contact.opted_in` was one boolean because there was one channel. Adding SMS on
top of it would have meant **every contact who agreed to WhatsApp order updates
was silently opted in to SMS marketing** — and nothing would have looked wrong.
That is precisely what "consent is never inferred" forbids, and in most
jurisdictions the two channels are separately regulated.

So `eligible()` takes a channel, and it is still the only place the rule is
written:

```python
Contact.objects.eligible(Channel.SMS)   # only people who agreed to SMS
```

**No record means no consent.** The absence of a row is a "no", never a "not
asked yet, so probably fine". A channel added tomorrow starts with nobody on it.

WhatsApp still reads `opted_in`; every other channel reads
`ContactChannelConsent`. That asymmetry is deliberate and documented: `opted_in`
carries every opt-in and opt-out this system has ever recorded, each with a
source and an audit entry, and **migrating consent state is the riskiest data
migration this codebase could run** — getting it wrong means messaging somebody
who said no. Consolidating them is a later job done on purpose, not a side effect
of adding SMS.

### What follows from the channel

| Thing | Behaviour |
|---|---|
| `Campaign.channel` | Chosen at creation. The audience was resolved against this channel's consent, so changing it later would send to people who never agreed |
| `Message.channel` | Taken from the campaign **on insert**, not defaulted — a caller passing a different one is the exact mistake being guarded against, so the campaign wins |
| Template on SMS | Refused at validation, not discovered a thousand messages in |
| Empty SMS audience | Says *"no recipient has recorded consent for SMS"* — naming the channel, because "nobody consented" is baffling to somebody looking at a group full of opted-in WhatsApp contacts |
| Launch | `routing.preflight()` checks the channel's provider before any state changes |

### Segments, because a gateway bills per segment

160 characters — unless one character forces UCS-2, at which point it is 70. A
customer who writes 155 characters and adds one emoji goes from **one segment to
three** and finds out on an invoice. `segment_count()` gets this right, including
the seven characters a concatenation header costs and the two positions an
extended GSM-7 character (`{`, `€`) takes.

### Only the mock exists

Same position as the payment gateway, for the same reason: a real SMS provider
needs an account, a registered sender id and, in several countries, regulatory
paperwork. A half-written integration against one is worse than an honest
absence. The mock simulates a carrier rejection, an unregistered sender id, a
throttle and a transient error — the two permanent and two retryable — because a
retry path that has never run is a retry path that does not work.

### Not in this stage

No per-organization SMS sender ids (there is one installation-wide `SMS_SENDER_ID`),
no delivery-receipt webhook endpoint for SMS, no per-channel plan limits, and no
UI for choosing a channel when creating a campaign — `Campaign.channel` is set in
the admin or the API. Each is a small piece; none of them is the abstraction.
