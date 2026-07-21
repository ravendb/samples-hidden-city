# Demo Script — RavenDB as In-Cluster Agent Working Memory

## RavenDB Operator Demo (~5 minutes)

### Step 1 — Show the problem (30 sec)

> "Setting up a 3-node RavenDB cluster with Raft quorum, TLS, and rolling upgrades manually takes dozens of steps. Instead, we have one values file."

Show the manifest:
```bash
cat k8s/ravendb/values.yaml
```
Point to the 3-entry `nodes` list and `mode: None` (self-signed TLS).

---

### Step 2 — Install the operator (fresh cluster only)

```bash
bash k8s/operator/install.sh
```

> "The operator is a controller — it registers a new resource type in Kubernetes. From this point on, the cluster understands `RavenDBCluster`."

---

### Step 3 — One apply, watch it work (main moment)

```bash
helm upgrade --install ravendb-cluster ravendb-operator/ravendb-cluster \
  -n hidden-city --create-namespace -f k8s/ravendb/values.yaml
watch kubectl get pods -n hidden-city
```

Watch 3 pods appear live. Say:
> "The operator bootstraps the Raft cluster, generates TLS certificates, and waits for quorum before moving to the next node."

---

### Step 4 — Show status conditions

```bash
kubectl get ravendbclusters -n hidden-city
kubectl describe ravendbclusters ravendb-cluster -n hidden-city
```

Point to `Status.Conditions` — `Ready: True`:
> "This is the operator's contract — not 'pods are running', but 'cluster is Ready with quorum'."

---

### Step 5 — Show admission webhook (optional, high impact)

```bash
kubectl patch ravendbcluster ravendb-cluster -n hidden-city \
  --type=json -p '[{"op":"remove","path":"/spec/nodes/2"}]'
```

Show the webhook rejecting the request:
> "Going below quorum is blocked before anything changes. You can't accidentally break the cluster."

(Verify this patch against the installed CRD before the live demo — the operator
may reject topology changes after bootstrap for a different reason than quorum;
confirm the actual admission-webhook error message with `kubectl describe` first.)

---

### Step 6 — Close the loop back to the agent

```bash
kubectl get svc -n hidden-city
```

Point to `ravendb-cluster-svc` as ClusterIP:
> "The agent sees RavenDB as a local address inside the cluster. Zero egress, zero retrieval latency."

---

### Closing line

> "One `kubectl apply` — the operator does the rest. We declare what we want, not how to build it."

---

## Token Counter Slide

### Option A — Side-by-side live counter (static slide)

```
┌─────────────────────────────────┬─────────────────────────────────┐
│         NAIVE BASELINE          │        RAVENDB IN-CLUSTER        │
│                                 │                                  │
│   Raw Travelpayouts → LLM prompt │   Tool call → RavenDB → LLM      │
│                                 │                                  │
│         40 000 tokens           │           1 500 tokens           │
│   ████████████████████████░░    │   █░░░░░░░░░░░░░░░░░░░░░░░░░░   │
│                                 │                                  │
│        $0.12 / request          │         $0.005 / request         │
│        $1 260 / day             │           $52 / day              │
│         (10k requests)          │          (10k requests)          │
└─────────────────────────────────┴─────────────────────────────────┘
              ──────────────── 96% cheaper ────────────────
```

### Option B — Live ticker during agent conversation

```
  User: "Find hidden city flights WAW → LHR"

  [Agent thinking...]  ████████░░░░░░░░░░░░

  ┌─────────────────────────────────────────┐
  │  tokens sent to OpenAI this turn        │
  │                                         │
  │  system prompt   ░░░░  198 tok          │
  │  user message    ░     87 tok           │
  │  tool results    ░░░   612 tok          │
  │  ─────────────────────────────          │
  │  TOTAL           ░░░░  897 tok  ✓       │
  │                  budget: 1 500          │
  └─────────────────────────────────────────┘
```

### Option C — 4-stage evolution (summary slide)

```
  tokens / request

  100k │ ●
       │  \
   50k │   \
       │    \
   20k │     ●
       │      \
    5k │       ●
    1k │        ●
       └──────────────────
        naive  +k8s  +ES  RavenDB
                          in-cluster

                            ← 96% reduction
```

Run `python -m scripts.measure_tokens` live to show real numbers during the demo.

---

## Demo Commands Reference

```bash
# Pick Local or Kubernetes interactively
.\start.ps1

# Local dev (one command)
.\start.ps1 -Mode Local

# Local dev — skip seeding if already populated
.\start.ps1 -Mode Local -SkipSeed

# Local dev — also start price-drop worker
.\start.ps1 -Mode Local -Worker

# K8s — full deploy (kind + cert-manager + ingress-nginx + operator + cluster + app)
.\start.ps1 -Mode K8s

# K8s — redeploy without reinstalling cert-manager/ingress-nginx/operator
.\start.ps1 -Mode K8s -SkipOperator

# Show cluster state
kubectl get ravendbclusters
kubectl describe ravendbclusters ravendb-cluster

# Measure token usage live
python -m scripts.measure_tokens

# Measure egress live
python -m scripts.measure_egress
```
