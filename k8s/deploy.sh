#!/usr/bin/env bash
# Full deployment of Hidden City Flight Agent to Kubernetes.
#
# Usage:
#   bash k8s/deploy.sh                  # deploy everything
#   bash k8s/deploy.sh --skip-operator  # skip operator install (already installed)
#   bash k8s/deploy.sh --skip-build     # skip docker build (image already pushed)
#
# Prerequisites:
#   - kubectl configured and pointing at the right cluster
#   - k8s/secrets.local.yaml filled in (copy from k8s/secrets.yaml)
#   - Docker daemon running (unless --skip-build)
set -euo pipefail

# versions.env is the single source of truth (repo root). Capture any
# explicitly pre-exported override BEFORE sourcing -- `source` unconditionally
# reassigns, so it would otherwise clobber a caller's override with the file's
# default.
_user_ravendb_cluster_chart_version="${RAVENDB_CLUSTER_CHART_VERSION:-}"
root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
set -a
source "${VERSIONS_FILE:-$root/versions.env}"
set +a
RAVENDB_CLUSTER_CHART_VERSION="${_user_ravendb_cluster_chart_version:-$RAVENDB_CLUSTER_CHART_VERSION}"

SKIP_OPERATOR=false
SKIP_BUILD=false
IMAGE_TAG="${IMAGE_TAG:-hidden-city:latest}"
NS="hidden-city"

for arg in "$@"; do
  case $arg in
    --skip-operator) SKIP_OPERATOR=true ;;
    --skip-build)    SKIP_BUILD=true ;;
  esac
done

# ── helpers ──────────────────────────────────────────────────────────────────
bold()  { printf '\033[1m%s\033[0m\n' "$*"; }
step()  { printf '\n\033[1;34m==> %s\033[0m\n' "$*"; }
ok()    { printf '\033[0;32m    ✓ %s\033[0m\n' "$*"; }
warn()  { printf '\033[0;33m    ! %s\033[0m\n' "$*"; }
fail()  { printf '\033[0;31m    ✗ %s\033[0m\n' "$*"; exit 1; }

# ── preflight ────────────────────────────────────────────────────────────────
step "Preflight checks"

kubectl cluster-info --request-timeout=5s > /dev/null 2>&1 \
  && ok "kubectl connected to cluster" \
  || fail "kubectl cannot reach the cluster — check your kubeconfig"

if [[ ! -f k8s/secrets.local.yaml ]]; then
  fail "k8s/secrets.local.yaml not found. Run: cp k8s/secrets.yaml k8s/secrets.local.yaml && edit it"
fi
ok "secrets.local.yaml present"

# ── step 1: RavenDB operator ──────────────────────────────────────────────────
if [[ "$SKIP_OPERATOR" == "true" ]]; then
  warn "Skipping operator install (--skip-operator)"
else
  step "Installing RavenDB Kubernetes Operator"
  bash k8s/operator/install.sh
fi

# ── step 2: docker build ──────────────────────────────────────────────────────
image_rebuilt=false
if [[ "$SKIP_BUILD" == "true" ]]; then
  warn "Skipping Docker build (--skip-build)"
else
  step "Building Docker image  →  $IMAGE_TAG"
  docker build -t "$IMAGE_TAG" .
  ok "Image built: $IMAGE_TAG"
  image_rebuilt=true
fi

# ── step 3: namespace + secrets ───────────────────────────────────────────────
step "Applying namespace and secrets"
kubectl apply -f k8s/namespace.yaml
ok "Namespace '$NS' ready"

kubectl apply -f k8s/secrets.local.yaml
ok "Secrets applied"

# ── step 4: RavenDB cluster (Helm chart, not kustomize) ──────────────────────
step "Installing RavenDB cluster  (helm upgrade --install ravendb-cluster ...)"
echo "    Requires the license/cert secrets referenced by k8s/ravendb/values.yaml"
echo "    to already exist in namespace '$NS' — see k8s/operator/install.sh."
helm upgrade --install ravendb-cluster ravendb-operator/ravendb-cluster \
  -n "$NS" --create-namespace -f k8s/ravendb/values.yaml \
  --version "$RAVENDB_CLUSTER_CHART_VERSION"
ok "RavenDB cluster chart applied"

# ── step 5: wait for RavenDB cluster ─────────────────────────────────────────
# Before the app manifests (step 6), not after: the agent pod's own startup
# creates the RavenDB *database* itself, and used to race RavenDB's actual
# readiness when applied first — a single failed attempt there was silently
# swallowed, leaving DatabaseDoesNotExistException forever (confirmed in
# practice; fixed in src/db/seed.py, which now retries on its own too, but
# this ordering means fewer pods ever need to exercise that retry path).
step "Waiting for RavenDB cluster to be ready (operator reconciliation)"
echo "    This typically takes 60–120 s on first install."
echo "    The operator is: creating PVCs → starting pods → forming Raft quorum → issuing TLS certs."
echo ""

