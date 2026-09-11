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
      b. **Windows only**: PowerShell 7+ (`pwsh.exe`) — install with `winget install Microsoft.PowerShell` if you don't have it. Not required to already be your active shell: `start.ps1`/`start-k8s.ps1` detect the built-in PowerShell 5.1 (`powershell.exe`) and auto-relaunch themselves under `pwsh.exe`
      c. Free local ports: `8001` (agent), `8080` + `38888` (RavenDB, Local mode) or `8081` (RavenDB Studio, Kubernetes mode)
      d. Resources: ~5 GB free disk (Docker images, RavenDB data volume, Python venv) and 4 GB+ RAM available to Docker
3. Start the app and pick a mode when prompted:
      - **Windows**: `.\start.ps1` (requires PowerShell 7+ installed — the script itself auto-relaunches under `pwsh.exe` even if run from the built-in 5.1 `powershell.exe`)
      - **macOS / Linux / WSL2 — Kubernetes mode**: `bash k8s/start-k8s.sh`
      - **macOS / Linux / WSL2 — Local mode**: `bash start-local.sh`
4. Before the first run, both modes prompt you to enter:
   a. `OPENAI_API_KEY`: required, the agent won't start without it ([platform.openai.com](https://platform.openai.com/))
   b. `TRAVELPAYOUTS_TOKEN`: optional; without it the agent falls back to fixture data seeded by `scripts/seed_local.py`
   c. The RavenDB license - can be picked up silently from `license.json` in the repo root. Get a free Community license at [ravendb.net/download](https://ravendb.net/download).
5. RavenDB Studio is available at `http://localhost:8080` (Local mode) or `https://localhost:8081` (Kubernetes mode). 

## Stopping / tearing down

How to stop, depending on which mode you started with and how much you want to keep.

**Local mode** (`start-local.sh` / `start.ps1 -Mode Local`):
- Press `Ctrl+C` in the terminal running it — the script's cleanup trap runs `docker compose down`, stopping the RavenDB container and releasing its ports.
- This does **not** delete the RavenDB data volume, so the next `start-local.sh` run comes back up with the same data already seeded.
- To also wipe the data volume: `docker compose down -v`.

**Kubernetes mode** (`start-k8s.ps1` / `k8s/start-k8s.sh`):
- To delete the whole kind cluster (nodes, RavenDB data, everything — full reset):
  ```bash
  .\start-k8s.ps1 -DeleteCluster        # Windows
  bash k8s/start-k8s.sh --delete-cluster  # macOS/Linux/WSL2
  ```
  Do this when you want a guaranteed-clean slate (e.g. after a botched TLS cert regen, a stuck cert-manager/operator install, or before switching license/node-count configs) — it's the only supported "reset everything" path; there's no partial/soft-reset flag.
- To just pause work without deleting anything: leave the kind cluster running (it's a set of Docker containers) and re-run `start-k8s.ps1 -SkipBuild -SkipOperator` later to pick up where you left off — nothing needs to be stopped explicitly between sessions.
- If you only need to redeploy app code without touching the cluster/operator/cert-manager: `bash k8s/deploy.sh --skip-operator --skip-build` (see "Key Commands" in [`CLAUDE.md`](CLAUDE.md)).

**When to delete vs. redeploy:** prefer `-DeleteCluster`/`--delete-cluster` only when something in the cluster's underlying state is actually broken (expired cert-manager webhook cert, a cert/CA mismatch between RavenDB and its clients, a corrupted kind node) — for routine "I changed application code" iteration, `-SkipOperator`/`-SkipBuild` re-runs (or `k8s/deploy.sh`) are faster and don't throw away the RavenDB data volume.

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

See [`docs/architecture.md`](docs/architecture.md) for the full token/egress cost breakdown across four infrastructure stages, and [`docs/hidden-city.md`](docs/hidden-city.md) for the hidden-city detection algorithm spec. [`CLAUDE.md`](CLAUDE.md) is a separate, agent/contributor-facing reference (repo conventions, what the agent may/may not do) rather than another copy of this overview — no need to read it unless you're modifying the code.

## Legal Note

This project demonstrates the *detection* of hidden city pricing patterns for educational purposes. It does not automate booking, generate itineraries, or interact with airline reservation systems. Consult an airline's terms of service before applying hidden city strategies to real bookings.

## Contributing

See [`CONTRIBUTING.md`](CONTRIBUTING.md) for how to report issues and submit changes.

## License

MIT: see [`LICENSE`](LICENSE).
