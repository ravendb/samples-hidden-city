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
if [[ "$SKIP_BUILD" == "true" ]]; then
  warn "Skipping Docker build (--skip-build)"
else
  step "Building Docker image  →  $IMAGE_TAG"
  docker build -t "$IMAGE_TAG" .
  ok "Image built: $IMAGE_TAG"
fi

# ── step 3: namespace + secrets ───────────────────────────────────────────────
step "Applying namespace and secrets"
kubectl apply -f k8s/namespace.yaml
ok "Namespace '$NS' ready"

kubectl apply -f k8s/secrets.local.yaml
ok "Secrets applied"

# ── step 4: full kustomize apply ──────────────────────────────────────────────
step "Applying all manifests  (kubectl apply -k k8s/)"
kubectl apply -k k8s/

# ── step 5: wait for RavenDB cluster ─────────────────────────────────────────
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

# ── step 6: wait for agent ────────────────────────────────────────────────────
step "Waiting for agent deployment to roll out"
kubectl rollout status deployment/agent -n "$NS" --timeout=120s
ok "Agent deployment ready"

# ── step 7: final status ──────────────────────────────────────────────────────
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
  warn "  kubectl port-forward svc/agent-svc 8000:80 -n $NS"
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
