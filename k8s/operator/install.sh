#!/usr/bin/env bash
# Install cert-manager (operator prerequisite) and the RavenDB Kubernetes Operator.
#
# Operator project: https://github.com/ravendb/ravendb-operator
# Run once per cluster. Safe to re-run (helm upgrade --install is idempotent).
set -euo pipefail

# versions.env is the single source of truth (repo root). Capture any
# explicitly pre-exported override BEFORE sourcing -- `source` unconditionally
# reassigns, so it would otherwise clobber a caller's override with the file's
# default.
_user_cert_manager_version="${CERT_MANAGER_VERSION:-}"
_user_operator_chart_version="${OPERATOR_CHART_VERSION:-}"

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
set -a
source "${VERSIONS_FILE:-$root/versions.env}"
set +a

CERT_MANAGER_VERSION="${_user_cert_manager_version:-$CERT_MANAGER_VERSION}"
OPERATOR_CHART_VERSION="${_user_operator_chart_version:-$RAVENDB_OPERATOR_CHART_VERSION}"

echo "==> Installing cert-manager (${CERT_MANAGER_VERSION})..."
kubectl apply -f "https://github.com/cert-manager/cert-manager/releases/download/${CERT_MANAGER_VERSION}/cert-manager.yaml"

echo "==> Waiting for cert-manager to be ready..."
kubectl wait --for=condition=Available deployment --all -n cert-manager --timeout=120s

echo "==> Adding the RavenDB operator Helm repo..."
helm repo add ravendb-operator https://ravendb.github.io/ravendb-operator/helm
helm repo update

echo "==> Installing the RavenDB Kubernetes Operator..."
version_flag=()
if [[ -n "$OPERATOR_CHART_VERSION" ]]; then
  version_flag=(--version "$OPERATOR_CHART_VERSION")
fi
helm upgrade --install ravendb-operator ravendb-operator/ravendb-operator \
  -n ravendb-operator-system --create-namespace \
  "${version_flag[@]}"

echo "==> Waiting for the operator controller to be ready..."
kubectl rollout status deployment -n ravendb-operator-system -l app.kubernetes.io/name=ravendb-operator --timeout=120s

echo "==> Waiting for the RavenDBCluster CRD to be established..."
kubectl wait --for=condition=Established \
  crd/ravendbclusters.ravendb.ravendb.io \
  --timeout=60s

echo ""
echo "Operator ready. Next steps:"
echo "  1. Create the RavenDB license + certificate secrets referenced by"
echo "     k8s/ravendb/values.yaml (licenseSecretRef, clusterCertSecretRef,"
echo "     caCertSecretRef, clientCertSecretRef) — see the self-signed walkthrough"
echo "     at https://github.com/ravendb/ravendb-operator/tree/main/examples/tls/selfsigned"
echo "  2. Fill in k8s/ravendb/values.yaml with your node hostnames and secret names"
echo "  3. Deploy the RavenDB cluster:"
echo "       helm upgrade --install ravendb-cluster ravendb-operator/ravendb-cluster \\"
echo "         -n hidden-city --create-namespace -f k8s/ravendb/values.yaml"
echo "  4. Deploy the rest of the app:  kubectl apply -k k8s/"
echo ""
