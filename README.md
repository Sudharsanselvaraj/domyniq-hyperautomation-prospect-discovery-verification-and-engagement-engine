# ⚡ Cold Outreach Pipeline

**Fully automated B2B cold outreach — one domain in, emails out.**

Built as a production-quality solution for the Vocallabs SDE take-home assignment.

---

## Architecture Diagram

```
 INPUT: company.domain
        │
        ▼
┌───────────────────────────────────────────────────────────────┐
│                     main.py  (Typer CLI)                      │
│         Rich progress bars ─ Safety checkpoint ─ CSV output   │
└───────────────────────┬───────────────────────────────────────┘
                        │
           ┌────────────▼────────────┐
           │   PipelineOrchestrator  │
           │   asyncio + semaphore   │
           └──┬──────┬──────┬──────┬─┘
              │      │      │      │
         [1/4]│ [2/4]│ [3/4]│ [4/4]│
              ▼      ▼      ▼      ▼
          Apollo  Prospeo Prospeo Brevo
           .io    Search  Enrich  + OpenAI
              │      │      │      │
              └──────┴──────┴──────┘
                         │
                  BaseClient (all four)
                  ├── httpx async
                  ├── Tenacity retry (429/5xx)
                  └── Circuit breaker
```

---

## Project Structure

```
outreach_pipeline/
├── main.py                    # CLI entry point (Typer + Rich)
├── config/
│   ├── __init__.py
│   └── settings.py            # Pydantic-settings config loader
├── clients/
│   ├── __init__.py
│   ├── base.py                # BaseClient: retry + circuit breaker
│   ├── apollo_client.py       # Stage 1: find similar companies
│   ├── prospeo_client.py      # Stage 2 & 3: find decision-makers + bulk enrich emails
│   ├── ocean_client.py        # (archival) original Stage 1 client
│   ├── eazyreach_client.py    # (archival) original Stage 3 client
│   └── brevo_client.py        # Stage 4: send outreach emails
├── services/
│   ├── __init__.py
│   ├── orchestrator.py        # Pipeline orchestration + async parallelism
│   └── email_generator.py     # OpenAI email copy generation
├── models/
│   ├── __init__.py
│   └── pipeline.py            # Pydantic models: Company, Contact, Lead
├── utils/
│   ├── __init__.py
│   ├── circuit_breaker.py     # Circuit breaker pattern
│   ├── dedup.py               # Deduplication utilities
│   ├── exceptions.py          # Custom exception hierarchy
│   ├── logger.py              # Structured JSON + Rich logging
│   ├── metrics.py             # Per-stage execution metrics
│   └── resume.py              # Checkpoint / resume interrupted runs
├── tests/
│   ├── conftest.py
│   ├── test_unit.py           # Unit tests (models, dedup, circuit breaker)
│   └── test_integration.py    # Integration tests (mocked APIs)
├── docs/
│   └── system_design.md       # Architecture + sequence diagrams
├── data/
│   └── output.csv             # Generated after pipeline run
├── logs/
│   └── pipeline.log           # Structured JSON logs
├── .env.example               # Environment variable template
├── .gitignore
├── pytest.ini
├── requirements.txt
└── README.md
```

---

## Setup Instructions

### Prerequisites

- Python 3.11+
- pip

### 1. Clone and install

```bash
git clone <repo-url>
cd outreach_pipeline
python -m venv venv
source venv/bin/activate        # Windows: venv\Scripts\activate
pip install -r requirements.txt
```

### 2. Configure environment

```bash
cp .env.example .env
# Edit .env with your real API keys
nano .env
```

### 3. Verify setup

```bash
python -c "from config.settings import Settings; print('Config OK:', Settings())"
```

---

## Environment Variables

