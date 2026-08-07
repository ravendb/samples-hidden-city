# Hidden City Flight Search — RavenDB as Agent Working Memory

## Overview

**This project solves the hidden cost problem in LLM agents by keeping the agent's working memory in the same cluster as the model call.** Instead of stuffing raw API responses into every prompt, the agent queries RavenDB — co-located in Kubernetes — as an explicit tool call. The LLM only ever sees a pre-structured, token-budgeted result, never the raw payload.

**The application also demonstrates conversational hidden-city flight detection.** Airlines price connecting routes cheaper than direct legs; the agent compares cached and live prices from Travelpayouts, scores the savings against a risk profile, and surfaces the opportunity as information only — never automating a booking.

**To fully embed itself in the Kubernetes ecosystem**, the RavenDB cluster is declared as a `RavenDBCluster` custom resource and reconciled by the [RavenDB Kubernetes Operator](https://github.com/ravendb/ravendb-operator), which handles bootstrap, certificate wiring, and rolling node upgrades. On top of this, a CronJob bulk-ingests Travelpayouts prices every 6 hours, and a RavenDB Data Subscription pushes price-drop alerts to a worker with no polling loop.

**Built with RavenDB, Python, FastAPI, the OpenAI API, and Kubernetes.**

## Features used

The following RavenDB features are used to build the application:

1. AI / Agent Integration
   1. Tool-calling agent loop (OpenAI `gpt-4o-mini`) — RavenDB is queried only through explicit LLM tool calls, never injected as a sidecar — [`src/agent/loop.py`](src/agent/loop.py)
   1. Single-call fast path — a narrow exception that pre-resolves unambiguous "from X to Y" messages before calling the LLM at all, cutting a turn from two OpenAI calls to one — [`src/tools/resolve_airports.py`](src/tools/resolve_airports.py)
1. Document & Query Features
   1. Document Store — schema-flexible route, session, and airport documents, no migrations — [`src/db/models.py`](src/db/models.py)
   1. Auto-Indexes (full-text search) — airport city/IATA lookup with no static index ever defined — [`src/tools/resolve_airports.py`](src/tools/resolve_airports.py)
   1. Vector Search — nearby-airport fallback using a 3D unit-sphere projection of each airport's lat/lng — [`src/tools/search_routes.py`](src/tools/search_routes.py), [`src/db/geo.py`](src/db/geo.py)
   1. Document Expiration — price fields auto-expire 20 minutes after write via `@expires` metadata, no cleanup job — [`src/db/expiration.py`](src/db/expiration.py)
   1. Attachments — passport scan, bag photo, and a preference PDF stored as binary blobs on the user document, never inflating a query — [`src/tools/user_attachments.py`](src/tools/user_attachments.py)
   1. Data Subscriptions — push-only price-drop alerts, no polling and no message broker — [`src/worker/run.py`](src/worker/run.py)
1. Kubernetes
   1. RavenDB Kubernetes Operator — a 3-node cluster declared as a CRD; bootstrap, cert wiring, and rolling upgrades are handled automatically — [ravendb-operator](https://github.com/ravendb/ravendb-operator), [`k8s/operator/`](k8s/operator/)

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

## Local setup

A few steps are required to run the application locally.

1. Check out the Git repository
1. Install prerequisites:
   1. [Python 3.11–3.13](https://www.python.org/downloads/) — 3.14 is not yet supported (pyravendb dependency)
   1. [uv](https://github.com/astral-sh/uv)
   1. [Docker](https://docs.docker.com/get-docker/) + Docker Compose (bundled with Docker Desktop) — Kubernetes mode's `start-k8s.ps1` will also install Docker Desktop via `winget` if it's missing, but Docker Desktop's first launch needs one manual step (accept the license, finish WSL2/Hyper-V setup, possibly reboot) before the script can continue
   1. Kubernetes mode only — `start-k8s.ps1` installs all of these automatically, no manual setup required:
      1. [kubectl](https://kubernetes.io/docs/tasks/tools/), [Helm](https://helm.sh/), [kind](https://kind.sigs.k8s.io/) — fetched at the exact versions pinned in `versions.env` straight into a local `.tools/` folder (no `winget`, no admin rights, no PATH changes)
      1. [OpenSSL](https://www.openssl.org/) — via `winget`, only checked/installed if the RavenDB TLS cert chain (`k8s/ravendb/certs/`) doesn't already exist locally; skipped entirely once that chain has been generated once
1. Run `.\start.ps1` and pick a mode when prompted, or skip the prompt directly:
   1. `.\start.ps1 -Mode Local` — docker-compose RavenDB, seeds fixture data, starts the agent
   1. `.\start.ps1 -Mode K8s` — kind cluster + cert-manager + ingress-nginx + RavenDB Operator + full app deployment
1. Before the first run, both modes prompt you interactively in the terminal (Enter to skip an optional value) for:
   1. `OPENAI_API_KEY` — required, the agent won't start without it ([platform.openai.com](https://platform.openai.com/))
   1. `TRAVELPAYOUTS_TOKEN` — optional; without it the agent falls back to fixture data seeded by `scripts/seed_local.py`
   1. The RavenDB license is picked up silently from `license.json` in the repo root if present, otherwise Local mode runs RavenDB in Developer Mode (3 GB / 1 node limit) and Kubernetes mode skips the license secret with a warning
1. Once the agent is running, its landing page links to a `/setup` wizard (`http://localhost:8000/setup`) that walks through all three of `OPENAI_API_KEY`, `RAVENDB_LICENSE`, and `TRAVELPAYOUTS_TOKEN` in the browser and writes whichever you fill in to `.env` — the easiest way to add a key you skipped in the terminal, or to hand the demo to someone without shell access.
1. RavenDB Studio is available at `http://localhost:8080` (Local mode) or `https://localhost:8081` (Kubernetes mode — self-signed cert, browser will warn). Open the `Routes` collection to inspect enriched documents.

## Remarks

Without a Travelpayouts token, every demo scenario still runs end to end on fixture data seeded by `scripts/seed_local.py` — no live API dependency is required to see the full flow.

The bulk scraper's Travelpayouts endpoint only returns price and a transfer count, never the actual connecting airport — real itinerary segments require a partnership-gated API this project doesn't have. For routes with transfers, the scraper instead infers the likely hub offline from airport coordinates already in RavenDB. It's a heuristic, not ground truth — see [`src/scraper/hub_inference.py`](src/scraper/hub_inference.py).

Kubernetes mode automates cert-manager, ingress-nginx, the RavenDB Operator, and the app deployment end to end — including the RavenDB cluster's TLS certificate chain, which `start-k8s.ps1` generates locally as a self-signed CA/server/client chain via `openssl` (see `Ensure-RavenDbCerts` in the script) and applies as Kubernetes secrets automatically. No manual Setup Wizard step or `kubectl create secret` command is required.

The `kubernetes/ingress-nginx` project was archived on 2026-03-24 and will receive no further releases. `versions.env` pins the last one it published (`controller-v1.15.1`) rather than tracking a moving branch. Its own compatibility matrix only declares support up to Kubernetes 1.35, while `versions.env`'s pinned kind node image is already on 1.36 — outside that tested range (though not necessarily broken; the Ingress API surface it depends on rarely changes between minor Kubernetes releases). This gap only widens with every future kind bump, since ingress-nginx can never test against a newer Kubernetes again. Worth knowing before assuming a version bump here is risk-free.

See [`docs/architecture.md`](docs/architecture.md) for the full token/egress cost breakdown across four infrastructure stages, and [`docs/hidden-city.md`](docs/hidden-city.md) for the hidden-city detection algorithm spec.

## Legal Note

This project demonstrates the *detection* of hidden city pricing patterns for educational purposes. It does not automate booking, generate itineraries, or interact with airline reservation systems. Consult an airline's terms of service before applying hidden city strategies to real bookings.
