#!/usr/bin/env bash
# Install the RavenDB Kubernetes Operator.
#
# Run once per cluster. Safe to re-run (kubectl apply is idempotent).
# Check for the latest release at:
#   https://github.com/ravendb/ravendb-k8s-operator/releases
set -euo pipefail

OPERATOR_VERSION="${OPERATOR_VERSION:-latest}"

if [[ "$OPERATOR_VERSION" == "latest" ]]; then
  OPERATOR_URL="https://github.com/ravendb/ravendb-k8s-operator/releases/latest/download/ravendb-operator.yaml"
else
  OPERATOR_URL="https://github.com/ravendb/ravendb-k8s-operator/releases/download/${OPERATOR_VERSION}/ravendb-operator.yaml"
fi

echo "==> Installing RavenDB Kubernetes Operator (${OPERATOR_VERSION})..."
kubectl apply -f "$OPERATOR_URL"

echo "==> Waiting for operator controller to be ready..."
kubectl rollout status deployment/ravendb-operator-controller-manager \
  -n ravendb-operator-system \
  --timeout=120s

echo "==> Waiting for RavenDBCluster CRD to be established..."
kubectl wait --for=condition=Established \
  crd/ravendbclusters.ravendb.com \
  --timeout=60s

echo ""
echo "Operator ready. Next steps:"
echo "  1. Copy secrets:  cp k8s/secrets.yaml k8s/secrets.local.yaml"
echo "  2. Fill in keys:  \$EDITOR k8s/secrets.local.yaml"
echo "  3. Apply secrets: kubectl apply -f k8s/secrets.local.yaml"
echo "  4. Deploy all:    kubectl apply -k k8s/"
echo ""
echo "The RavenDBCluster will spin up a 3-node cluster and expose the Studio"
echo "via the LoadBalancer IP. Run: kubectl get svc -n hidden-city"