| Variable | Required | Description |
|----------|----------|-------------|
| `APOLLO_API_KEY` | ✅ | Apollo.io API key (Stage 1) |
| `PROSPEO_API_KEY` | ✅ | Prospeo API key (Stages 2 & 3) |
| `BREVO_API_KEY` | ✅ | Brevo SMTP API key (Stage 4) |
| `OPENAI_API_KEY` | ✅ | OpenAI key for email generation |
| `SENDER_NAME` | ✅ | Your name (From field) |
| `SENDER_EMAIL` | ✅ | Your verified Brevo sender email |
| `MAX_SIMILAR_COMPANIES` | ○ | Max companies from Apollo.io (default: 25) |
| `MAX_CONTACTS_PER_COMPANY` | ○ | Max DMs per company (default: 5) |
| `MAX_CONCURRENT_REQUESTS` | ○ | Parallel API calls (default: 10) |
| `PROSPEO_ENRICH_DELAY_SECONDS` | ○ | Pause between search & enrich (default: 90) |
| `LOG_LEVEL` | ○ | DEBUG/INFO/WARNING (default: INFO) |
| `OPENAI_MODEL` | ○ | GPT model (default: gpt-4o-mini) |

---

## Run Instructions

### Standard run

```bash
python main.py openai.com
```

### Dry run (all stages except sending emails)

```bash
python main.py stripe.com --dry-run
```

### Resume an interrupted run

```bash
python main.py stripe.com --resume
```

### More companies

```bash
python main.py notion.so --max-companies 50
```

### JSON output in addition to CSV

```bash
python main.py hubspot.com --json-output
```

### Full help

```bash
python main.py --help
```

---

## Demo Instructions (Live Interview)

1. Ensure `.env` is populated with real API keys
2. **Pre-warm (rate-limit workaround):**  
   Prospeo's free tier needs ~90 s between search and bulk enrich.
   Run a dry run 2–3 min before the interview to populate checkpoints:
   ```bash
   python main.py <seed-domain> --dry-run --max-companies 1
   ```
3. **During the interview**, resume so only Stage 3 fires after the delay:
   ```bash
   python main.py <seed-domain> --resume --max-companies 1
   ```
4. Walk through the terminal output:
   - Stage progress bars
   - Pipeline summary table
   - Safety checkpoint prompt
5. When ready to actually send:
   ```bash
   python main.py <seed-domain>
   ```
6. Check outputs:
   ```bash
   cat data/output.csv
   cat data/failures.json
   cat logs/pipeline.log | python -m json.tool | head -50
   ```

---

## Error Handling Strategy

The pipeline **never crashes** — every failure is isolated:

| Layer | Mechanism |
|-------|-----------|
| HTTP errors | `httpx` raises → mapped to typed exceptions |
| 429 / 5xx | Tenacity retries with exponential backoff |
| Repeated failures | Circuit breaker → fast-fail after 5 errors |
| One company fails | `asyncio.gather` continues remaining companies |
| No email found | Lead skipped; others continue |
| Email send fails | Recorded in CSV `email_sent=False` |
| Interrupted run | JSON checkpoint → `--resume` restarts from checkpoint |

---

## Scaling Strategy

- **Increase concurrency**: raise `MAX_CONCURRENT_REQUESTS` (bounded by API rate limits)
- **Multiple seeds**: wrap `orchestrator.execute()` in `asyncio.gather()`
- **Distributed execution**: replace asyncio with Celery + Redis broker
- **Large email volumes**: multiple Brevo sub-accounts with round-robin
- **Data storage**: swap CSV writer for PostgreSQL, BigQuery, or S3

---

## Running Tests

```bash
# All tests
pytest

# With coverage
pytest --cov=. --cov-report=html

# Unit tests only
pytest tests/test_unit.py -v

# Integration tests only
pytest tests/test_integration.py -v
```

---

## Future Improvements

- [ ] **Webhook callbacks** — Brevo open/click tracking → update CSV
- [ ] **Follow-up sequences** — schedule follow-up emails if no reply in N days
- [ ] **CRM integration** — push leads to HubSpot/Salesforce after Stage 2
- [ ] **A/B testing** — rotate 2+ email templates; track open rates
- [ ] **Domain warm-up** — throttle sends to new domains to protect deliverability
- [ ] **Web UI** — Flask/FastAPI dashboard for non-technical operators
- [ ] **Slack notifications** — post pipeline summary to a Slack channel on completion
