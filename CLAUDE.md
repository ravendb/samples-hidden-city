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
Amadeus API → ETL Pod → RavenDB ←──tool call──┐
                                               │
                            User → Chat UI → Agent → LLM-d (in-cluster)
                                               │
                                         RavenDB (tool result)
```

The agent calls RavenDB as an **LLM tool** — the model decides what to retrieve
and when. Nothing is blindly injected. Only the user prompt goes outbound to the
inference pod (which is also in-cluster via LLM-d/vLLM).

### Three Data Paths

| Scenario     | What happens                                                         | Egress |
|--------------|----------------------------------------------------------------------|--------|
| Cache hit    | Route + price already in RavenDB, agent retrieves locally            | Zero   |
| Cache miss   | Agent triggers live Amadeus call, result written to RavenDB, served  | One    |
| Price watch  | RavenDB Subscription pushes price change to agent — no polling       | Zero   |

## Stack

- **Runtime**: Kubernetes (k8s)
- **Inference**: LLM-d + vLLM (prefill/decode disaggregation, GPU-targeted)
- **Database**: RavenDB — documents + vector search + attachments in one product
- **Operator**: RavenDB Kubernetes Operator (`RavenDBCluster` CRD)
- **ETL**: RavenDB Subscriptions (Kafka only if multi-source ingestion needed)
- **Flight API**: Amadeus (sandbox for dev, production for prod)
- **Language**: Python (ETL, agent, tools), C# or Python (RavenDB client)
- **LLM**: claude-sonnet-4-20250514 via Anthropic API (or self-hosted via LLM-d)

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
│   ├── llm-d/                # Inference pod config (prefill + decode)
│   ├── etl/                  # Amadeus ETL job
│   └── agent/                # Chat agent deployment
├── src/
│   ├── etl/                  # Amadeus → RavenDB pipeline
│   ├── agent/                # LLM agent + tool definitions
│   ├── tools/                # RavenDB tool implementations
│   ├── memory/               # Conversation memory (RavenDB-backed)
│   ├── hidden_city/          # Hidden city detection logic
│   └── chat/                 # Chat UI
└── tests/
    ├── unit/
    └── integration/
```

## Key Commands

```bash
# Local dev
docker-compose up -d ravendb
python src/etl/run.py --env dev

# Check operator + cluster state
kubectl get ravendbclusters
kubectl describe ravendbclusters ravendb-cluster

# Deploy everything
kubectl apply -f k8s/operator/
kubectl apply -f k8s/

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

**Conversation session** (durable agent memory, survives pod restarts):

```json
{
  "id": "sessions/user-42-sess-7",
  "user_id": "user-42",
  "turns": [
    { "role": "user", "content": "Find hidden city WAW→NYC next Friday" },
    { "role": "assistant", "content": "..." }
  ],
  "active_constraints": { "carry_on_only": true, "max_stops": 1 },
  "last_active": "2025-05-20T10:05:00Z"
}
```

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

Route embeddings for semantic similarity search ("something like LHR but cheaper",
"fastest Europe→Asia hub"). Stored as vector fields on route documents — no
separate vector DB needed.

### Attachments

User profile attached to the user document: passport scan (for name/nationality
validation), saved preferences, loyalty program numbers. Stored as RavenDB
attachments — binary blobs alongside the document, managed by the same Operator.

## RavenDB as Agent Tool

IMPORTANT: Do NOT use sidecar injection to blindly push context into every prompt.
The model must call RavenDB explicitly as a tool — it decides what to fetch.

### Tool Definitions (`src/tools/`)

| Tool                 | Description                                                              |
|----------------------|--------------------------------------------------------------------------|
| `search_routes`      | Vector + doc query against RavenDB: origin, dest, date, stops, price, semantic similarity |
| `get_live_price`     | Live call to Kiwi Tequila (hidden city) or Amadeus (direct) on cache miss |
| `get_user_profile`   | Read user profile and preferences from RavenDB attachments               |
| `save_conversation`  | Persist the current turn to RavenDB after each exchange                  |

Hidden city scoring and semantic similarity are not separate tools — they are
logic inside `search_routes` (vector query handles similarity; hidden city score
is a field on the route document, filtered at query time).

### What Goes Outbound

```
Outbound payload = system_prompt + user_message + tool_results
                 ≈ 200 + 100 + 800 tokens ≈ 1500 tokens max
