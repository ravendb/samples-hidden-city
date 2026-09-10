# RavenDB as In-Cluster Working Memory for LLM Agents (K8s-Native)

## Project Goal

Demo showing that RavenDB co-located with inference pods eliminates the two costs
nobody calculates upfront: egress traffic leaving the cluster, and token payload
inflation on every retrieval call. The agent sends one thing outbound: the user's
prompt. All context lives inside the cluster.

Use case: conversational flight search with hidden city detection. The agent
reasons over price history, route data, and user preferences — entirely locally.

See @docs/architecture.md for the evolutionary cost narrative (naive → optimized).

## The Core Problem

AI models running on-prem or on-cluster are stateless by nature. Feeding them
context from outside the cluster means paying twice:

1. **Egress cost** — every megabyte of context leaving the cluster is billed
2. **Token cost** — every token in the payload is billed by the inference provider

At 10k requests/day with fat context payloads, this is a budget line that grows
with every user. RavenDB inside the cluster makes retrieval a local call.

## Architecture

### Data Flow

```
Travelpayouts ──(CronJob, 6h)──→ RavenDB (in-cluster)
                                      ↑ ↑
Travelpayouts  ←──(cache miss)── Agent │ └── tool results
                                      │
User → Chat UI ──────────────────→ Agent (FastAPI)
                                      │
                              only prompt ↓
                               OpenAI API
```

The agent calls RavenDB as an **LLM tool** — the model decides what to retrieve
and when. Nothing is blindly injected. Only the user's prompt leaves the cluster.

### Three Data Paths

| Scenario     | What happens                                                         | Egress |
|--------------|----------------------------------------------------------------------|--------|
| Cache hit    | Route + price already in RavenDB, agent retrieves locally            | Zero   |
| Cache miss   | Agent triggers Travelpayouts live call, result written to RavenDB,   | One    |
| Price watch  | RavenDB Subscription pushes price change to agent — no polling       | Zero   |

## Stack

- **Runtime**: Kubernetes (k8s)
- **Database**: RavenDB — documents + vector search + attachments in one product
- **Operator**: RavenDB Kubernetes Operator (`RavenDBCluster` CRD)
- **Agent**: Python + FastAPI
- **ETL**: RavenDB Subscriptions (Kafka only if multi-source ingestion needed)
- **Flight APIs**: Travelpayouts — bulk cached prices (CronJob, every 6h) and live price lookup on cache miss
- **Language**: Python (agent, tools, scraper, worker), C# or Python (RavenDB client)
- **LLM**: gpt-4o-mini via OpenAI API — only the user prompt goes outbound

## Repo Structure

```
/
├── CLAUDE.md
├── docs/
│   ├── architecture.md       # Evolutionary cost narrative + diagrams
│   └── hidden-city.md        # Hidden city detection spec
├── k8s/
│   ├── operator/             # RavenDBCluster CRD + operator install
│   ├── ravendb/              # RavenDBCluster manifest
│   ├── scraper/              # CronJob: Travelpayouts → RavenDB bulk (every 6h)
│   ├── worker/               # Subscription worker: price drop push alerts
│   └── agent/                # FastAPI agent deployment
├── src/
│   ├── db/                   # DocumentStore client, seed/bootstrap, expiration, geo/vector helpers
│   ├── scraper/              # Travelpayouts bulk ingest (CronJob)
│   ├── worker/               # RavenDB subscription listener + alert push
│   ├── agent/                # FastAPI app + LLM agent loop
│   ├── tools/                # Tool implementations (search_routes, get_live_prices, …)
│   ├── hidden_city/          # Hidden city scoring logic
│   └── chat/                 # Chat UI
├── versions.env              # Single source of truth for every pinned tool/image/manifest version
└── tests/
    ├── unit/
    └── integration/
```

## Key Commands

