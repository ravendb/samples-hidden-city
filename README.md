# Hidden City Flight Search: RavenDB as Agent Working Memory

## Overview

**This project solves the hidden cost problem in LLM agents by keeping the agent's working memory in the same cluster as the model call.** Instead of stuffing raw API responses into every prompt, the agent queries RavenDB, co-located in Kubernetes, as an explicit tool call. The LLM only ever sees a pre-structured, token-budgeted result, never the raw payload.

**The application also demonstrates conversational hidden-city flight detection.** Airlines price connecting routes cheaper than direct legs; the agent compares cached and live prices from Travelpayouts, scores the savings against a risk profile, and surfaces the opportunity as information only, never automating a booking.

**To fully embed itself in the Kubernetes ecosystem**, the RavenDB cluster is declared as a `RavenDBCluster` custom resource and reconciled by the [RavenDB Kubernetes Operator](https://github.com/ravendb/ravendb-operator), which handles bootstrap, certificate wiring, and rolling node upgrades. On top of this, a CronJob bulk-ingests Travelpayouts prices every 6 hours, and a RavenDB Data Subscription pushes price-drop alerts to a worker with no polling loop.

**Built with RavenDB, Python, FastAPI, the OpenAI API, and Kubernetes.**

## Local setup

A few steps are required to run the application locally. 

1. Check out the Git repository
2. Install prerequisites:
      a. [Docker](https://docs.docker.com/get-docker/) (Docker Desktop on macOS, Docker Engine or Docker Desktop on Linux)
         
3. `.\start.ps1` and pick a mode when prompted
4. Before the first run, both modes prompt you interactively in the terminal (Enter to skip an optional value) for:
   a. `OPENAI_API_KEY`: required, the agent won't start without it ([platform.openai.com](https://platform.openai.com/))
   b. `TRAVELPAYOUTS_TOKEN`: optional; without it the agent falls back to fixture data seeded by `scripts/seed_local.py`
   c. The RavenDB license is picked up silently from `license.json` in the repo root if present, otherwise Local mode runs RavenDB in Developer Mode (3 GB / 1 node limit) 
5. RavenDB Studio is available at `http://localhost:8080` (Local mode) or `https://localhost:8081` (Kubernetes mode, self-signed cert, browser will warn). 


## Features used

The following RavenDB features are used to build the application:

1. AI / Agent Integration
   1. Tool-calling agent loop (OpenAI `gpt-4o-mini`): RavenDB is queried only through explicit LLM tool calls, never injected as a sidecar - [`src/agent/loop.py`](src/agent/loop.py)
   1. Single-call fast path: a narrow exception that pre-resolves unambiguous "from X to Y" messages before calling the LLM at all, cutting a turn from two OpenAI calls to one - [`src/tools/resolve_airports.py`](src/tools/resolve_airports.py)
1. Document & Query Features
   1. Document Store: schema-flexible route, session, and airport documents, no migrations - [`src/db/models.py`](src/db/models.py)
   1. Auto-Indexes (full-text search): airport city/IATA lookup with no static index ever defined - [`src/tools/resolve_airports.py`](src/tools/resolve_airports.py)
   1. Vector Search: nearby-airport fallback using a 3D unit-sphere projection of each airport's lat/lng - [`src/tools/search_routes.py`](src/tools/search_routes.py), [`src/db/geo.py`](src/db/geo.py)
   1. Document Expiration: price fields auto-expire 20 minutes after write via `@expires` metadata, no cleanup job - [`src/db/expiration.py`](src/db/expiration.py)
   1. Attachments: passport scan, bag photo, and a preference PDF stored as binary blobs on the user document, never inflating a query - [`src/tools/user_attachments.py`](src/tools/user_attachments.py)
   1. Data Subscriptions: push-only price-drop alerts, no polling and no message broker - [`src/worker/run.py`](src/worker/run.py)
   1. Batched Multi-Document Load: conversation history and user profile loaded in a single round trip per chat turn, even across two different collections - [`src/agent/app.py`](src/agent/app.py)
   1. Server-Wide Operations: `CreateDatabaseOperation` bootstraps the database on first boot; every process that touches it (agent, worker, scraper) calls this independently and retries transient RavenDB unavailability, so none of them is a single point of failure for it - [`src/db/seed.py`](src/db/seed.py)
   1. Client Certificate Authentication: mTLS between every Python process and RavenDB inside the cluster, never plaintext - [`src/db/client.py`](src/db/client.py)
1. Kubernetes
   1. RavenDB Kubernetes Operator: a multi-node cluster declared as a single CRD; bootstrap, cert wiring, and rolling upgrades are handled automatically - [ravendb-operator](https://github.com/ravendb/ravendb-operator), [`k8s/operator/`](k8s/operator/). Currently scaled to 1 node in [`k8s/ravendb/values.yaml`](k8s/ravendb/values.yaml) pending a multi-node-capable license (see that file's comment); restore nodes b/c there to demo the 3-node case.

## Technologies

The following technologies were used to build this application:

1. RavenDB 7.2
1. Python 3.11–3.13
1. FastAPI + Uvicorn
1. OpenAI API (`gpt-4o-mini`)
1. Kubernetes + RavenDB Kubernetes Operator
1. Docker / Docker Compose
1. kind, Helm, kubectl (local Kubernetes)
1. uv (Python package/venv manager)
1. Plain HTML/JS chat UI

## Remarks

Kubernetes mode automates cert-manager, ingress-nginx, the RavenDB Operator, and the app deployment end to end, including the RavenDB cluster's TLS certificate chain, which `start-k8s.ps1` generates locally as a self-signed CA/server/client chain via `openssl` (see `Ensure-RavenDbCerts` in the script) and applies as Kubernetes secrets automatically. No manual Setup Wizard step or `kubectl create secret` command is required.

See [`docs/architecture.md`](docs/architecture.md) for the full token/egress cost breakdown across four infrastructure stages, and [`docs/hidden-city.md`](docs/hidden-city.md) for the hidden-city detection algorithm spec.

## Legal Note

This project demonstrates the *detection* of hidden city pricing patterns for educational purposes. It does not automate booking, generate itineraries, or interact with airline reservation systems. Consult an airline's terms of service before applying hidden city strategies to real bookings.

## Contributing

See [`CONTRIBUTING.md`](CONTRIBUTING.md) for how to report issues and submit changes.

## License

MIT: see [`LICENSE`](LICENSE).
