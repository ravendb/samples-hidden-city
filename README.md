# Hidden City Flight Search — RavenDB as Agent Working Memory

> **Conference PoC** — shows how RavenDB eliminates 95 %+ of LLM token spend and
> cloud egress cost in an AI flight-search agent. Use this repo as a sales demo
> and conversation-starter with customers running LLM workloads.

---

## The Problem

Airlines price connecting routes cheaper than direct legs — flying Warsaw → London
via New York (exit at London) can cost 62 % less than booking London directly.
Surfacing these *hidden city* opportunities requires querying three flight APIs,
cross-referencing prices, and explaining the risks — every single turn of the
conversation.

### What happens without a local memory layer

| Step | What the agent does | Cost |
|------|---------------------|------|
| User asks about flights | Agent calls Amadeus flight-offers API | 250 KB response, €0.04 egress |
| Agent summarises results for the LLM | Full JSON sent in the prompt | ~40 000 tokens ≈ $0.48 per turn |
| User asks a follow-up | Same API call again | Another 250 KB, another $0.48 |
| Price watch (polling) | CronJob hits API every 5 min | 288 API calls/day per route |

At 1 000 daily active users this is **~$480/day in LLM tokens alone** before any
infrastructure costs — and it scales linearly with usage.

---

## The Solution: RavenDB as In-Cluster Working Memory

RavenDB runs *inside* the Kubernetes cluster, a few milliseconds from the agent.
Every result that comes back from an external flight API is written once and read
many times from the local store. The LLM never sees a raw API response — it sees
a pre-structured 800-token tool result.

```
User → Agent (FastAPI) ──tool call──▶ search_routes ──▶ RavenDB (in-cluster)
                                                              │ cache hit → return
                                                              │ cache miss
                                                              ▼
                                                    Kiwi / Amadeus API
                                                    (write-back to RavenDB)

Price drops → RavenDB Subscription ──push──▶ Worker → alert
```

### Key savings

| Metric | Naive baseline | With RavenDB | Saving |
|--------|---------------|--------------|--------|
| Tokens per agent turn (cache hit) | 40 000–100 000 | ~1 500 | **96 %** |
| LLM cost per 1 000 turns (GPT-4o) | ~$480 | ~$18 | **$462/day** |
| Egress per turn (cache hit) | 250 KB | 0.1 KB | **99.96 %** |
| Egress cost per 1 000 turns\* | ~$40 | ~$0.02 | **$39.98** |
| External API calls (price watch) | 288/day/route | 0 (push) | **100 %** |

\*AWS us-east-1 egress pricing, $0.09/GB.

**How these numbers are calculated:**
- Token baseline: raw Amadeus `/v2/shopping/flight-offers` response for 3 offers
  across a popular route is ~40 000 tokens (JSON verbosity). We measured with
  `tiktoken` on actual API responses; see `scripts/measure_token_cost.py`.
- Token budget with RavenDB: system prompt 200 + user message 100 + tool result
  800 + assistant response 400 = 1 500 tokens. Capped in `src/agent/loop.py`.
- Egress: `curl -o /dev/null -w "%{size_download}"` on Amadeus vs a RavenDB
  document fetch. See `scripts/measure_egress.py`.
- Price watch: polling every 5 min = 288 calls/day. RavenDB Data Subscriptions
  push on change — zero polling.

---

## RavenDB Features Used

| Feature | Where | Why it matters |
|---------|-------|----------------|
| **Document store** | `src/db/models.py`, all tools | Schema-flexible JSON — route & session docs coexist, no migrations |
| **Bulk insert** | `src/scraper/run.py` | Ingest 10 000+ Travelpayouts routes in one network round-trip |
| **Optimistic concurrency** | `src/tools/save_conversation.py` | Multiple agent replicas can update the same session doc safely |
| **Data Subscriptions** | `src/worker/run.py` | Push-only price-drop alerts — no polling, no message broker needed |
| **Kubernetes Operator** | `k8s/operator/` | 3-node cluster declared as a CRD; scaling, failover, TLS handled automatically |
| **TTL index** (roadmap) | — | Stale route documents expire automatically without a housekeeping job |

---

## When to Show This to Customers

Show this demo to prospects who are:

- **Running LLM agents in production** and complaining about inference costs that
  scale with traffic (the token-budget story lands immediately).
- **Building RAG or agentic systems** where context retrieval is the bottleneck —
  RavenDB as the retrieval layer beats a vector-only store because it also handles
  structured queries, full-text search, and document updates in one product.
- **On Kubernetes** already — the Operator demo shows zero-ops cluster management.
- **Concerned about cloud egress** — any microservices shop paying AWS/GCP egress
  fees between an API gateway and a downstream service will recognise the pattern.