```bash
# One-command onboarding (kind cluster from scratch: kind, cert-manager, ingress-nginx,
# operator, RavenDB cluster, app, all versions pinned in versions.env). Prompts
# interactively for secrets/certs on first run; add flags to skip steps on re-runs.
.\start.ps1                              # Windows: picks Local (docker-compose) or K8s (kind) interactively
.\start.ps1 -Mode K8s                    # Windows: K8s mode directly, forwards to start-k8s.ps1
.\start-k8s.ps1 -SkipBuild -SkipOperator # Windows: fast re-run, skips image rebuild + operator reinstall
bash k8s/start-k8s.sh                    # macOS/Linux (or WSL2 on Windows): same flow, --skip-build/--skip-operator/--delete-cluster

# Local mode (docker-compose stack: venv + deps via uv, RavenDB, seed, agent)
.\start.ps1 -Mode Local                  # Windows
bash start-local.sh                      # macOS/Linux (or WSL2 on Windows): same flow, --skip-seed/--worker

# Local dev (manual steps -- what start.ps1 -Mode Local / start-local.sh automate)
docker-compose up -d ravendb
python -m scripts.seed_local          # seed airports + fixture routes
uvicorn src.agent.app:app --reload --port 8001    # start agent on :8001

# Check operator + cluster state
kubectl get ravendbclusters
kubectl describe ravendbclusters ravendb-cluster

# Deploy onto an ALREADY-EXISTING cluster (any cluster, not just kind -- no kind
# provisioning, no cert generation; assumes k8s/secrets.local.yaml and the RavenDB
# license/cert secrets already exist -- see k8s/operator/install.sh)
cp k8s/secrets.yaml k8s/secrets.local.yaml && vim k8s/secrets.local.yaml
bash k8s/deploy.sh                      # full deploy with progress output
bash k8s/deploy.sh --skip-operator      # re-deploy without reinstalling the operator
bash k8s/deploy.sh --skip-build         # re-deploy without rebuilding the Docker image

# Run tests
pytest tests/unit/
pytest tests/integration/ --require-ravendb

# Measure token + egress cost per request
python scripts/measure_tokens.py
python scripts/measure_egress.py
```

## Data Model

RavenDB stores three types of data in one cluster — this is a key demo point.
Competing stacks need a separate vector DB + document DB + blob store.

### Documents

**Route document** (refreshed hourly via ETL):

```json
{
  "id": "routes/WAW-JFK-1",
  "origin": "WAW",
  "destination": "JFK",
  "hubs": ["LHR", "FRA", "AMS"],
  "typical_price": { "min": 2400, "max": 5800, "currency": "PLN" },
  "duration_avg_min": 600,
  "hidden_city_score": 0.82,
  "last_updated": "2025-05-20T10:00:00Z",
  "@metadata": { "@expires": "<TTL 20 min for price fields>" }
}
```

**Conversation session** (durable, survives pod restarts, partially queryable):

```json
{
  "id": "sessions/user-42-sess-7",
  "user_id": "user-42",
  "turns": [
    { "role": "user", "content": "Find hidden city WAW→JFK next Friday" },
    { "role": "assistant", "content": "..." }
  ],
  "active_constraints": { "carry_on_only": true, "max_stops": 1 },
  "last_active": "2025-05-20T10:05:00Z"
}
```

Stored as a document (not attachment) so `active_constraints` and `last_active`
are indexable and the agent can retrieve only last N turns without reading the
full history. Binary user data (passport, photos, bag files) lives as attachments
on the separate `users/...` document.

**Airport document** (static):

```json
{
  "id": "airports/WAW",
  "iata": "WAW",
  "name": "Warsaw Chopin",
  "coordinates": { "lat": 52.1657, "lng": 20.9671 },
  "nearby": [
    { "iata": "WMI", "distance_km": 35, "train": false },
    { "iata": "KRK", "distance_km": 290, "train": true }
  ]
}
```

### Vectors

Airport documents carry a `location_vector` (a 3D unit-vector projection of
lat/lng — see `src/db/geo.py`), used by `search_routes`'s vector-search
fallback to find geographically nearby airports when no direct route exists,
without a hand-curated "nearby airports" list. Requires RavenDB 7.0+ (native
vector search did not exist before) — see `src/db/client.py` and the modern
`ravendb` Python client. Stored as a field on the same document — no separate
vector DB needed.

Route-level semantic similarity search ("something like LHR but cheaper",
"fastest Europe→Asia hub") is a distinct, not-yet-built idea — would need
route embeddings stored as vector fields on route documents. Not implemented.

### Attachments

Binary blobs stored on the user document: passport scan, bag photo, and an
exported preference sheet (PDF — e.g. a bucket list). Stored as RavenDB
attachments (`src/tools/user_attachments.py`) — not queryable, not indexed,
uploaded/retrieved whole via the Profile screen (`/profile`) and its
`/api/profile/attachment*` endpoints, not via the `get_user_profile` LLM tool.

## RavenDB as Agent Tool

IMPORTANT: Do NOT use sidecar injection to blindly push context into every prompt.
The model must call RavenDB explicitly as a tool — it decides what to fetch.

