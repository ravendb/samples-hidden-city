# Architecture — RavenDB as In-Cluster Agent Working Memory

## The Core Cost Problem

An LLM agent running flight searches has two billing lines nobody puts in the
initial budget: **token cost** and **egress cost**. Both scale linearly with
traffic. This document traces the architecture through four stages and shows
exactly where the money goes at each step.

---

## Stage 1 — Naive: Laptop Script, No Persistence

```
User
 │
 ▼
Python script ──────────────────────────────▶ Travelpayouts API
                                              (bulk response, large JSON)
                    ┌────────────────┐
                    │  raw JSON      │   40 000–100 000 tokens
                    │  stuffed into  │──▶ OpenAI API  $0.006/request
                    │  the prompt    │
                    └────────────────┘
```

**What happens:**

1. User asks "cheap flights WAW→LHR".
2. Script calls Travelpayouts `/v2/prices/latest`. Response: large JSON with
   hundreds of price records, routes, carriers.
3. Entire response is pasted into the LLM prompt.
4. Model answers. No history saved.
5. Next question: repeat from step 2.

**Cost at 1 000 requests/day:**

| Component | Calculation | Daily cost |
|-----------|-------------|------------|
| Travelpayouts API calls | 1 000 × free tier | $0 |
| LLM input tokens | 1 000 × 40 000 tok × $0.15/1M | **$6** |
| LLM output tokens | 1 000 × 400 tok × $0.60/1M | $0.24 |
| Egress (Travelpayouts → app) | 1 000 × 250 KB × $0.09/GB | $0.02 |
| **Total** | | **~$6.26/day** |

**Pain points:**

- Cache hit rate: 0 % — every turn hits the live API.
- Conversation history: none — user must repeat context each message.
- Price watch: impossible without polling, which burns API quota.
- Demo breaks on rate limits within minutes.

---

## Stage 2 — Containerized, External Redis + Postgres

```
User
 │
 ▼
Flask/FastAPI ──cache miss──▶ Travelpayouts API
      │                       (bulk response, cached in Redis for 5 min)
      │
      ├──▶ Redis (TTL cache)        outside the cluster
      │    returns 250 KB blob ─────────────────────────────────▶ LLM prompt
      │
      └──▶ Postgres (sessions)      outside the cluster
           full history ────────────────────────────────────────▶ LLM prompt

Celery worker ──every 5 min──▶ Travelpayouts (polling)
```

**What happens:**

- Redis stores raw API responses with a 5-min TTL. Cache hits skip Travelpayouts
  but still send the full bulk blob to the LLM.
- Postgres stores conversation turns. At the start of each turn, the agent reads
  the last N turns and appends them to the prompt.
- Celery polls Travelpayouts every 5 minutes per watched route (price watch feature).

**Cost at 1 000 requests/day (assuming 40 % cache hit rate):**

| Component | Calculation | Daily cost |
|-----------|-------------|------------|
| Travelpayouts calls | 600 × misses | free tier |
| LLM input tokens | 1 000 × 35 000 avg tok × $0.15/1M | **$5.25** |
| Redis managed (AWS ElastiCache r7g.large) | — | **$100/month** |
| Postgres managed (RDS t4g.medium) | — | **$60/month** |
| Celery workers (2 × t3.small) | — | **$30/month** |
| Cross-AZ egress (app ↔ Redis ↔ RDS) | 1 000 × 250 KB × $0.01/GB (intra-AZ) | ~$0 |
| **Total LLM** | | **~$5.49/day** |
| **Total infra** | | **~$190/month** |

**Pain points:**

- Token cost barely improved — cache hit returns the same fat payload.
- Three infrastructure components to operate and monitor.
- Redis evicts on memory pressure, causing unexpected cache misses under load.
- Postgres schema migrations for every new field.
- 288 Celery polling calls/route/day regardless of whether price changed.

---

## Stage 3 — K8s, Cloud-Managed Search + Vector DB

```
User
 │
 ▼
FastAPI (k8s) ──▶ Elasticsearch (cloud-managed)  ←─── bulk ingest (CronJob)
                  returns 50 KB JSON ──────────────────────────▶ LLM prompt
      │
      ├──▶ pgvector (RDS Postgres)   semantic similarity
      │    returns 20 KB JSON ────────────────────────────────▶ LLM prompt
      │
      └──▶ Redis (ElastiCache)       session history
           returns 10 KB history ──────────────────────────────▶ LLM prompt

Kafka ──price events──▶ consumer pod ──▶ ES update ──▶ agent notified
```

**What happens:**

