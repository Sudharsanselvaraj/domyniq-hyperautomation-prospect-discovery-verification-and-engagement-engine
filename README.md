# Domyniq — Hyperautomation Prospect Discovery, Verification & Engagement Engine

> One domain in. Researched, verified, personalised emails out. Zero human steps in between.

Built for the **Vocallabs SDE take-home assignment** — a fully automated 4-stage B2B cold outreach pipeline that sources lookalike companies, identifies decision-makers, resolves verified work emails, and fires personalised AI-generated outreach — all from a single CLI command.

---

## Table of Contents

1. [How it works](#how-it-works)
2. [Architecture](#architecture)
3. [Project structure](#project-structure)
4. [Prerequisites & accounts](#prerequisites--accounts)
5. [Installation](#installation)
6. [Environment variables](#environment-variables)
7. [Running the pipeline](#running-the-pipeline)
8. [CLI reference](#cli-reference)
9. [Output files](#output-files)
10. [Running tests](#running-tests)
11. [Error handling](#error-handling)
12. [Reliability internals](#reliability-internals)
13. [Scaling strategy](#scaling-strategy)
14. [Design decisions & tradeoffs](#design-decisions--tradeoffs)
15. [Future improvements](#future-improvements)

---

## How it works

```
You type:   python main.py stripe.com

Stage 1  ──  Apollo.io        seed domain  →  up to 25 lookalike company domains
Stage 2  ──  Prospeo Search   domains      →  C-suite / VP / Director contacts + LinkedIn URLs
Stage 3  ──  Prospeo Enrich   person IDs   →  verified work emails (bulk, up to 50/call)
             ── pause ──      (rate-limit window between Stage 2 and 3)
             [SAFETY GATE]    show summary, wait for y/n before any email fires
Stage 4  ──  OpenAI + Brevo   emails       →  personalised subject + body, sent
```

Every stage's output is the next stage's input. No copy-paste, no manual handoffs. The only human interaction is the confirmation prompt before emails send.

---

## Architecture

```
 INPUT: company.domain
        │
        ▼
┌────────────────────────────────────────────────────────────────┐
│                    main.py  (Typer CLI)                        │
│  Rich progress bars · Safety checkpoint · CSV/JSON output      │
│  Signal handlers (SIGINT/SIGTERM) → save checkpoint on Ctrl+C  │
└───────────────────────┬────────────────────────────────────────┘
                        │
           ┌────────────▼─────────────┐
           │    PipelineOrchestrator   │
           │  asyncio · semaphore(10)  │
           │  checkpoint · dedup       │
           └──┬──────┬──────┬────────┬─┘
              │      │      │        │
         [1/4]│ [2/4]│ [3/4]│   [4/4]│
              ▼      ▼      ▼        ▼
          Apollo  Prospeo  Prospeo  Brevo
           .io    Search   Enrich   + OpenAI
              │      │      │        │
              └──────┴──────┴────────┘
                         │
                   BaseClient (all)
                   ├── httpx.AsyncClient
                   ├── Tenacity retry (429 / 500–504)
                   └── Circuit breaker (CLOSED → OPEN → HALF-OPEN)
```

### Sequence diagram

```
User      main.py     Orchestrator   Apollo.io   Prospeo    Prospeo    OpenAI    Brevo
 │           │              │             │        Search    Enrich       │        │
 │  domain ──▶             │             │           │          │         │        │
 │           │  execute() ─▶            │           │          │         │        │
 │           │              │─ POST /organizations/search ──▶  │         │        │
 │           │              │◀─ company domains ──────────────  │         │        │
 │           │              │  [dedup by domain]               │         │        │
 │           │              │─ POST /search-person (×25) ────────▶        │         │        │
 │           │              │◀─ contacts + person_ids ─────────────       │         │        │
 │           │              │  [dedup by linkedin_url]         │          │         │        │
 │           │              │  [15s rate-limit pause]          │          │         │        │
 │           │              │─ POST /bulk-enrich-person ─────────────────▶│         │        │
 │           │              │◀─ verified work emails ──────────────────────         │        │
 │           │              │  [dedup by email]                │          │         │        │
 │           │◀─ result ────│             │           │          │         │        │
 │  [SUMMARY]◀─            │             │           │          │         │        │
 │  y/n ─────▶             │             │           │          │         │        │
 │           │  send() ─────▶            │           │          │         │        │
 │           │              │─ POST /chat/completions (×N) ──────────────▶│        │
 │           │              │◀─ subject + body JSON ──────────────────────          │        │
 │           │              │─ POST /v3/smtp/email (×N) ──────────────────────────▶│
 │           │              │◀─ messageId ────────────────────────────────────────── │
 │  [DONE] ◀─              │             │           │          │         │        │
```

### Data flow

| Stage | Service | Input | Output | API calls |
|-------|---------|-------|--------|-----------|
| 1 | Apollo.io | 1 seed domain | ≤ 25 company domains | 2 (introspect + search) |
| 2 | Prospeo `/search-person` | 25 domains | ≤ 125 contacts + `person_id` | 25 parallel |
| — | Rate-limit pause | — | — | `PROSPEO_ENRICH_DELAY_SECONDS` wait |
| 3 | Prospeo `/bulk-enrich-person` | ≤ 125 `person_id`s | verified emails | ceil(N/50) batched |
| 4 | OpenAI + Brevo | emails + names | personalised email sent | 2 per lead |

Deduplication is applied after each stage: by `domain` (Stage 1), by `linkedin_url` (Stage 2), by `email` (Stage 3).

---

## Project structure

```
.
├── main.py                      # CLI entry point — Typer + Rich, signal handlers
├── env.example                  # Copy to .env and fill in API keys
├── requirements.txt
├── pytest.ini
│
├── config/
│   └── settings.py              # Pydantic-settings config — validated at startup
│
├── clients/
│   ├── base.py                  # BaseClient: httpx + Tenacity retry + circuit breaker
│   ├── apollo_client.py         # Stage 1: POST /api/v1/organizations/search
│   ├── prospeo_client.py        # Stage 2+3: /search-person + /bulk-enrich-person
│   ├── brevo_client.py          # Stage 4: POST /v3/smtp/email
│   ├── ocean_client.py          # Original Stage 1 (Ocean.io) — superseded by Apollo
│   └── eazyreach_client.py      # Original Stage 3 (EazyReach) — superseded by Prospeo
│
├── services/
│   ├── orchestrator.py          # 4-stage pipeline, asyncio gather, checkpoints, dedup
│   └── email_generator.py       # OpenAI GPT-4o-mini → JSON subject + body
│
├── models/
│   └── pipeline.py              # Pydantic v2: Company, Contact, Lead, EmailResult
│
├── utils/
│   ├── circuit_breaker.py       # CLOSED / OPEN / HALF-OPEN state machine
│   ├── dedup.py                 # Dedup by domain / linkedin_url / email
│   ├── exceptions.py            # Typed exception hierarchy (ApolloError, ProspeoError…)
│   ├── logger.py                # JSON file log + Rich console handler
│   ├── metrics.py               # Per-stage timing + count tracking
│   └── resume.py                # JSON checkpoints in data/.checkpoints/
│
├── tests/
│   ├── conftest.py
│   ├── test_unit.py             # Models, dedup, circuit breaker, email gen (28 tests)
│   └── test_integration.py      # Full pipeline mocked end-to-end
│
├── docs/
│   └── system_design.md         # Extended architecture, tradeoffs, scaling
│
├── data/                        # Created at runtime
│   ├── output.csv               # Lead report — written after every run
│   ├── output.json              # Optional JSON export (--json-output flag)
│   └── .checkpoints/            # Per-domain checkpoint files for --resume
│
└── logs/                        # Created at runtime
    └── pipeline.log             # Newline-delimited JSON structured logs
```

---

## Prerequisites & accounts

### Accounts you need

| Service | Purpose | Sign-up URL | Cost |
|---------|---------|------------|------|
| **Apollo.io** | Stage 1 — find lookalike companies | [apollo.io](https://apollo.io) | Free tier available |
| **Prospeo** | Stage 2+3 — decision-makers + email enrichment | [app.prospeo.io/api](https://app.prospeo.io/api) | Free credits on signup |
| **Brevo** | Stage 4 — transactional email send | [app.brevo.com](https://app.brevo.com) | Free tier (300 emails/day) |
| **OpenAI** | Email copy generation | [platform.openai.com](https://platform.openai.com) | Pay-per-use |

> **Important for Brevo:** You must verify your sender domain (or at minimum your sender email address) inside the Brevo dashboard before any emails will deliver. Do this before your first run.

> **Important for Prospeo:** The free plan enforces a rate limit between the `/search-person` and `/bulk-enrich-person` calls. The `PROSPEO_ENRICH_DELAY_SECONDS` setting (default: 15) adds a pause between Stage 2 and Stage 3 to respect this. Increase it if you see 429 errors during enrichment.

### System requirements

- **Python 3.11+** (developed on 3.12)
- pip / venv
- Internet access (all APIs are cloud-hosted)

---

## Installation

### 1. Clone the repository

```bash
git clone https://github.com/<your-username>/domyniq-hyperautomation-prospect-discovery-verification-and-engagement-engine.git
cd domyniq-hyperautomation-prospect-discovery-verification-and-engagement-engine
```

### 2. Create a virtual environment

```bash
python -m venv venv

# macOS / Linux
source venv/bin/activate

# Windows (Command Prompt)
venv\Scripts\activate.bat

# Windows (PowerShell)
venv\Scripts\Activate.ps1
```

### 3. Install dependencies

```bash
pip install -r requirements.txt
```

### 4. Create your `.env` file

```bash
cp env.example .env
```

Open `.env` in your editor and fill in every value (see [Environment variables](#environment-variables) below).

### 5. Verify the setup

```bash
python -c "from config.settings import Settings; s = Settings(); print('✓ Config loaded:', s.apollo_api_key[:8] + '...')"
```

If this prints `✓ Config loaded: ...` without error, you're ready to run.

---

## Environment variables

Copy `env.example` to `.env` and populate every value. All keys are **required** unless marked optional.

```bash
# ── API Keys ──────────────────────────────────────────────────
APOLLO_API_KEY=your_apollo_api_key_here
PROSPEO_API_KEY=your_prospeo_api_key_here
BREVO_API_KEY=your_brevo_api_key_here
OPENAI_API_KEY=sk-your_openai_key_here

# ── Sender identity (must be verified in Brevo) ───────────────
SENDER_NAME=Your Full Name
SENDER_EMAIL=you@yourdomain.com

# ── Pipeline tuning (optional — defaults shown) ───────────────
MAX_SIMILAR_COMPANIES=25
MAX_CONTACTS_PER_COMPANY=5
MAX_CONCURRENT_REQUESTS=10
REQUEST_TIMEOUT_SECONDS=30
PROSPEO_ENRICH_DELAY_SECONDS=15

# ── Logging (optional) ────────────────────────────────────────
LOG_LEVEL=INFO
LOG_FILE=logs/pipeline.log

# ── OpenAI (optional) ─────────────────────────────────────────
OPENAI_MODEL=gpt-4o-mini
EMAIL_MAX_WORDS=120
```

### Full reference

| Variable | Required | Default | Description |
|----------|:--------:|---------|-------------|
| `APOLLO_API_KEY` | ✅ | — | Apollo.io API key. Found in Settings → API Keys inside the Apollo dashboard. |
| `PROSPEO_API_KEY` | ✅ | — | Prospeo API key. Found under your account at app.prospeo.io/api. |
| `BREVO_API_KEY` | ✅ | — | Brevo SMTP API key (not the login password). SMTP & API → API Keys in the Brevo dashboard. |
| `OPENAI_API_KEY` | ✅ | — | OpenAI secret key starting with `sk-`. Used only for email copy generation. |
| `SENDER_NAME` | ✅ | — | The "From" display name on outreach emails, e.g. `Jane Smith`. |
| `SENDER_EMAIL` | ✅ | — | The "From" email address. Must be verified in Brevo or sends will fail. |
| `MAX_SIMILAR_COMPANIES` | ○ | `25` | How many lookalike companies Apollo returns. Range: 1–200. |
| `MAX_CONTACTS_PER_COMPANY` | ○ | `5` | Max decision-makers fetched per company from Prospeo. Range: 1–50. |
| `MAX_CONCURRENT_REQUESTS` | ○ | `10` | Semaphore ceiling for parallel API calls. Lower this if you hit rate limits. |
| `REQUEST_TIMEOUT_SECONDS` | ○ | `30` | Per-request HTTP timeout in seconds. |
| `PROSPEO_ENRICH_DELAY_SECONDS` | ○ | `15` | Seconds to pause between Stage 2 (search) and Stage 3 (enrich). Increase if Prospeo returns 429 during enrichment. Set to `0` on paid plans. |
| `LOG_LEVEL` | ○ | `INFO` | Python log level: `DEBUG`, `INFO`, `WARNING`, `ERROR`, or `CRITICAL`. |
| `LOG_FILE` | ○ | `logs/pipeline.log` | Path for the JSON structured log file. |
| `OPENAI_MODEL` | ○ | `gpt-4o-mini` | OpenAI chat model used for email generation. |
| `EMAIL_MAX_WORDS` | ○ | `120` | Soft word-count ceiling passed in the email generation prompt. |

---

## Running the pipeline

### Standard run

```bash
python main.py openai.com
```

This executes all four stages. After Stage 3 completes, a summary is shown and you are prompted to confirm before any email is sent.

### Dry run — execute everything except the email send

```bash
python main.py stripe.com --dry-run
```

Runs Stages 1–3 in full, generates the CSV and (optionally) JSON output, but skips the Stage 4 send. Use this to verify your API integrations are working before committing to a real send.

### Resume an interrupted run

```bash
python main.py stripe.com --resume
```

If a previous run for this domain was interrupted (Ctrl+C, network failure, etc.), checkpoints are stored in `data/.checkpoints/stripe_com.json`. The `--resume` flag reloads from the last successful stage and continues from there, saving API quota.

### Adjust the number of companies

```bash
python main.py notion.so --max-companies 50
```

Overrides `MAX_SIMILAR_COMPANIES` for this run only. Useful for quick tests (set to `5`) or broader searches (up to 200).

### Export JSON in addition to CSV

```bash
python main.py hubspot.com --json-output
```

Writes `data/output.json` alongside the standard `data/output.csv`.

### Combining flags

```bash
python main.py salesforce.com --dry-run --max-companies 10 --json-output
```

---

## CLI reference

```
Usage: python main.py [OPTIONS] DOMAIN

  Run the full 4-stage cold outreach pipeline for a single seed domain.

  Stages:
    [1/4] Apollo.io  — find lookalike companies
    [2/4] Prospeo    — extract decision-makers + LinkedIn URLs
    [3/4] Prospeo    — bulk-enrich verified work emails
    [4/4] Brevo      — send personalised outreach emails

Arguments:
  DOMAIN  Seed company domain, e.g. openai.com  [required]

Options:
  -d, --dry-run                Run all stages but skip the final email send.
  -r, --resume                 Resume from last checkpoint if a previous run
                               exists for this domain.
  -n, --max-companies INTEGER  Maximum number of similar companies to fetch
                               from Apollo.io.  [default: 25]
  --json-output                Also write results to data/output.json in
                               addition to CSV.
  --help                       Show this message and exit.
```

---

## Output files

### `data/output.csv`

Written after every run (even dry runs, even if no emails were sent).

| Column | Description |
|--------|-------------|
| `company` | Company domain from Stage 1 |
| `contact` | Full name of the decision-maker |
| `title` | Job title |
| `linkedin` | LinkedIn profile URL |
| `email` | Verified work email |
| `email_subject` | Generated subject line (blank if emails not sent) |
| `email_body` | Generated HTML email body (blank if emails not sent) |
| `email_sent` | `True` / `False` |
| `timestamp` | UTC ISO-8601 timestamp of lead creation |

### `data/output.json`

Written only when `--json-output` is passed. Contains the same data as the CSV in JSON array format, with the full nested `contact` object.

### `data/.checkpoints/<domain>.json`

Written incrementally during the run. Contains the serialised output of each completed stage so the pipeline can resume mid-run. Automatically deleted on successful completion. Do not edit manually.

### `logs/pipeline.log`

Newline-delimited JSON. Every log entry is a complete JSON object:

```json
{"timestamp": "2026-06-08T10:23:41+00:00", "level": "INFO", "logger": "services.orchestrator", "message": "Stage 1 complete — 23 companies"}
{"timestamp": "2026-06-08T10:23:56+00:00", "level": "INFO", "logger": "services.orchestrator", "message": "Email sent", "to": "jane@acme.com", "message_id": "MSG-abc123"}
```

Parse with `jq`:
```bash
cat logs/pipeline.log | jq .
cat logs/pipeline.log | jq 'select(.level == "ERROR")'
cat logs/pipeline.log | jq 'select(.message == "Email sent") | .to'
```

---

## Running tests

```bash
# All tests (28 total)
pytest

# Verbose output
pytest -v

# Unit tests only (models, dedup, circuit breaker, email gen)
pytest tests/test_unit.py -v

# Integration tests only (full pipeline with mocked APIs)
pytest tests/test_integration.py -v

# With coverage report
pytest --cov=. --cov-report=term-missing

# HTML coverage report
pytest --cov=. --cov-report=html
open htmlcov/index.html
```

All 28 tests pass with zero external network calls — every API is mocked.

---

## Error handling

The pipeline **never crashes**. Every failure is isolated and the run continues.

| Scenario | Behaviour |
|----------|-----------|
| Apollo returns 0 companies | Pipeline exits cleanly with an empty report |
| One company's Prospeo lookup fails | Logged as a warning; remaining companies continue unaffected |
| Prospeo bulk enrich returns no email for a contact | Contact is skipped; not added to the lead list |
| OpenAI email generation fails | Hardcoded fallback copy is used; email still sends |
| Brevo send fails for one email | Logged as an error; `email_sent=False` in CSV; rest of sends continue |
| API returns 429 (rate limit) | Tenacity retries up to 3× with exponential backoff (1s → 60s) |
| API returns 500 / 502 / 503 / 504 | Same retry policy |
| 5 consecutive failures to one service | Circuit breaker opens; all calls to that service fast-fail for 60s |
| Run interrupted with Ctrl+C or SIGTERM | Signal handler saves checkpoint; `--resume` continues from that point |
| Config key missing at startup | `pydantic-settings` raises with a clear message; pipeline never starts |

---

## Reliability internals

### Retry policy (Tenacity)

Configured in `clients/base.py`. Applied to every outbound API call.

- **Retried on:** `RateLimitError` (429) and `ServiceUnavailableError` (500/502/503/504)
- **Not retried on:** `AuthenticationError` (401/403) — a bad key won't fix itself
- **Strategy:** exponential backoff, `min=1s`, `max=60s`, up to `RETRY_MAX_ATTEMPTS` (default: 3)
- **Before sleep:** logs a warning with the exception and wait time

### Circuit breaker

Three states managed in `utils/circuit_breaker.py`:

```
CLOSED  ─────── 5 consecutive ServiceUnavailableErrors ──────▶  OPEN
   ▲                                                                │
   │                                                   60s recovery timeout
   │                                                                │
   └─────────── next call succeeds ──────────────  HALF-OPEN ◀─────┘
```

- **CLOSED** → normal operation
- **OPEN** → raises `CircuitOpenError` immediately; no network call made
- **HALF-OPEN** → allows one probe request; success → CLOSED, failure → back to OPEN

### Checkpoint / resume

Checkpoints are JSON files written to `data/.checkpoints/<domain>.json`.

- After Stage 1: all company objects saved
- During Stage 2: each company's contacts saved immediately after its Prospeo call (per-item checkpoint — survives mid-stage interruption)
- After Stage 3: all verified lead objects saved
- On Ctrl+C: signal handler calls `resumable.save()` before exit
- On `--resume`: loads from checkpoint, skips completed stages

### Deduplication

Applied at three points to prevent duplicate contacts or emails:

| Point | Key | Why |
|-------|-----|-----|
| After Stage 1 | `company.domain` | Apollo can return the same domain via different keyword matches |
| After Stage 2 | `contact.linkedin_url` | The same exec may appear at multiple company lookups |
| After Stage 3 | `lead.email` | Belt-and-suspenders before the email send |

---

## Scaling strategy

The current design handles ~25 companies × 5 contacts = 125 leads in a single Python process. To go larger:

| Axis | Current limit | Scale path |
|------|--------------|-----------|
| Companies per run | 200 (Apollo hard cap) | Multiple seed domains → `asyncio.gather()` multiple `execute()` calls |
| Concurrent API calls | 10 (semaphore) | Raise `MAX_CONCURRENT_REQUESTS`; monitor for 429s |
| Email volume | Brevo free: 300/day | Upgrade Brevo plan or use multiple sub-accounts with round-robin |
| Throughput | single process | Replace `asyncio.gather` with Celery workers + Redis broker |
| Storage | local CSV | Swap `write_csv` for SQLAlchemy → PostgreSQL or BigQuery |
| Observability | JSON log file | Ship `logs/pipeline.log` to Datadog / Loki via Fluentd |
| Checkpointing at scale | per-file JSON | Replace with Redis keys (`SETEX` with TTL) |

For > 10K leads/day: decouple stages with a message queue (Redis Streams or Kafka). Each stage becomes a consumer group. Stage 1 publishes company domains; Stage 2 consumes them and publishes contacts; and so on. Checkpointing becomes consumer offset tracking.

---

## Design decisions & tradeoffs

| Decision | Chosen | Alternative considered | Reasoning |
|----------|--------|----------------------|-----------|
| HTTP client | `httpx` (async) | `requests` (sync) | Stages 2 and 3 dispatch 25+ requests in parallel; sync would be ~10× slower |
| Stage 3 implementation | Prospeo `/bulk-enrich-person` | EazyReach per-URL calls | One batch call for 50 contacts vs 50 individual HTTP calls; cheaper and faster |
| Stage 1 implementation | Apollo.io | Ocean.io (original spec) | Ocean.io sign-ups unavailable; Apollo has richer firmographic filtering |
| Retry library | Tenacity | Manual `for _ in range(n)` loop | Tenacity handles jitter, `before_sleep` logging, and retry predicates cleanly |
| CLI framework | Typer + Rich | Click + colorama | Typer is type-hint-native; Rich is the best-in-class terminal renderer |
| Config | `pydantic-settings` | `os.environ` + manual casting | Type validation + `.env` loading + fail-fast error messages in one library |
| Models | Pydantic v2 | `dataclasses` | Runtime validation, JSON serialisation, and field-level validators built in |
| Concurrency | `asyncio` + semaphore | `threading.ThreadPoolExecutor` | No GIL contention on I/O; coroutines are cheaper than threads for 100+ tasks |
| Checkpoint format | JSON file per domain | Redis, SQLite | Zero infrastructure dependency; restores perfectly for single-machine use |
| Email generation | OpenAI JSON-mode prompt | Jinja2 templates | Personalisation quality; JSON response contract makes parsing trivial to test |

---

## Future improvements

- **Follow-up sequences** — if no Brevo open within 5 days, auto-enqueue a follow-up with different copy
- **CRM push** — after Stage 2, push contacts to HubSpot / Salesforce so sales can see the pipeline in real time
- **Brevo webhook** — consume open/click events and update `data/output.csv` with engagement data
- **Sender domain warm-up** — for new domains, throttle daily send volume and ramp gradually to protect deliverability
- **A/B copy variants** — generate 2 subject line variants per lead; split 50/50 and report winner
- **Web dashboard** — FastAPI + HTMX frontend for non-technical operators to kick off runs and view results
- **Slack/Teams notifications** — post a pipeline summary card on completion
- **Unsubscribe handling** — maintain a suppression list; skip re-contacted domains and opted-out emails