# Poll the RavenDBCluster status condition.
# The operator sets condition type=Ready on the cluster resource when quorum is reached.
RAVENDB_READY=false
for i in $(seq 1 40); do
  STATUS=$(kubectl get ravendbcluster ravendb-cluster -n "$NS" \
    -o jsonpath='{.status.conditions[?(@.type=="Ready")].status}' 2>/dev/null || true)
  if [[ "$STATUS" == "True" ]]; then
    RAVENDB_READY=true
    break
  fi
  printf "    [%2d/40] RavenDB cluster not ready yet (%s)…\n" "$i" "$(date +%H:%M:%S)"
  sleep 5
done

if [[ "$RAVENDB_READY" == "true" ]]; then
  ok "RavenDB cluster is Ready"
else
  warn "RavenDB cluster did not reach Ready in time — check with:"
  warn "  kubectl describe ravendbcluster ravendb-cluster -n $NS"
  warn "  kubectl get pods -n $NS"
fi

# Show cluster pods
echo ""
kubectl get pods -n "$NS" -l app.kubernetes.io/name=ravendb-cluster 2>/dev/null \
  || kubectl get pods -n "$NS" | grep ravendb || true

# ── step 6: full kustomize apply (rest of the app) ────────────────────────────
step "Applying app manifests  (kubectl apply -k k8s/)"
kubectl apply -k k8s/

# `kubectl apply` only triggers a new rollout when the manifest text itself
# changes -- with a mutable image tag, a rebuilt image (loaded into kind, or
# pushed to a registry under the same tag) leaves the Deployment's pod
# template textually identical, so already-running pods keep serving the OLD
# image forever until something forces a restart (confirmed in practice: pods
# stayed up unchanged after a rebuild, still serving stale code). Force it
# whenever this run actually rebuilt the image -- not on every run, so an
# unrelated --skip-operator-only re-run doesn't bounce pods for no reason.
if [[ "$image_rebuilt" == "true" ]]; then
  step "Restarting agent/worker to pick up the freshly built image"
  kubectl rollout restart deployment/agent -n "$NS" >/dev/null
  kubectl rollout restart deployment/subscription-worker -n "$NS" >/dev/null
fi

# ── step 7: wait for agent ────────────────────────────────────────────────────
# 180s: the Travelpayouts bulk scraper no longer blocks readiness (it runs as
# a background task in _startup() -- see app.py) -- what's left gating it is
# ensure_database's own retry budget (up to 90s) plus two quick API
# validation calls. This used to be 1200s to match the scraper blocking
# startup for ~20 min.
step "Waiting for agent deployment to roll out (up to ~3 min on a cold DB)"
kubectl rollout status deployment/agent -n "$NS" --timeout=180s
ok "Agent deployment ready"

# ── step 8: final status ──────────────────────────────────────────────────────
step "Deployment complete — cluster status"

echo ""
bold "Pods:"
kubectl get pods -n "$NS"

echo ""
bold "Services:"
kubectl get svc -n "$NS"

echo ""
bold "RavenDB cluster:"
kubectl get ravendbcluster -n "$NS"

# Extract agent ingress / LoadBalancer IP
echo ""
bold "Access:"

INGRESS_IP=$(kubectl get ingress agent-ingress -n "$NS" \
  -o jsonpath='{.status.loadBalancer.ingress[0].ip}' 2>/dev/null || true)
INGRESS_HOST=$(kubectl get ingress agent-ingress -n "$NS" \
  -o jsonpath='{.status.loadBalancer.ingress[0].hostname}' 2>/dev/null || true)

if [[ -n "$INGRESS_IP" ]]; then
  ok "Chat UI  →  http://${INGRESS_IP}/"
elif [[ -n "$INGRESS_HOST" ]]; then
  ok "Chat UI  →  http://${INGRESS_HOST}/"
else
  warn "Ingress IP not assigned yet. Try:"
  warn "  kubectl get ingress -n $NS"
  warn "  kubectl port-forward svc/agent-svc 8001:80 -n $NS"
fi

RAVENDB_LB=$(kubectl get svc -n "$NS" \
  -o jsonpath='{.items[?(@.spec.type=="LoadBalancer")].status.loadBalancer.ingress[0].ip}' \
  2>/dev/null | awk '{print $1}' || true)
if [[ -n "$RAVENDB_LB" ]]; then
  ok "RavenDB Studio  →  https://${RAVENDB_LB}:443"
else
  warn "RavenDB LoadBalancer IP not assigned yet — run: kubectl get svc -n $NS"
fi

echo ""