One narrow, documented exception: `_try_fast_path()` in `src/agent/loop.py`
pre-resolves an unambiguous "from X to Y" message via
`src/tools/resolve_airports.py` (RavenDB full-text search over `Airports`,
using an auto-created index — see README's "RavenDB Features Used") and
pre-fetches `search_routes` itself, cutting the turn from two OpenAI calls to
one (~2100 tokens → ~750 measured). This is *not* a blanket sidecar — it only
fires when both airports resolve to exactly one match each; anything
ambiguous or unmatched returns `None` and falls back to the model calling
`search_routes` itself, unchanged. Do not widen this pattern to other tools
without the same strict "exact match or fall back" discipline.

### Tool Definitions (`src/tools/`)

| Tool                 | Description                                                              |
|----------------------|--------------------------------------------------------------------------|
| `search_routes`      | Doc query against RavenDB: origin, dest, date, stops, price, hidden city hubs. If no direct route exists: tries a connecting-hub search (origin→X→destination via cached routes) first, then falls back to vector search over airport `location_vector` for geographically nearby airports |
| `get_live_prices`    | Live call to Travelpayouts on cache miss — single route, or several cheapest destinations from an origin when destination is omitted ("anywhere from home") — writes back to RavenDB |
| `get_user_profile`   | Read user profile and preferences from RavenDB attachments               |
| `update_constraints` | Save a trip-specific constraint (carry-on only for this trip, max stops) — called only when the user states one |

Turn persistence itself (`persist_turn` in `src/tools/save_conversation.py`) is
NOT an LLM tool — it is called directly by `src/agent/app.py` after the model's
final response, using the response text the server already has. Routing it
through a tool call would force the model to restate its full answer as a tool
argument before saying it again as the reply, doubling output tokens and
adding a full extra round trip for zero benefit.

Hidden city scoring and nearby-airport vector search are not separate tools —
they are logic inside `search_routes` (hidden city score is a field on the
route document, filtered at query time; nearby-airport lookup is a
vector_search query against airport `location_vector`, used only as a
fallback when no direct or connecting route is found).

### What Goes Outbound

```
Outbound payload = system_prompt + user_message + tool_results
                 ≈ 200 + 100 + 1800 tokens ≈ 2500 tokens max
```

Raw Travelpayouts bulk response (typically 40k–100k tokens) never reaches the model.

## Hidden City Detection Logic

See @docs/hidden-city.md for full spec. Summary:

- Compare price of A→B→C vs A→B direct
- If A→B→C cheaper and user's real destination is B: flag as hidden city candidate
- Store pattern in RavenDB with `hidden_city_score` and risk metadata
- NEVER automate booking of hidden city tickets — display as information only

## Kubernetes Operator

The `RavenDBCluster` CRD (from https://github.com/ravendb/ravendb-operator) is
the contract. The Operator is the enforcer. Installed via Helm — see
`k8s/operator/install.sh` — not applied as a raw manifest.

```yaml
apiVersion: ravendb.ravendb.io/v1
kind: RavenDBCluster
metadata:
  name: ravendb-cluster
spec:
  nodes:
    - tag: a
      publicServerUrl: https://a.hiddencity.local:443
      publicServerUrlTcp: tcp://a-tcp.hiddencity.local:443
    - tag: b
      publicServerUrl: https://b.hiddencity.local:443
      publicServerUrlTcp: tcp://b-tcp.hiddencity.local:443
    - tag: c
      publicServerUrl: https://c.hiddencity.local:443
      publicServerUrlTcp: tcp://c-tcp.hiddencity.local:443
  mode: None  # self-signed via *CertSecretRef fields; use LetsEncrypt for a public demo
  storage:
    data:
      size: 50Gi
```

See `k8s/ravendb/values.yaml` for the full chart values (cert/license secret
refs, ingress config) actually used to deploy this cluster.

The Operator handles: bootstrapping (via a one-shot cluster-bootstrapper Job),
certificate wiring, rolling node upgrades with safety gates (node-by-node,
halts on failed gates, resumes automatically once fixed, blocks downgrades),
and continuous reconciliation against declared state. Admission webhooks block
invalid configs before any damage is done. Note: initial node topology is fixed
at bootstrap — adding/removing nodes later is a manual operation, not automatic.

Contact Omer for operator internals — he wrote it.

## LLM Configuration

- **Model**: `gpt-4o-mini` via OpenAI API
- **What goes outbound**: system prompt + user message only — no context payload
- **Context delivery**: via tool results returned in the same API call, never pre-injected
- Do not switch models mid-session (breaks conversation continuity)
- Max tokens into the API call: 2500 (system + user + all tool results combined)

If you have a GPU in the cluster and want zero-egress inference, llm-d/vLLM is
the drop-in replacement — the agent tool interface does not change.

## Token Budget per Request

| Component           | Max tokens |
|---------------------|------------|
| System prompt       | 200        |
| User message        | 100        |
| Tool results total  | 1800       |
| LLM response        | 400        |
| **Total**           | **~2500**  |

This is the core demo metric. Enforce it, measure it, show it on screen.
The naive baseline (raw Travelpayouts response stuffed into prompt) runs 40k–100k tokens per call.

## Conversation Memory

Conversation state is a RavenDB document (`sessions/...`), not pod RAM, not Redis.

- Persists across pod restarts — demo beat: kill the agent pod mid-conversation, query resumes
- `active_constraints` (carry-on only, max stops, etc.) are indexed fields, not buried in turn text
- `persist_turn` appends each turn after the model responds — called directly by the FastAPI
  handler, not through an LLM tool call (see "RavenDB as Agent Tool" above)
- At the start of each turn the agent reads last N turns + `active_constraints` only — never the full raw history
- Binary user data (passport scan, bag photo) lives as attachments on `users/...` document, fetched whole by `get_user_profile`

## Code Style

- Python: type hints always, Pydantic models for all inter-service boundaries
- No raw dicts passed between agent, tools, and memory — typed models only
- All k8s manifests in `/k8s/`, never inline `kubectl` calls in Python code
- Config via k8s ConfigMaps and Secrets — never hardcoded, never in `.env` files
  committed to the repo
- RavenDB Subscriptions over polling loops everywhere (price watch, ETL triggers)

## Testing Requirements

- Unit: token budget enforcement — MUST assert total tool result output < 1800 tokens
- Unit: hidden city detection scoring logic
- Unit: conversation memory truncation (only last N turns passed to model)
- Integration: RavenDB tool `search_routes` returns correct results for known fixtures
- Integration: vector-search nearby-airport lookup in `search_routes` returns geographically relevant airports
- Integration: session document survives simulated pod restart
- Always run `pytest tests/unit/` before committing

## What NOT to Do

- Never pass raw Travelpayouts API response to the model — always filter through tools
- Never hardcode API keys or credentials — use k8s Secrets
- Never use sidecar injection to blindly push context into every prompt — agent tools only
- Never store conversation memory in pod RAM or a Redis sidecar — use RavenDB
- Never automate hidden city booking flow — legal risk, display only
- Never use `--force` on kubectl in production manifests
- Do not split the data layer into a separate vector DB + document DB — that's the problem we're solving
- Do not add Kafka until RavenDB Subscriptions prove insufficient for the ingestion volume

## Key Metrics to Track (Demo)

| Metric                        | Naive baseline               | With RavenDB in-cluster     |
|-------------------------------|------------------------------|-----------------------------|
| Tokens per API call           | 40k–100k                     | ~2500                       |
| OpenAI API cost/day (10k req) | Calculate and show on screen | Calculate and show on screen|
| Retrieval egress              | Full payload leaves cluster  | Zero (local RavenDB query)  |
| p95 latency (retrieval)       | External API round trip      | In-cluster query time       |
| Cache hit rate                | N/A                          | Measure after 24h of data   |

See @scripts/measure_tokens.py and @scripts/measure_egress.py for tooling.

## Evolutionary Narrative (Demo Script Context)

The presentation shows the same use case rebuilt across three eras — each with a
concrete cost breakdown. RavenDB appears as the final step, not the starting
assumption. LLM inference stays external (OpenAI API) throughout all three
stages — the savings come from what we stop sending, not from where inference runs.

1. **No k8s, naive DB** — full Travelpayouts response stuffed into every prompt, paying for 100k tokens/call, high OpenAI bill
2. **K8s, still external DB** — containerized but retrieval still crosses cluster boundary on every request
3. **K8s + RavenDB in-cluster via Operator** — retrieval is local, 2500 tokens/call to OpenAI, near-zero egress on context

Each stage is costed live. The closing slide leaves the architecture open:
"We kept inference external to show you the savings are purely about context —
if you have a GPU in your cluster, llm-d takes it the rest of the way."

See @docs/architecture.md for per-stage cost estimates and diagrams.