- **Replacing a mix of Redis + Postgres + a message broker** — show them that
  subscriptions replace Kafka for this workload, bulk insert replaces ETL tooling,
  and the document model replaces rigid schemas.

Avoid this demo for customers who are purely on-prem without Kubernetes, or who
have no LLM/AI workload yet — the hidden city domain will distract from the
database message.

---

## Four Infrastructure Stages (Evolutionary Path)

### Stage 1 — Laptop / No persistence  *(~2021)*
```
User → Python script → Amadeus API → print results
```
- Every run re-fetches everything from the internet.
- No memory between calls; no conversation history.
- Cost: Amadeus free tier, but 100 % cache-miss rate.
- **Pain:** 40 000+ tokens per query × every query. Demo breaks on API rate limits.

### Stage 2 — Shared Redis + Postgres  *(~2022–2023)*
```
User → Flask app → Redis (TTL cache) → Amadeus
                 → Postgres (conversation history)
                 → Celery worker (polling cron)
```
- Redis gives cache hits for repeated routes, Postgres stores sessions.
- Separate broker (RabbitMQ/Celery) for the price-watch cron.
- **Pain:** Three infrastructure components to operate. Redis evicts on memory
  pressure. Postgres schema migrations for every new field. Celery workers poll
  every 5 min regardless of whether prices changed. Egress cost unchanged.

### Stage 3 — Managed cloud search + vector DB  *(~2023–2024)*
```
User → FastAPI → Elasticsearch (routes) + pgvector (embeddings) + Redis
               → Kafka (price events)
```
- Full-text search on airport names, semantic search on user intent.
- Kafka for real-time price-drop events — finally no polling.
- **Pain:** Five infrastructure components. Cloud-managed costs $800+/month for
  HA. Egress from ES cluster to agent adds ~50 ms + 200 KB per call.
  Three separate query languages (ES DSL, SQL, Redis commands).

### Stage 4 — RavenDB in-cluster  *(this repo)*
```
User → FastAPI → RavenDB (routes + sessions + subscriptions)
               → Kiwi / Amadeus (cache-miss only)
```
- One database replaces Redis, Postgres, Elasticsearch, and Kafka.
- Subscriptions replace polling and the message broker entirely.
- RavenDB Operator manages the 3-node cluster as a single Kubernetes resource.
- **Result:** 96 % token reduction, 99.96 % egress reduction, 100 % fewer polling
  calls. One operator to upgrade, one monitoring target, one backup strategy.

---

## Prerequisites

The table below lists everything you need installed before running this repo.
The "Local dev" column covers the docker-compose path; the "Kubernetes" column
covers the full cluster deployment.