```

Raw Amadeus response (typically 40k–100k tokens) never reaches the model.

## Hidden City Detection Logic

See @docs/hidden-city.md for full spec. Summary:

- Compare price of A→B→C vs A→B direct
- If A→B→C cheaper and user's real destination is B: flag as hidden city candidate
- Store pattern in RavenDB with `hidden_city_score` and risk metadata
- NEVER automate booking of hidden city tickets — display as information only

## Kubernetes Operator

The `RavenDBCluster` CRD is the contract. The Operator is the enforcer.

```yaml
apiVersion: ravendb.com/v1alpha1
kind: RavenDBCluster
metadata:
  name: ravendb-cluster
spec:
  nodes: 3
  storage: 50Gi
  tlsMode: ClusterExternalAccess
```

The Operator handles: bootstrapping, certificate wiring, rolling node upgrades
with Raft quorum gates (never loses quorum during upgrade), and continuous
reconciliation against declared state. Admission webhooks block invalid configs
before any damage is done.

Contact Omer for operator internals — he wrote it.

## LLM-d Configuration

- Prefill pods: scale based on prompt size (stays small because context is filtered)
- Decode pods: scale based on concurrent users
- Target GPU: point llm-d at the GPU node pool via `nodeSelector` / tolerations
- Model: do not switch models mid-session (breaks conversation continuity)
- Max tokens into model: 1500 (system + user + all tool results combined)

## Token Budget per Request

| Component           | Max tokens |
|---------------------|------------|
| System prompt       | 200        |
| User message        | 100        |
| Tool results total  | 800        |
| LLM response        | 400        |
| **Total**           | **~1500**  |

This is the core demo metric. Enforce it, measure it, show it on screen.
The naive baseline (raw Amadeus response in prompt) runs 40k–100k tokens per call.

## Conversation Memory

Conversation state is stored in RavenDB, not in pod RAM and not in Redis.

- Session documents persist across pod restarts (demo: kill the agent pod, query continues)
- Active constraints from earlier in the conversation are carried forward automatically
- The `get_conversation` tool is called at the start of each turn — the model
  receives only the last N turns + extracted constraints, not the full raw history

## Code Style

- Python: type hints always, Pydantic models for all inter-service boundaries
- No raw dicts passed between agent, tools, and memory — typed models only
- All k8s manifests in `/k8s/`, never inline `kubectl` calls in Python code
- Config via k8s ConfigMaps and Secrets — never hardcoded, never in `.env` files
  committed to the repo
- RavenDB Subscriptions over polling loops everywhere (price watch, ETL triggers)

## Testing Requirements

- Unit: token budget enforcement — MUST assert total tool result output < 800 tokens
- Unit: hidden city detection scoring logic
- Unit: conversation memory truncation (only last N turns passed to model)
- Integration: RavenDB tool `search_routes` returns correct results for known fixtures
- Integration: `find_similar_routes` vector search returns semantically relevant routes
- Integration: session document survives simulated pod restart
- Always run `pytest tests/unit/` before committing

## What NOT to Do

- Never pass raw Amadeus API response to the model — always filter through tools
- Never hardcode API keys or credentials — use k8s Secrets
- Never use sidecar injection to blindly push context into every prompt — agent tools only
- Never store conversation memory in pod RAM or a Redis sidecar — use RavenDB
- Never automate hidden city booking flow — legal risk, display only
- Never use `--force` on kubectl in production manifests
- Do not split the data layer into a separate vector DB + document DB — that's the problem we're solving
- Do not add Kafka until RavenDB Subscriptions prove insufficient for the ingestion volume

## Key Metrics to Track (Demo)

| Metric                     | Naive baseline          | With RavenDB in-cluster    |
|----------------------------|-------------------------|----------------------------|
| Tokens per call            | 40k–100k                | ~1500                      |
| Egress per retrieval call  | Full payload leaves cluster | Zero (local query)     |
| Cost at 10k requests/day   | Measure and show        | Measure and show           |
| Prefill time               | Baseline                | With LLM-d disaggregation  |
| p95 latency (retrieval)    | External API RTT        | In-cluster query time      |

See @scripts/measure_tokens.py and @scripts/measure_egress.py for tooling.

## Evolutionary Narrative (Demo Script Context)

The presentation shows the same use case rebuilt across three eras — each with a
cost breakdown. RavenDB appears as the final step, not the starting assumption.

1. **No k8s, naive DB** — fat context payloads, external API, high egress + token cost
2. **K8s, still external DB** — containerized but retrieval still crosses cluster boundary
3. **K8s + RavenDB in-cluster via Operator** — retrieval is local, egress goes to near-zero

Each stage is costed. The closing slide leaves the architecture choice open:
"The data is already there. The Operator makes sure it stays there."

See @docs/architecture.md for per-stage cost estimates and diagrams.