- Elasticsearch handles full-text search on route descriptions and airport names.
- pgvector stores embeddings for semantic queries ("something like LHR but cheaper").
- A Kafka consumer replaces Celery polling — price events pushed, not polled.
- The agent queries all three stores and composes a context payload before calling
  the LLM.

**Cost at 1 000 requests/day:**

| Component | Calculation | Daily cost |
|-----------|-------------|------------|
| LLM input tokens | 1 000 × 20 000 avg tok × $0.15/1M | **$3** |
| Elasticsearch (AWS managed, 2-node) | — | **$350/month** |
| RDS Postgres + pgvector (db.r6g.large) | — | **$180/month** |
| ElastiCache Redis | — | **$100/month** |
| MSK Kafka (3-broker) | — | **$500/month** |
| Kafka consumer pods (2 × t3.medium) | — | **$60/month** |
| Cross-service egress (app ↔ ES ↔ RDS) | 1 000 × 70 KB × $0.09/GB | $0.006 |
| **Total LLM** | | **~$3.24/day** |
| **Total infra** | | **~$1,190/month** |

**Pain points:**

- Five separate infrastructure components; five different monitoring targets; five
  backup strategies.
- ES queries and SQL queries and Redis commands — three query languages.
- Cross-service egress still leaves the cluster boundary on every retrieval.
- Kafka overkill for a single ingestion source — MSK minimum cluster is $500/month.
- 50 ms added latency per turn from ES round-trip.

---

## Stage 4 — K8s + RavenDB In-Cluster (This Demo)

```
User ──prompt only──▶ Agent (FastAPI, k8s) ──▶ OpenAI API
                           │                    ▲
                           │  tool call          │  ~2 500 tokens
                           ▼                    │
                      RavenDB (in-cluster) ─────┘
                      ┌──────────────────────────────────┐
                      │  Documents   Routes, Sessions     │
                      │  Full-text   airport/hub search   │
                      │  Subscriptions  price drop push   │
                      └──────────────────────────────────┘
                           ▲                    ▲
                  cache miss │                  │ bulk ingest (6h CronJob)
                           │                  │
                      Travelpayouts        Travelpayouts
                      (live, ~5% miss)      (bulk prices)
```

**What happens (cache hit — zero egress):**

1. User asks "cheap flights WAW→LHR".
2. Agent calls `search_routes(origin="WAW", destination="LHR")` as a tool.
3. RavenDB returns a pre-structured document: origin, destination, price range,
   hidden city score. Serialized to ~400 tokens.
4. Tool result sent to LLM as part of the same API call.
5. Model responds. `persist_turn` appends the turn to RavenDB server-side —
   no LLM tool call, so the response text is never generated twice.
6. **Nothing leaves the cluster except the user's 100-token prompt.**

**What happens (cache miss):**

1. `search_routes` returns `stale: true`.
2. Agent calls `get_live_prices(origin="WAW", destination="LHR")`.
3. Travelpayouts live API is called once. Result is written back to RavenDB.
4. Same 2 500 token LLM call.
5. Next user asking the same route hits the cache — zero Travelpayouts calls.

**What happens (price drop alert — zero polling):**

```
RavenDB detects score change on Routes document
       │
       ▼  push (Data Subscription)
  Worker pod  ──▶  log.info / Slack webhook
  (src/worker/run.py)
```

RavenDB Subscriptions fire when documents matching the query change. No scheduler,
no polling loop, no message broker.

**Cost at 1 000 requests/day:**

| Component | Calculation | Daily cost |
|-----------|-------------|------------|
| LLM input tokens | 1 000 × 2 500 tok × $0.15/1M | **$0.375** |
| LLM output tokens | 1 000 × 400 tok × $0.60/1M | $0.24 |
| Travelpayouts calls (cache miss, ~5 %) | 50 × free tier | $0 |
| RavenDB (3-node cluster, k8s, 3 × t3.medium) | — | **$90/month** |
| Egress (prompt only, 0.1 KB/req) | 1 000 × 0.1 KB × $0.09/GB | ~$0 |
| **Total LLM** | | **~$0.62/day** |
| **Total infra** | | **~$90/month** |

---

## Summary Cost Comparison

### Per-request cost (LLM tokens only, gpt-4o-mini)

| Stage | Input tokens | Input cost | Output tokens | Output cost | Total/req |
|-------|-------------|------------|---------------|-------------|-----------|
| 1 — Naive | 40 000 | $0.0060 | 400 | $0.00024 | **$0.0062** |
| 2 — Redis+Postgres | 35 000 | $0.0053 | 400 | $0.00024 | **$0.0055** |
| 3 — ES+pgvector+Kafka | 20 000 | $0.0030 | 400 | $0.00024 | **$0.0032** |
| 4 — RavenDB in-cluster | 2 500 | $0.00038 | 400 | $0.00024 | **$0.0006** |