| Tool | Version | Local dev | Kubernetes | Install |
|------|---------|-----------|------------|---------|
| **Python** | 3.11–3.13 | required | required | [python.org](https://www.python.org/downloads/) — **3.14 not supported** (pyravendb dependency incompatibility) |
| **uv** | latest | required | required | see below |
| **Docker** | 24+ | required | — | [docs.docker.com](https://docs.docker.com/get-docker/) |
| **Docker Compose** | v2 (bundled with Docker Desktop) | required | — | bundled with Docker Desktop |
| **kubectl** | 1.28+ | — | required | [kubernetes.io](https://kubernetes.io/docs/tasks/tools/) |
| **Kubernetes cluster** | 1.28+ | — | required | kind / minikube / cloud provider |

> **Windows note:** Docker Desktop on Windows requires either WSL 2 or Hyper-V.
> Make sure one of these is enabled before installing Docker.

---

## Python Environment Setup (uv)

[uv](https://github.com/astral-sh/uv) replaces pip + venv in one fast tool.
Install it once, then use it for all Python dependency work in this repo.

### 1. Install uv

**Windows (winget):**
```powershell
winget install astral-sh.uv
```

**macOS / Linux:**
```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
```

**Fallback (pip):**
```bash
pip install uv
```

### 2. Create a virtual environment and install dependencies

Run these once from the repo root:

```bash
uv venv                        # creates .venv in the repo root
uv pip install -e ".[dev]"     # installs the package + all dev deps (pytest, ruff, …)
```

### 3. Activate the environment

**Windows (PowerShell):**
```powershell
.venv\Scripts\Activate.ps1
```

**macOS / Linux:**
```bash
source .venv/bin/activate
```

After activation your prompt will show `(hidden-city)` and all `python` /
`uvicorn` / `pytest` commands will use the repo's isolated environment.

### Everyday uv commands

```bash
uv pip install <package>          # add a dependency
uv pip install -e ".[dev]"        # re-sync after editing pyproject.toml
uv pip list                       # list installed packages
```

---

## How to Run It

### Local (docker-compose)

The fastest way is the included start script — it handles `.env`, RavenDB health
checks, seeding, and launching the agent in one command:

```powershell
.\start.ps1             # RavenDB + seed + agent
.\start.ps1 -Worker     # also starts the price-drop worker in a separate window
.\start.ps1 -SkipSeed   # skip seeding when the database is already populated
```

Or step by step:

```bash
# 1. Copy environment variables file (only needed once)
cp .env.example .env

# 2. Start RavenDB
docker compose up -d ravendb

# 3. Seed airports and fixture routes
uv run python -m scripts.seed_local

# 4. Start the agent API
uv run uvicorn src.agent.app:app --reload --port 8000

# 5. (Optional) Start the price-drop subscription worker
uv run python -m src.worker.run
```

The RavenDB Studio is available at http://localhost:8080 — no credentials needed
in local mode. Open the `Routes` collection to inspect enriched documents.

### Environment variables

Copy `.env.example` to `.env` and fill in:

```
RAVENDB_URL=http://localhost:8080
RAVENDB_DATABASE=HiddenCity
ANTHROPIC_API_KEY=sk-...
KIWI_API_KEY=...          # optional — only for live price lookup
AMADEUS_CLIENT_ID=...     # optional — only for live price lookup
AMADEUS_CLIENT_SECRET=... # optional — only for live price lookup
```

Without Kiwi/Amadeus keys the agent uses fixture data seeded by `seed_local.py`.

### Kubernetes

```bash
# Install RavenDB Operator (one-time)
bash k8s/operator/install.sh

# Deploy everything
kubectl apply -k k8s/

# Check status
kubectl -n hidden-city get pods
```

---

## Testing the Agent

### Run the test suite

```bash
uv run pytest tests/unit/ -v          # 50 unit tests, no external deps
uv run pytest tests/integration/ -v  # requires running RavenDB
```

### Interactive chat via the REST API

With the agent running on port 8000, send a chat message:

```bash
curl -s -X POST http://localhost:8000/chat \
  -H "Content-Type: application/json" \
  -d '{
    "user_id": "demo",
    "session_id": "1",
    "message": "Find me cheap flights from Warsaw to Chongqing with at most 1 stop"
  }' | python -m json.tool
```

Or use the Swagger UI at http://localhost:8000/docs.

#### Example: Warsaw → Chongqing (CKG) with max 1 stop

Chongqing (IATA: `CKG`) is not in the fixture data, so the agent will:
1. Call `search_routes(origin="WAW", destination="CKG")` — returns empty.
2. Call `get_live_price(origin="WAW", destination="CKG", route_type="hidden_city")`
   — hits Kiwi Tequila if `KIWI_API_KEY` is set, otherwise returns `found: false`.
3. Explain that no routes were found and suggest nearby hubs (IST, DOH, DXB are
   common transfer points for Central Asia).

With fixture data, try Warsaw → JFK (exits at London) as a hidden city example:

```bash
curl -s -X POST http://localhost:8000/chat \
  -H "Content-Type: application/json" \
  -d '{
    "user_id": "demo",
    "session_id": "1",
    "message": "Any hidden city options from Warsaw? I only have carry-on luggage."
  }' | python -m json.tool
```

The agent will find WAW→JFK (via LHR) at ~$220 vs WAW→LHR direct at $580 — a
62 % saving — and surface it with a `score: 0.62`. With carry-on only it will
note the checked-baggage risk is eliminated, improving the adjusted score.

---

## Project Structure

```
src/
  agent/          FastAPI app + Anthropic tool-calling loop
  tools/          4 MCP-style tools: search_routes, get_live_price,
                  get_user_profile, save_conversation
  db/             RavenDB client + Pydantic models
  hidden_city/    Scoring algorithm + enricher
  scraper/        Travelpayouts bulk ingest (CronJob)
  worker/         RavenDB Subscription price-drop worker
data/
  airports.json   15 airports (WAW, LHR, FRA, JFK, …)
scripts/
  seed_local.py   Seed fixture data for local dev
  measure_*.py    Token / egress cost measurement
k8s/              Kubernetes manifests + RavenDB Operator config
tests/
  unit/           50 tests, no external dependencies
  integration/    Requires live RavenDB
```

---

## Legal Note

This project demonstrates the *detection* of hidden city pricing patterns for
educational purposes. It does not automate booking, generate itineraries, or
interact with airline reservation systems. Consult an airline's terms of service
before applying hidden city strategies to real bookings.
