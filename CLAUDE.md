# Flight Search PoC — Context Efficiency via RavenDB + LLM-d on k8s

## Project Goal

Proof of concept showing token cost reduction by using RavenDB as a middleware
context filter in k8s, feeding only relevant context to LLM (via LLM-d) instead
of raw API data. Use case: conversational flight search with hidden city detection.

## Architecture Overview

```
Amadeus API → ETL Pod → RavenDB (middleware) → Context Extractor → LLM-d → Chat
```

Key thesis: RavenDB filters ~50k tokens of flight data down to ~800 tokens of
relevant context before it reaches the LLM. LLM-d handles disaggregated
prefill/decode for efficient GPU utilization.

See @docs/architecture.md for full diagram.

## Stack

- **Runtime**: Kubernetes (k8s)
- **Inference**: LLM-d + vLLM (prefill/decode disaggregation)
- **Database**: RavenDB (document store + spatial + full-text)
- **ETL**: RavenDB Subscriptions (or Kafka if multi-source needed)
- **Flight API**: Amadeus (sandbox for dev, production for prod)
- **Language**: Python (ETL, context extractor), C# or Python (RavenDB client)
- **LLM**: claude-sonnet-4-20250514 via Anthropic API

## Repo Structure

```
/
├── CLAUDE.md
├── docs/
│   ├── architecture.md
│   └── hidden-city.md
├── k8s/
│   ├── ravendb/
│   ├── llm-d/
│   ├── etl/
│   └── context-extractor/
├── src/
│   ├── etl/              # Amadeus → RavenDB pipeline
│   ├── context/          # Context Extractor service
│   ├── hidden_city/      # Hidden city detection logic
│   └── chat/             # Chat UI + LLM agent
└── tests/
    ├── unit/
    └── integration/
```

## Key Commands

```bash
# Local dev
docker-compose up -d ravendb
python src/etl/run.py --env dev

# Deploy to k8s
kubectl apply -f k8s/

# Run tests
pytest tests/unit/
pytest tests/integration/ --require-ravendb

# Check token usage metrics
python scripts/measure_tokens.py
```

## Data Model

### RavenDB Documents

**Route document** (stable, refreshed hourly):

```json
{
  "id": "routes/KTW-PVG-1",
  "origin": "KTW",
  "destination": "PVG",
  "typical_price": { "min": 1800, "max": 4200, "currency": "PLN" },
  "duration_avg_min": 570,
  "hubs": ["FRA", "IST", "DOH"],
  "hidden_city_score": 0.7,
  "@metadata": { "@expires": "<TTL 20 min for price fields>" }
}
```

**Airport document** (static):

```json
{
  "id": "airports/KTW",
  "iata": "KTW",
  "name": "Katowice Pyrzowice",
  "coordinates": { "lat": 50.4743, "lng": 19.0800 },
  "nearby": [
    { "iata": "KRK", "distance_km": 80, "train": true },
    { "iata": "WRO", "distance_km": 170, "train": true },
    { "iata": "PRG", "distance_km": 350, "train": false }
  ]
}
```

## Context Extractor — Rules

IMPORTANT: The context extractor MUST reduce output to under 1000 tokens.

- Use RavenDB spatial query for airport discovery ("near Katowice")
- Use full-text + range query for flight constraints (duration, stops)
- Flag stale prices: if `last_updated` > 15 min, add `"price_stale": true`
- Never pass raw Amadeus response to LLM — always transform first
- Hidden city candidates: include only if `hidden_city_score` > 0.5

## Hidden City Detection Logic

See @docs/hidden-city.md for full spec. Summary:

- Compare price of A→B→C vs A→B
- If A→B→C cheaper and user wants B: flag as hidden city candidate
- Store pattern in RavenDB with risk score
- NEVER automate booking of hidden city tickets — display only

## LLM-d Configuration

- Prefill pods: scale based on context extractor output size (should stay small)
- Decode pods: scale based on concurrent users
- Model: keep consistent, do not switch models mid-session
- Max context to LLM: 1500 tokens (system + user + RavenDB context combined)

## Token Budget per Request

| Component       | Max tokens |
|-----------------|------------|
| System prompt   | 200        |
| User message    | 100        |
| RavenDB context | 800        |
| LLM response    | 400        |
| **Total**       | **~1500**  |

This is the core PoC metric — enforce it and measure it.

## Code Style

- Python: type hints always, dataclasses for internal models
- No raw dicts passed between services — use typed models (Pydantic)
- All k8s manifests in `/k8s/`, no inline kubectl commands in code
- Environment config via k8s ConfigMaps and Secrets, never hardcoded
- Prefer RavenDB Subscriptions over polling loops where possible

## Testing Requirements

- Unit tests for: context extractor output size (MUST assert < 1000 tokens)
- Unit tests for: hidden city detection logic
- Integration tests for: RavenDB queries return correct airport clusters
- Always run `pytest tests/unit/` before committing

## What NOT to Do

- Never pass full Amadeus API response to LLM
- Never hardcode API keys — use k8s Secrets
- Never use `--force` on kubectl in production manifests
- Never automate hidden city booking flow — legal risk
- Do not over-engineer ETL: RavenDB Subscriptions before Kafka

## Key Metrics to Track (PoC Demo)

1. Tokens per request: before vs after RavenDB filtering
2. Prefill time: with LLM-d disaggregation vs without
3. Cost per query: target ~60x reduction vs naive approach
4. p95 latency: context extraction + LLM response

See @scripts/measure_tokens.py for measurement tooling.