*Pricing: $0.15/1M input tokens, $0.60/1M output tokens (gpt-4o-mini, 2025)*

### Daily cost at scale

| Scale | Stage 1 | Stage 2 | Stage 3 | Stage 4 | Stage 1 → 4 saving |
|-------|---------|---------|---------|---------|---------------------|
| 1 000 req/day | $6.26 | $5.49 | $3.24 | **$0.62** | **$5.65/day** |
| 10 000 req/day | $62.6 | $54.9 | $32.4 | **$6.15** | **$56.5/day** |
| 100 000 req/day | $626 | $549 | $324 | **$61.5** | **$565/day** |

### Infrastructure overhead (monthly, HA deployment)

| Stage | Components | Monthly infra cost |
|-------|------------|--------------------|
| 1 — Naive | None | $0 |
| 2 — Redis+Postgres+Celery | 3 | ~$190 |
| 3 — ES+pgvector+Redis+Kafka | 5 | ~$1,190 |
| 4 — RavenDB in-cluster | **1** | **~$90** |

### What RavenDB eliminates from your stack

| Replaced component | Why you no longer need it |
|--------------------|--------------------------|
| Redis (session cache) | RavenDB stores sessions as documents with optional TTL |
| Postgres (conversation history) | Session documents are durable, indexed, queryable |
| Elasticsearch (route search) | Full-text and filtered queries built into RavenDB |
| pgvector (semantic search) | Vector fields on route documents — no separate store |
| Kafka/Celery (price watch) | RavenDB Data Subscriptions push on document change |

---

## How the Token Budget Works

The agent enforces a hard budget via the tool result warning in `src/agent/loop.py`:

```
system_prompt     ≈  200 tokens  (fixed)
user_message      ≈  100 tokens  (varies; 100 is a conservative average)
tool_results      ≤ 1800 tokens  (enforced — warning logged if exceeded)
assistant_response ≤  400 tokens  (max_tokens parameter)
─────────────────────────────────
Total             ≈ 2 500 tokens  per turn
```

The raw Travelpayouts bulk response for a typical route query is large JSON with
hundreds of price records — easily 40 000+ tokens when pasted unfiltered into a prompt.
`search_routes` filters it to origin, destination, hubs, price range, and hidden
city score — roughly 5 fields × 5 routes = ~300 tokens. The model never sees the
raw response.

---

## Kubernetes Operator

The `RavenDBCluster` CRD (chart values in `k8s/ravendb/values.yaml`, project:
https://github.com/ravendb/ravendb-operator) declares the desired cluster
state. The RavenDB Kubernetes Operator reconciles it continuously:

- **Bootstrap** — a one-shot Job discovers the deployed RavenDB pods, wires up
  certs, and forms the Raft cluster (first node becomes leader). Topology
  (which nodes exist) is fixed after this runs — adding/removing nodes later
  is a manual operation, not automatic reconciliation.
- **Upgrades** — orchestrates rolling image upgrades node-by-node in the order
  declared in the spec, with availability/ordering safety gates; halts on a
  failed gate and resumes automatically once resolved; blocks downgrades.
- **Admission webhooks** — reject invalid cluster configurations before they
  are applied.

```yaml
apiVersion: ravendb.ravendb.io/v1
kind: RavenDBCluster
metadata:
  name: ravendb-cluster
  namespace: hidden-city
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
  mode: None
  storage:
    data:
      size: 50Gi
```

Deployed via Helm (`helm upgrade --install ravendb-cluster
ravendb-operator/ravendb-cluster -f k8s/ravendb/values.yaml`), not a raw
`kubectl apply` — the operator handles everything else after that.

---

## Demo Script

1. **Show Stage 1** — run `scripts/measure_tokens.py` against a raw Travelpayouts API call.
   Point at the 40 000 token number.

2. **Show Stage 4** — run `scripts/measure_tokens.py` against the agent.
   Point at the ~2 500 token number.

3. **Show zero egress** — run `scripts/measure_egress.py`. Highlight the
   "Retrieval egress: 0 KB" line.

4. **Show price-drop push** — start `src/worker/run.py`, update a route document
   in the RavenDB Studio, watch the alert fire immediately (no polling delay).

5. **Show pod restart resilience** — kill the agent pod mid-conversation,
   restart it, continue the conversation. History survived in RavenDB.

6. **Show the operator** — run `kubectl get ravendbclusters` and
   `kubectl describe ravendbclusters ravendb-cluster`. Show the status conditions.

**Closing line:** "We kept inference external to show you the savings are purely
about context — if you have a GPU in your cluster, llm-d takes it the rest of
the way."
