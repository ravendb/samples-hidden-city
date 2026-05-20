#!/usr/bin/env bash
# Install the RavenDB Kubernetes Operator.
# Check for the latest release before running:
#   https://github.com/ravendb/ravendb-k8s-operator/releases
set -euo pipefail

OPERATOR_URL="https://github.com/ravendb/ravendb-k8s-operator/releases/latest/download/ravendb-operator.yaml"

echo "Installing RavenDB Kubernetes Operator..."
kubectl apply -f "$OPERATOR_URL"

echo "Waiting for operator to be ready..."
kubectl rollout status deployment/ravendb-operator-controller-manager \
  -n ravendb-operator-system --timeout=120s

echo "Verifying CRD registration..."
kubectl get crd ravendbclusters.ravendb.com

echo "Operator ready. Contact Omer for internals questions."
