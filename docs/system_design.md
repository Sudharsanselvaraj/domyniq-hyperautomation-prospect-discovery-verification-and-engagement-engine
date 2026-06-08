# System Design: DOMYNIQ Pipeline

## 1. High-Level Architecture

```
┌─────────────────────────────────────────────────────────────────────┐
│                         CLI Entry Point (main.py)                   │
│   typer CLI  ──  Rich UI  ──  Safety Checkpoint  ──  CSV/JSON out   │
└───────────────────────────────┬─────────────────────────────────────┘
                                │
                    ┌───────────▼────────────┐
                    │  PipelineOrchestrator   │  (services/orchestrator.py)
                    │  - Stage coordination   │
                    │  - Async parallelism    │
                    │  - Error isolation      │
                    │  - Checkpoint/resume    │
                    └──────────┬─────────────┘
                               │
          ┌─────────────────┬──┴──────────────┬──────────────────┐
          │                 │                 │                  │
    ┌─────▼──────┐   ┌──────▼─────┐  ┌───────▼──────┐  ┌───────▼──────┐
    │ OceanClient│   │ProspeoClient│  │EazyReachClient│  │  BrevoClient │
    │ (Stage 1)  │   │ (Stage 2)  │  │  (Stage 3)   │  │  (Stage 4)   │
    └─────┬──────┘   └──────┬─────┘  └───────┬──────┘  └───────┬──────┘
          │                 │                 │                  │
    ┌─────▼──────────────────▼─────────────────▼──────────────────▼─────┐
    │                      BaseClient                                     │
    │  httpx.AsyncClient  ──  Tenacity retry  ──  Circuit Breaker        │
    └────────────────────────────────────────────────────────────────────┘
```

---

## 2. Sequence Diagram

```
User        main.py       Orchestrator    Ocean.io    Prospeo    EazyReach   OpenAI    Brevo
 │             │               │              │           │           │          │        │
 │ domain ─── ▶              │              │           │           │          │        │
 │             │ execute() ─ ▶              │           │           │          │        │
 │             │               │──── POST /v1/similar ─▶           │           │        │
 │             │               │◀── companies list ────            │           │        │
 │             │               │ (dedup)        │           │           │          │        │
 │             │               │──── POST /domain-search ──▶        │           │        │
 │             │               │                   ◀── contacts ───           │          │        │
 │             │               │ (dedup)        │           │           │          │        │
 │             │               │─────────────────── POST /email ──▶           │          │        │
 │             │               │◀──────────────────── verified emails ────               │        │
 │             │               │ (dedup)        │           │           │          │        │
 │             │ ◀── result ───                              │           │          │        │
 │ [summary] ◀─              │              │           │           │          │        │
 │ y/n ──────▶               │              │           │           │          │        │
 │             │ send_emails()─▶              │           │           │          │        │
 │             │               │────────────────────────────────── generate() ─▶        │
 │             │               │◀───────────────────────────────── subject,body ─────── │        │
 │             │               │────────────────────────────────────────── POST /smtp ─▶│
 │             │               │◀───────────────────────────────────────── message_id ──│
 │ [done] ◀────              │              │           │           │          │        │
```

---

## 3. Data Flow

| Stage | Input | Output | Volume |
|-------|-------|--------|--------|
| 1 Ocean.io | 1 domain | ≤25 company domains | 1 API call |
| 2 Prospeo | 25 domains | ≤125 contacts (5/company) | 25 calls (parallel) |
| 3 EazyReach | 125 LinkedIn URLs | ≤125 verified emails | 125 calls (parallel) |
| 4 OpenAI + Brevo | emails + names | email sent | 2 calls/lead = 250 calls |

**Deduplication applied at:**
- After Stage 1: by domain (e.g., ocean returns same domain twice)
- After Stage 2: by LinkedIn URL (same exec found at multiple companies)
- After Stage 3: by email address (belt-and-suspenders)

---

## 4. Failure Handling

| Failure Scenario | Behaviour |
|-----------------|-----------|
| Ocean.io returns 0 companies | Pipeline exits cleanly with empty report |
| One company's Prospeo lookup fails | Log + continue; other companies unaffected |
| One contact has no verified email | Skip lead; others continue |
| OpenAI email generation fails | Use hardcoded fallback copy; still send |
| Brevo send fails for one email | Log failure; continue rest; record in CSV |
| Any API returns 5xx | Tenacity retries up to 3× with exponential backoff |
| API returns 429 | Same retry + circuit breaker opens after 5 failures |
| Run interrupted mid-stage | Checkpoint saves state; `--resume` restarts from checkpoint |

---

## 5. Rate Limiting Strategy

**Two complementary mechanisms:**

1. **Semaphore-based concurrency cap** (`max_concurrent_requests=10`)
   - Limits simultaneous in-flight requests
   - Prevents API quota exhaustion even without hitting 429

2. **Tenacity retry on 429**
   - `wait_exponential(min=1s, max=60s)` with jitter
   - Each retry doubles the wait time
   - After `max_attempts` (default 3) the call is marked failed

3. **Circuit breaker**
   - After 5 consecutive failures → OPEN (fast-fail all calls for 60s)
   - Prevents cascading quota burn during a complete API outage

---

## 6. Scaling Strategy

| Dimension | Current | Horizontal Scale Path |
|-----------|---------|----------------------|
| Companies | 25 | Increase `--max-companies`; async gather scales naturally |
| Concurrent requests | 10 | Tune `MAX_CONCURRENT_REQUESTS` env var |
| Parallelism unit | asyncio coroutine | Move to Celery tasks for multi-machine |
| Storage | local CSV | Swap CSV writer for PostgreSQL or S3 writer |
| Multiple seeds | one at a time | Wrap `execute()` in `asyncio.gather()` |
| Email volume | one Brevo API key | Multiple Brevo sub-accounts + round-robin |

For truly large-scale operation (>10K leads/day):
- Decouple stages with a message queue (Redis Streams, Kafka)
- Each stage becomes a consumer group
- Checkpointing becomes offset tracking

---

## 7. Tradeoffs

| Decision | Chosen | Alternative | Reason |
|----------|--------|-------------|--------|
| HTTP client | httpx (async) | requests (sync) | Parallelism is essential for Stage 2/3 |
| Retry lib | Tenacity | manual loop | Tenacity handles edge cases (jitter, predicates) better |
| Config | pydantic-settings | raw os.environ | Type validation + .env support in one library |
| CLI | Typer + Rich | Click + colorama | Typer is type-hint-native; Rich is best-in-class for terminal UI |
| Email gen | OpenAI JSON mode | template strings | Personalisation quality + easy to swap model |
| Models | Pydantic v2 | dataclasses | Validation, serialisation, and JSON export built in |
| Concurrency | asyncio gather | threading | GIL-free for I/O; simpler mental model than threads |
| Resume | JSON checkpoint | Redis | Zero infrastructure requirement; good enough for single-machine |

---

## 8. Security Considerations

- API keys live only in `.env` (never hardcoded, never logged)
- `.gitignore` excludes `.env` and output data
- Brevo sender email must be domain-verified to prevent spoofing
- Rate limiting prevents accidental quota exhaustion
- Safety checkpoint requires explicit human confirmation before sends
