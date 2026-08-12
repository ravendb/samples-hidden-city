#!/usr/bin/env bash
# k8s/start-k8s.sh -- bash port of start-k8s.ps1: full local kind cluster bring-up.
# Exists so CI (Linux runners) can exercise the exact same pinned-version flow
# Windows developers get from start-k8s.ps1.
#
# Usage:
#   bash k8s/start-k8s.sh                    # collect secrets/certs + create cluster + operator + full deploy
#   bash k8s/start-k8s.sh --skip-build        # skip docker build (image already loaded)
#   bash k8s/start-k8s.sh --skip-operator     # skip cert-manager/ingress-nginx/operator install
#   bash k8s/start-k8s.sh --delete-cluster    # delete the kind cluster and exit
#   bash k8s/start-k8s.sh --no-wait           # exit after readiness instead of blocking on port-forwards (also
#                                              # triggered automatically when $CI=true, as GitHub Actions sets)
#
# Prerequisites: kind, kubectl, helm fetched deterministically into .tools/ (see
# fetch_tool) -- no admin rights, no PATH mutation beyond this process, exact
# versions pinned in versions.env (repo root). System openssl is used as-is --
# the FireDaemon/Git-for-Windows PATH conflict documented in start-k8s.ps1's
# Resolve-OpenSslExe is Windows-only and doesn't apply here.
#
# NOT a replacement for k8s/deploy.sh / k8s/operator/install.sh -- those assume
# an already-existing cluster (any cluster, not just kind: no kind-specific
# logic, no Calico, no cert generation). This script owns kind provisioning
# (cluster create, Calico, CoreDNS) the same way start-k8s.ps1 does on Windows.
set -euo pipefail

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
NS="hidden-city"
IMAGE_TAG="hidden-city:latest"
CLUSTER_NAME="hidden-city"
SKIP_BUILD=false
SKIP_OPERATOR=false
DELETE_CLUSTER=false
NO_WAIT=false

for arg in "$@"; do
  case $arg in
    --skip-build) SKIP_BUILD=true ;;
    --skip-operator) SKIP_OPERATOR=true ;;
    --delete-cluster) DELETE_CLUSTER=true ;;
    --no-wait) NO_WAIT=true ;;
    --cluster-name=*) CLUSTER_NAME="${arg#*=}" ;;
  esac
done

step()  { printf '\n\033[1;36m==> %s\033[0m\n' "$*"; }
ok()    { printf '\033[0;32m  OK: %s\033[0m\n' "$*"; }
warn()  { printf '\033[0;33m  WARN: %s\033[0m\n' "$*"; }
fail()  { printf '\033[0;31m  ERROR: %s\033[0m\n' "$*" >&2; exit 1; }

if [[ "${CI:-}" != "true" ]]; then
  printf '\n\033[0;33m  Make sure Docker is running before continuing (kind, the RavenDB cluster,\033[0m\n'
  printf '\033[0;33m  and the app image all run as Docker containers).\033[0m\n'
fi

# versions.env is the single source of truth (repo root) -- see its header
# comment. VERSIONS_FILE lets the nightly CI job point at a freshly-resolved
# "latest upstream" file without touching this script.
set -a
source "${VERSIONS_FILE:-$root/versions.env}"
set +a

INGRESS_NGINX_URL="https://raw.githubusercontent.com/kubernetes/ingress-nginx/${INGRESS_NGINX_VERSION}/deploy/static/provider/kind/deploy.yaml"

get_raven_node_tags() {
  grep -E '^\s*-\s*tag:\s*\S+' "$root/k8s/ravendb/values.yaml" |
    sed -E 's/^\s*-\s*tag:\s*([^[:space:]]+).*/\1/'
}

# Downloads a specific version's official release asset straight from each
# project's own URL into .tools/<name>/<version>/, skipping the download if
# that exact versioned path is already cached. No admin rights, no permanent
# PATH mutation (only $PATH for this process, restored on exit). Just the
# function definition here (defining it does no network I/O) -- the full
# kind+kubectl+helm invocation happens after preflight below, so preflight
# still runs before any network call for the main flow. --delete-cluster
# is the one exception: it needs kind specifically before preflight even
# applies (see that branch).
fetch_tool() {
  local name="$1" version="$2" url="$3" exe_name="$4" archive_entry="${5:-}"
  local dir="$root/.tools/$name/$version"
  local exe_path="$dir/$exe_name"
  if [[ -f "$exe_path" ]]; then echo "$dir"; return 0; fi

  mkdir -p "$dir"
  echo "  Fetching $name $version..." >&2

  if [[ -n "$archive_entry" ]]; then
    local archive_path="$dir/download.tar.gz"
    curl -fsSL -o "$archive_path" "$url"
    tar -xzf "$archive_path" -C "$dir"
    mv -f "$dir/$archive_entry" "$exe_path"
    chmod +x "$exe_path"
    rm -f "$archive_path"
    find "$dir" -mindepth 1 -maxdepth 1 -type d -exec rm -rf {} +
  else
    curl -fsSL -o "$exe_path" "$url"
    chmod +x "$exe_path"
  fi

  [[ -f "$exe_path" ]] || fail "failed to fetch $name $version from $url"
  echo "$dir"
}
# kind/kubectl/helm all publish release assets per-OS/arch under this exact
# naming convention (linux|darwin, amd64|arm64) -- covers native Linux, CI
# runners, and both Intel and Apple Silicon Macs. Confirmed missing entirely
# in an earlier version of this script (hardcoded "linux"/"amd64"), which
# would have silently fetched an x86_64 Linux binary on an Apple Silicon Mac.
os="linux"
case "$(uname -s)" in
  Linux)  os="linux" ;;
  Darwin) os="darwin" ;;
  *) fail "unsupported OS: $(uname -s) -- this script supports Linux and macOS (use start-k8s.ps1 on Windows)" ;;
esac
arch="amd64"
case "$(uname -m)" in
  x86_64|amd64)  arch="amd64" ;;
  arm64|aarch64) arch="arm64" ;;
  *) fail "unsupported architecture: $(uname -m)" ;;
esac

# --- delete cluster shortcut ---
# Fetches only kind (not kubectl/helm -- unneeded for this) before deleting.
# Before this fetch existed here, --delete-cluster on a machine that had never
# fetched kind into .tools/ yet failed outright with "kind: command not
# found" -- confirmed by actually running it, a real bug found by testing.
if [[ "$DELETE_CLUSTER" == "true" ]]; then
  kind_dir=$(fetch_tool "kind" "$KIND_VERSION" "https://kind.sigs.k8s.io/dl/${KIND_VERSION}/kind-${os}-${arch}" "kind")
  export PATH="$kind_dir:$PATH"
  step "Deleting kind cluster '$CLUSTER_NAME'"
  kind delete cluster --name "$CLUSTER_NAME"
  ok "Cluster deleted"
  exit 0
fi

# Runs every check up front and reports a full pass/fail table, instead of
# failing one check at a time minutes into a `kind create cluster` run. Two
# kinds of check: machine state (Python/Docker/ports) and cross-file literal
# pin consistency (Dockerfile/docker-compose.yml/values.yaml/kind-config.yaml
# vs versions.env -- see versions.env's header comment for why these can't
# just be templated from one source).
preflight() {
  local failed=false
  printf '\n  Preflight:\n'

  # NOT gated on pyproject.toml's requires-python (>=3.11,<3.14) -- that range
  # is a real constraint only for the Local/docker-compose flow's `uv venv`,
  # which actually runs the pyravendb-dependent app code against the host
  # Python. This K8s flow never does that: the app runs inside the Docker
  # image (its own separately pinned python:3.13.14-slim, asserted below).
  # The host python3 here is only used by this script itself for small
  # JSON/YAML text munging (set_secret_placeholder, the RavenDB readiness
  # check) -- any reasonably modern Python 3 handles that fine. Gating this on
  # the app's version range was confirmed wrong in practice: Ubuntu 26.04's
  # default python3 is 3.14, which this check used to reject outright even
  # though nothing in this flow actually needed <3.14.
  local py_cmd=""
  command -v python3 >/dev/null 2>&1 && py_cmd=python3
  [[ -z "$py_cmd" ]] && command -v python >/dev/null 2>&1 && py_cmd=python
  if [[ -z "$py_cmd" ]]; then
    printf '    FAIL  %s\n' "python3 not found on PATH (needed internally by this script, not by the deployed app)"; failed=true
  else
    printf '    OK    %s found (%s)\n' "$py_cmd" "$("$py_cmd" --version 2>&1)"
  fi

  if command -v docker >/dev/null 2>&1 && docker info >/dev/null 2>&1; then
    local mem_bytes mem_gib
    mem_bytes=$(docker info --format '{{.MemTotal}}' 2>/dev/null || echo 0)
    mem_gib=$(( mem_bytes / 1024 / 1024 / 1024 ))
    if [[ "$mem_gib" -lt 2 ]]; then
      printf '    FAIL  Docker memory %sGiB is below the ~2GiB minimum for kind + Calico + RavenDB\n' "$mem_gib"; failed=true
    else
      [[ "$mem_gib" -lt 4 ]] && warn "Docker memory ${mem_gib}GiB is below the recommended 4GiB+ -- may be tight"
      printf '    OK    Docker memory: %sGiB\n' "$mem_gib"
    fi
  else
    printf '    OK    docker not responding yet -- checked again below\n'
  fi

  local port
  for port in 8001 8081; do
    if (exec 3<>"/dev/tcp/127.0.0.1/$port") 2>/dev/null; then
      exec 3<&- 3>&-
      printf '    FAIL  Port %s already in use\n' "$port"; failed=true
    else
      printf '    OK    Port %s free\n' "$port"
    fi
  done

  local dockerfile_from dockerfile_uv
  dockerfile_from=$(grep -oE '^FROM python:[[:alnum:].-]+' "$root/Dockerfile" | sed -E 's/FROM python://') || true
  if [[ "$dockerfile_from" == "$PYTHON_IMAGE_TAG" ]]; then
    printf '    OK    Dockerfile FROM python:%s matches versions.env PYTHON_IMAGE_TAG=%s\n' "$dockerfile_from" "$PYTHON_IMAGE_TAG"
  else
    printf '    FAIL  Dockerfile FROM python:%s does not match versions.env PYTHON_IMAGE_TAG=%s\n' "$dockerfile_from" "$PYTHON_IMAGE_TAG"; failed=true
  fi

  dockerfile_uv=$(grep -oE 'uv==[0-9.]+' "$root/Dockerfile" | head -1 | sed -E 's/uv==//') || true
  if [[ "$dockerfile_uv" == "$UV_VERSION" ]]; then
    printf '    OK    Dockerfile uv==%s matches versions.env UV_VERSION=%s\n' "$dockerfile_uv" "$UV_VERSION"
  else
    printf '    FAIL  Dockerfile uv==%s does not match versions.env UV_VERSION=%s\n' "$dockerfile_uv" "$UV_VERSION"; failed=true
  fi

  local compose_tag values_tag
  compose_tag=$(grep -oE 'image:\s*ravendb/ravendb:[[:alnum:].-]+' "$root/docker-compose.yml" | sed -E 's#.*ravendb/ravendb:##') || true
  values_tag=$(grep -oE 'image:\s*ravendb/ravendb:[[:alnum:].-]+' "$root/k8s/ravendb/values.yaml" | sed -E 's#.*ravendb/ravendb:##') || true
  if [[ "$compose_tag" == "$RAVENDB_IMAGE_TAG" && "$values_tag" == "$RAVENDB_IMAGE_TAG" ]]; then
    printf '    OK    RavenDB image tag agrees: docker-compose.yml=%s values.yaml=%s versions.env=%s\n' "$compose_tag" "$values_tag" "$RAVENDB_IMAGE_TAG"
  else
    printf '    FAIL  RavenDB image tag mismatch: docker-compose.yml=%s values.yaml=%s versions.env=%s\n' "$compose_tag" "$values_tag" "$RAVENDB_IMAGE_TAG"; failed=true
  fi

  local kind_img
  kind_img=$(grep -oE 'image:\s*\S+' "$root/k8s/kind-config.yaml" | awk '{print $2}') || true
  if [[ "$kind_img" == "$KIND_NODE_IMAGE" ]]; then
    printf '    OK    k8s/kind-config.yaml node image matches versions.env KIND_NODE_IMAGE\n'
  else
    printf '    FAIL  k8s/kind-config.yaml node image (%s) does not match versions.env KIND_NODE_IMAGE\n' "$kind_img"; failed=true
  fi

  # Checked here, not just later when the ravendb-license k8s Secret gets
  # created from this file: the RavenDB operator's admission webhook always
  # requires spec.licenseSecretRef to resolve, so a missing license.json is
  # guaranteed to fail eventually. A stale, already-cached RAVENDB_LICENSE
  # value in k8s/secrets.local.yaml can make "already set" print even though
  # this file no longer exists on disk -- those are two independent,
  # unsynchronized representations of the license. Checking it here means the
  # whole run fails in seconds, before Docker/kind/cert work even starts.
  if [[ -f "$root/license.json" ]]; then
    printf '    OK    license.json present at repo root (required for the ravendb-license k8s Secret)\n'
  else
    printf '    FAIL  license.json missing at repo root (required for the ravendb-license k8s Secret)\n'; failed=true
  fi

  printf '\n'
  [[ "$failed" == "true" ]] && fail "preflight failed -- fix the above before continuing."
  return 0
}
preflight

# --- prerequisites: kind/kubectl/helm fetched deterministically into .tools/ ---
step "kind/kubectl/helm ($KIND_VERSION / $KUBECTL_VERSION / $HELM_VERSION)"
kind_dir=$(fetch_tool "kind" "$KIND_VERSION" "https://kind.sigs.k8s.io/dl/${KIND_VERSION}/kind-${os}-${arch}" "kind")
kubectl_dir=$(fetch_tool "kubectl" "$KUBECTL_VERSION" "https://dl.k8s.io/release/${KUBECTL_VERSION}/bin/${os}/${arch}/kubectl" "kubectl")
helm_dir=$(fetch_tool "helm" "$HELM_VERSION" "https://get.helm.sh/helm-${HELM_VERSION}-${os}-${arch}.tar.gz" "helm" "${os}-${arch}/helm")
export PATH="$kind_dir:$kubectl_dir:$helm_dir:$PATH"
# GitHub Actions runs each workflow step in a fresh shell -- `export PATH`
# above only lasts for this process/step. Appending to $GITHUB_PATH (a no-op
# outside Actions, where that var is unset) makes kind/kubectl/helm resolve in
# later steps too, e.g. a `kind delete cluster` cleanup step after this one.
if [[ -n "${GITHUB_PATH:-}" ]]; then
  { echo "$kind_dir"; echo "$kubectl_dir"; echo "$helm_dir"; } >> "$GITHUB_PATH"
fi
ok "kind $KIND_VERSION, kubectl $KUBECTL_VERSION, helm $HELM_VERSION ready (.tools/)"

command -v docker >/dev/null 2>&1 || fail "docker not found -- install Docker (or Docker-in-Docker for CI) first."
docker info >/dev/null 2>&1 || fail "Docker is installed but the daemon isn't responding."
ok "docker found"

# Writes VALUE into FILE's "KEY: \"REPLACE_ME\"" line, safely handling
# multi-line values (e.g. license.json's pretty-printed JSON) -- a plain sed
# s/// can't hold a literal newline in its replacement text. python3 is a hard
# project dependency already (validated by preflight above), so this doesn't
# add a new one.
set_secret_placeholder() {
  local file="$1" key="$2" value="$3"
  KEY="$key" VALUE="$value" FILE="$file" python3 - <<'PYEOF'
import os, re
key = os.environ["KEY"]
value = os.environ["VALUE"]
path = os.environ["FILE"]
with open(path, "r", encoding="utf-8") as f:
    content = f.read()
escaped = value.replace("\\", "\\\\").replace('"', '\\"')
content = re.sub(rf'{re.escape(key)}:\s+"REPLACE_ME"', lambda _m: f'{key}: "{escaped}"', content, count=1)
with open(path, "w", encoding="utf-8") as f:
    f.write(content)
PYEOF
}

# Look for a value the user already provided somewhere else before asking
# again: 1) an environment variable, 2) the repo-root .env file, 3) an
# optional plain file, whole contents as the value (e.g. license.json). Sets
# FOUND_VALUE/FOUND_SOURCE and returns 0 on a hit (globals, not stdout, since
# license.json's value can contain newlines).
find_existing_value() {
  local name="$1" fallback_file="$2" placeholder="$3"
  local val="${!name:-}"
  if [[ -n "$val" && "$val" != "$placeholder" ]]; then
    FOUND_VALUE="$val"; FOUND_SOURCE="environment variable \$$name"; return 0
  fi
  if [[ -f "$root/.env" ]]; then
    val=$(grep -E "^${name}=" "$root/.env" | head -1 | cut -d= -f2-) || true
    if [[ -n "$val" && "$val" != "$placeholder" ]]; then
      FOUND_VALUE="$val"; FOUND_SOURCE=".env"; return 0
    fi
  fi
  if [[ -n "$fallback_file" && -f "$root/$fallback_file" ]]; then
    val=$(cat "$root/$fallback_file")
    if [[ -n "$val" ]]; then
      FOUND_VALUE="$val"; FOUND_SOURCE="$fallback_file"; return 0
    fi
  fi
  return 1
}

total_steps=10
[[ "$SKIP_BUILD" == "true" ]] && total_steps=$((total_steps - 1))
[[ "$SKIP_OPERATOR" == "true" ]] && total_steps=$((total_steps - 1))

# --- secrets + certs (no cluster required yet) ---
step "Secrets & RavenDB TLS certs"

secrets_file="$root/k8s/secrets.local.yaml"
if [[ ! -f "$secrets_file" ]]; then
  cp "$root/k8s/secrets.yaml" "$secrets_file"
  warn "Created k8s/secrets.local.yaml from template"
fi

# name|required|fallback_file|placeholder -- RAVENDB_LICENSE has no interactive
# prompt (silent/optional), matching start-k8s.ps1's Prompt=$false for it.
declare -a secret_defs=(
  "OPENAI_API_KEY|true||sk-..."
  "TRAVELPAYOUTS_TOKEN|false||..."
  "RAVENDB_LICENSE|false|license.json|"
)
for def in "${secret_defs[@]}"; do
  IFS='|' read -r name required fallback_file placeholder <<<"$def"

  if ! grep -qE "${name}:[[:space:]]+\"REPLACE_ME\"" "$secrets_file"; then
    ok "$name already set in k8s/secrets.local.yaml"
    continue
  fi

  if find_existing_value "$name" "$fallback_file" "$placeholder"; then
    set_secret_placeholder "$secrets_file" "$name" "$FOUND_VALUE"
    ok "$name reused from $FOUND_SOURCE -- not asking again"
    continue
  fi

  if [[ "$name" == "RAVENDB_LICENSE" ]]; then
    continue
  fi

  if [[ -t 0 ]]; then
    echo ""
    if [[ "$required" == "true" ]]; then
      echo "  $name is required for the agent to call GPT."
    else
      echo "  [optional] $name -- press Enter to skip."
    fi
    read -r -p "  Enter $name: " val
    if [[ -n "$val" ]]; then
      set_secret_placeholder "$secrets_file" "$name" "$val"
      ok "$name saved to k8s/secrets.local.yaml"
    elif [[ "$required" == "true" ]]; then
      warn "$name not set -- agent will fail to call GPT"
    fi
  elif [[ "$required" == "true" ]]; then
    warn "$name not set and not interactive (CI?) -- agent will fail to call GPT"
  fi
done

# Generates a local self-signed CA + server + client certificate chain for
# RavenDB's TLS requirement -- see start-k8s.ps1's Ensure-RavenDbCerts for the
# full rationale (extension requirements, -legacy PKCS12 packaging). Skipped
# entirely if a chain already exists AND its SAN list still matches the
# current node tags; on a stale SAN list, fails with a clear message rather
# than silently regenerating (see start-k8s.ps1 for why).
ensure_ravendb_certs() {
  local certs_dir="$1"; shift
  local node_tags=("$@")

  local required_files=(ca.crt server.crt server.pfx client.pfx)
  local all_present=true f
  for f in "${required_files[@]}"; do
    [[ -f "$certs_dir/$f" ]] || { all_present=false; break; }
  done

  command -v openssl >/dev/null 2>&1 || fail "openssl not found -- install it (e.g. apt-get install openssl)"

  if [[ "$all_present" == "true" ]]; then
    # Subset check, not exact match: the cert covering MORE than the
    # currently active tags is harmless (e.g. it was generated for a/b/c and
    # values.yaml later scaled down to just a for a license limit -- RavenDB
    # doesn't care about unused SAN entries). Only a MISSING SAN for a
    # currently active tag is the actual risk (a node that can't be reached).
    local expected=() tag
    for tag in "${node_tags[@]}"; do
      expected+=("DNS:${tag}.hiddencity.local" "DNS:${tag}-tcp.hiddencity.local")
    done
    local actual_joined
    actual_joined=$(openssl x509 -in "$certs_dir/server.crt" -noout -ext subjectAltName 2>/dev/null |
      grep -oE '(DNS|IP Address):[^, ]+' | sort) || true

    local missing=() e
    for e in "${expected[@]}"; do
      grep -qxF "$e" <<<"$actual_joined" || missing+=("$e")
    done

    if [[ -n "$actual_joined" && ${#missing[@]} -eq 0 ]]; then
      ok "RavenDB TLS certs already present in k8s/ravendb/certs -- reusing"
      return 0
    fi

    echo "  ERROR: k8s/ravendb/certs/server.crt is missing SANs for the current node tags." >&2
    echo "    Missing: ${missing[*]}" >&2
    echo "    Cert covers: $(tr '\n' ' ' <<<"$actual_joined")" >&2
    echo "    Fix: delete k8s/ravendb/certs and re-run to regenerate for the current node tags." >&2
    echo "    If a cluster is already bootstrapped, adding nodes needs the operator's manual" >&2
    echo "    topology-change procedure -- regenerating local cert files alone won't add them." >&2
    exit 1
  fi

  # Linux system openssl isn't subject to the FireDaemon/Git-for-Windows PATH
  # conflict that broke -legacy on Windows (see start-k8s.ps1's
  # Resolve-OpenSslExe) -- still probe it for a fast, clear failure if a
  # minimal container image lacks the legacy provider.
  #
  # This runs the REAL operation (a throwaway `pkcs12 -export -legacy`) as
  # the probe, not a proxy check. `openssl list -providers -legacy` looks
  # like a capability check but isn't valid input for every build's `list`
  # subcommand even when the legacy provider itself loads and works fine --
  # confirmed for real on FireDaemon OpenSSL 4.0.1 (Windows side): `list
  # -providers -legacy` fails with "Unknown option: -legacy" while `pkcs12
  # -export -legacy` (the actual command used below) succeeds outright on
  # that same install. Test the thing you actually need, not a stand-in.
  _legacy_probe_dir="$(mktemp -d)"
  openssl req -x509 -newkey rsa:2048 -keyout "$_legacy_probe_dir/k.key" \
    -out "$_legacy_probe_dir/k.crt" -days 1 -nodes -subj "/CN=legacy-probe" >/dev/null 2>&1
  openssl pkcs12 -export -legacy -out "$_legacy_probe_dir/k.pfx" \
    -inkey "$_legacy_probe_dir/k.key" -in "$_legacy_probe_dir/k.crt" -passout pass: >/dev/null 2>&1
  _legacy_ok=$?
  rm -rf "$_legacy_probe_dir"
  [[ $_legacy_ok -eq 0 ]] ||
    fail "openssl can't do a -legacy PKCS12 export (needed for the operator's cert format) -- install a full openssl package with the legacy provider."

  mkdir -p "$certs_dir"
  echo "  Generating self-signed RavenDB TLS chain in k8s/ravendb/certs..."

  local san_entries="" tag
  for tag in "${node_tags[@]}"; do
    san_entries+="DNS:${tag}.hiddencity.local,DNS:${tag}-tcp.hiddencity.local,"
  done
  san_entries="${san_entries%,}"

  cat > "$certs_dir/ca-ext.cnf" <<EOF
[req]
distinguished_name = dn
x509_extensions = ext
[dn]
[ext]
basicConstraints = critical, CA:TRUE
keyUsage = critical, keyCertSign, cRLSign
EOF

  cat > "$certs_dir/server-san.cnf" <<EOF
[req]
distinguished_name = dn
req_extensions = ext
[dn]
[ext]
subjectAltName = $san_entries
keyUsage = critical, digitalSignature, keyEncipherment
extendedKeyUsage = serverAuth, clientAuth
EOF

  cat > "$certs_dir/client-ext.cnf" <<EOF
[req]
distinguished_name = dn
req_extensions = ext
[dn]
[ext]
keyUsage = critical, digitalSignature, keyEncipherment
extendedKeyUsage = clientAuth
EOF

  openssl genrsa -out "$certs_dir/ca.key" 4096 2>/dev/null
  openssl req -x509 -new -nodes -key "$certs_dir/ca.key" -sha256 -days 3650 \
    -out "$certs_dir/ca.crt" -subj "/CN=RavenDB Demo CA" \
    -extensions ext -config "$certs_dir/ca-ext.cnf"

  openssl genrsa -out "$certs_dir/server.key" 2048 2>/dev/null
  openssl req -new -key "$certs_dir/server.key" -out "$certs_dir/server.csr" \
    -subj "/CN=${node_tags[0]}.hiddencity.local" -config "$certs_dir/server-san.cnf"
  openssl x509 -req -in "$certs_dir/server.csr" -CA "$certs_dir/ca.crt" -CAkey "$certs_dir/ca.key" \
    -CAcreateserial -out "$certs_dir/server.crt" -days 825 -sha256 \
    -extfile "$certs_dir/server-san.cnf" -extensions ext

  openssl genrsa -out "$certs_dir/client.key" 2048 2>/dev/null
  openssl req -new -key "$certs_dir/client.key" -out "$certs_dir/client.csr" \
    -subj "/CN=hidden-city-client" -config "$certs_dir/client-ext.cnf"
  openssl x509 -req -in "$certs_dir/client.csr" -CA "$certs_dir/ca.crt" -CAkey "$certs_dir/ca.key" \
    -CAcreateserial -out "$certs_dir/client.crt" -days 825 -sha256 \
    -extfile "$certs_dir/client-ext.cnf" -extensions ext

  openssl pkcs12 -export -legacy -out "$certs_dir/server.pfx" \
    -inkey "$certs_dir/server.key" -in "$certs_dir/server.crt" \
    -certfile "$certs_dir/ca.crt" -passout pass:
  openssl pkcs12 -export -legacy -out "$certs_dir/client.pfx" \
    -inkey "$certs_dir/client.key" -in "$certs_dir/client.crt" \
    -certfile "$certs_dir/ca.crt" -passout pass:

  [[ -f "$certs_dir/server.pfx" && -f "$certs_dir/client.pfx" ]] ||
    fail "RavenDB cert generation failed -- see openssl output above."
  ok "RavenDB TLS chain generated"
}

raven_certs_dir="$root/k8s/ravendb/certs"
# Not `mapfile` -- macOS ships bash 3.2 (GPLv3 licensing) by default, which
# predates mapfile/readarray (bash 4.0+). This loop is 3.2-compatible.
raven_node_tags=()
while IFS= read -r tag; do raven_node_tags+=("$tag"); done < <(get_raven_node_tags)
ensure_ravendb_certs "$raven_certs_dir" "${raven_node_tags[@]}"

# license.json's presence is already asserted in preflight(), at the very
# start of the run -- no need to re-check it here.

# --- kind cluster ---
step "kind cluster '$CLUSTER_NAME'"
if kind get clusters 2>/dev/null | grep -qx "$CLUSTER_NAME"; then
  ok "Cluster already exists -- reusing"
else
  echo "  Creating cluster (this takes ~1 min)..."
  kind create cluster --name "$CLUSTER_NAME" --config "$root/k8s/kind-config.yaml" ||
    fail "kind create cluster failed -- see output above."
  ok "Cluster created"
fi
# NOT `kubectl config use-context` -- a kind cluster's existence (a set of
# Docker containers) and its kubeconfig entry (a file on whatever machine/user
# ran `kind create cluster`) are independent state. The "reusing" branch above
# only proves the cluster exists in Docker; if this environment's kubeconfig
# never had the context (fresh CI runner sharing a Docker socket, cleared
# ~/.kube/config, a different user/environment than the one that originally
# created it -- confirmed hitting this for real: same Docker Desktop engine,
# cluster created from Windows, this environment being WSL2 Ubuntu with its
# own separate kubeconfig), `use-context` fails with "no context exists".
# `kind export kubeconfig` (re)writes the entry and sets it current either
# way -- safe and idempotent whether the cluster was just created or reused.
kind export kubeconfig --name "$CLUSTER_NAME" >/dev/null

# kind-config.yaml disables kindnet (the default CNI) -- it doesn't reliably
# hairpin a pod's own traffic back to itself through its own Service ClusterIP,
# which RavenDB's self-signed-cert startup check needs
# (AssertServerCanContactItselfWhenAuthIsOn). Calico handles this correctly.
# Installed via the Tigera Operator, not the raw calico.yaml manifest -- see
# start-k8s.ps1 for why (permission errors on kind nodes with the raw manifest).
echo "  Installing Calico CNI (via Tigera Operator)..."
kubectl create -f "https://raw.githubusercontent.com/projectcalico/calico/${CALICO_VERSION}/manifests/tigera-operator.yaml" 2>/dev/null || true
kubectl -n tigera-operator rollout status deployment/tigera-operator --timeout=90s

for crd_name in installations.operator.tigera.io apiservers.operator.tigera.io; do
  crd_found=false
  for i in $(seq 1 30); do
    kubectl get crd "$crd_name" >/dev/null 2>&1 && { crd_found=true; break; }
    sleep 2
  done
  [[ "$crd_found" == "true" ]] || fail "CRD $crd_name never appeared -- Tigera operator install likely failed."
done
kubectl wait --for=condition=Established crd/installations.operator.tigera.io crd/apiservers.operator.tigera.io --timeout=60s

if ! kubectl get installation.operator.tigera.io default >/dev/null 2>&1; then
  kubectl create -f - <<'EOF'
apiVersion: operator.tigera.io/v1
kind: Installation
metadata:
  name: default
spec:
  calicoNetwork:
    ipPools:
    - name: default-ipv4-ippool
      blockSize: 26
      cidr: 192.168.0.0/16
      encapsulation: VXLANCrossSubnet
      natOutgoing: Enabled
      nodeSelector: all()
---
apiVersion: operator.tigera.io/v1
kind: APIServer
metadata:
  name: default
spec: {}
EOF
fi
kubectl wait --for=condition=Ready node --all --timeout=180s ||
  fail "nodes did not become Ready -- Calico CNI install likely failed."
ok "Calico ready"

# --- namespace + k8s Secret objects (needs the cluster to exist) ---
step "Applying secrets to the cluster"
kubectl create namespace "$NS" --dry-run=client -o yaml | kubectl apply -f - >/dev/null
kubectl apply -f "$secrets_file" >/dev/null
ok "hidden-city-secrets applied"

declare -a raven_secret_defs=(
  "ravendb-license|$root/license.json|license.json"
  "ravendb-cert|$raven_certs_dir/server.pfx|server.pfx"
  "ravendb-ca-cert|$raven_certs_dir/ca.crt|ca.crt"
  "ravendb-client-cert|$raven_certs_dir/client.pfx|client.pfx"
)
for def in "${raven_secret_defs[@]}"; do
  IFS='|' read -r sec_name source_file from_file <<<"$def"
  if [[ ! -f "$source_file" ]]; then
    # Was warn-and-skip: the RavenDB Helm install a few steps later
    # references all four of these by name (spec.licenseSecretRef,
    # certificate refs), so a silently-skipped secret here is guaranteed to
    # resurface as a cryptic admission-webhook or cert-mount failure
    # downstream. license.json's existence is already asserted in
    # preflight(); the three cert files are asserted right after generation
    # in ensure_ravendb_certs(). Reaching this branch means one disappeared
    # between then and now -- fail loudly instead of producing a cluster
    # that looks like it's coming up and isn't.
    fail "can't create secret $sec_name -- source file missing: $source_file"
  fi
  kubectl create secret generic "$sec_name" -n "$NS" "--from-file=${from_file}=${source_file}" --dry-run=client -o yaml |
    kubectl apply -f - >/dev/null
done
ok "RavenDB license/cert secrets applied"

# --- docker build ---
image_rebuilt=false
if [[ "$SKIP_BUILD" != "true" ]]; then
  step "Building Docker image  ->  $IMAGE_TAG"
  docker build -t "$IMAGE_TAG" "$root" || fail "docker build failed"
  ok "Image built"
  echo "  Loading image into kind cluster..."
  kind load docker-image "$IMAGE_TAG" --name "$CLUSTER_NAME"
  ok "Image loaded into kind"
  image_rebuilt=true
fi

# --- cert-manager + ingress-nginx + operator (all via Helm/kubectl) ---
if [[ "$SKIP_OPERATOR" != "true" ]]; then
  step "Installing cert-manager, ingress-nginx, and the RavenDB Operator"

  echo "  Installing cert-manager ($CERT_MANAGER_VERSION)..."
  kubectl apply -f "https://github.com/cert-manager/cert-manager/releases/download/${CERT_MANAGER_VERSION}/cert-manager.yaml"
  kubectl wait --for=condition=Available deployment --all -n cert-manager --timeout=120s ||
    fail "cert-manager did not become Available in time."
  ok "cert-manager ready"

  echo "  Installing ingress-nginx (kind provider)..."
  kubectl apply -f "$INGRESS_NGINX_URL"
  kubectl wait --namespace ingress-nginx --for=condition=ready pod \
    --selector=app.kubernetes.io/component=controller --timeout=120s ||
    fail "ingress-nginx controller pod did not become ready in time."
  ok "ingress-nginx ready"

  echo "  Adding RavenDB operator Helm repo..."
  helm repo add ravendb-operator https://ravendb.github.io/ravendb-operator/helm >/dev/null
  helm repo update >/dev/null

  echo "  Installing RavenDB Kubernetes Operator..."
  helm upgrade --install ravendb-operator ravendb-operator/ravendb-operator \
    -n ravendb-operator-system --create-namespace \
    --version "$RAVENDB_OPERATOR_CHART_VERSION" || fail "helm install of ravendb-operator failed."
  kubectl rollout status deployment -n ravendb-operator-system -l app.kubernetes.io/name=ravendb-operator --timeout=120s ||
    fail "ravendb-operator deployment did not roll out in time."
  kubectl wait --for=condition=Established crd/ravendbclusters.ravendb.ravendb.io --timeout=60s ||
    fail "ravendbclusters CRD was not Established in time."
  ok "Operator ready"
fi

# --- RavenDB cluster (Helm chart, not kustomize) ---
step "Installing RavenDB cluster  (helm upgrade --install ravendb-cluster ...)"
helm upgrade --install ravendb-cluster ravendb-operator/ravendb-cluster \
  -n "$NS" --create-namespace -f "$root/k8s/ravendb/values.yaml" \
  --version "$RAVENDB_CLUSTER_CHART_VERSION" || fail "helm install of ravendb-cluster failed."
ok "RavenDB cluster chart applied"

# --- CoreDNS: make each node's public hostname resolve inside the cluster ---
# See start-k8s.ps1 for the full rationale (no real DNS for *.hiddencity.local;
# points at each ravendb-<tag> Service's stable ClusterIP; only works because
# Calico, not kindnet, correctly hairpins a pod's traffic back to itself).
step "Configuring CoreDNS for RavenDB node hostnames"

node_tags=()
while IFS= read -r tag; do node_tags+=("$tag"); done < <(get_raven_node_tags)
hosts_lines=()
for tag in "${node_tags[@]}"; do
  svc_ip=""
  for i in $(seq 1 45); do
    svc_ip=$(kubectl get svc "ravendb-$tag" -n "$NS" -o jsonpath='{.spec.clusterIP}' 2>/dev/null || true)
    [[ -n "$svc_ip" ]] && break
    sleep 2
  done
  if [[ -z "$svc_ip" ]]; then
    warn "Could not find Service ravendb-$tag -- skipping its DNS entry"
    continue
  fi
  hosts_lines+=("           $svc_ip $tag.hiddencity.local $tag-tcp.hiddencity.local")
done

if [[ ${#hosts_lines[@]} -gt 0 ]]; then
  hosts_entries=$(printf '%s\n' "${hosts_lines[@]}")
  kubectl apply -f - >/dev/null <<EOF
apiVersion: v1
kind: ConfigMap
metadata:
  name: coredns
  namespace: kube-system
data:
  Corefile: |
    .:53 {
        errors
        health {
           lameduck 5s
        }
        ready
        hosts {
$hosts_entries
           fallthrough
        }
        kubernetes cluster.local in-addr.arpa ip6.arpa {
           pods insecure
           fallthrough in-addr.arpa ip6.arpa
           ttl 30
        }
        prometheus :9153
        forward . /etc/resolv.conf {
           max_concurrent 1000
        }
        cache 30 {
           disable success cluster.local
           disable denial cluster.local
        }
        loop
        reload
        loadbalance
    }
EOF
  kubectl rollout restart deployment coredns -n kube-system >/dev/null
  kubectl rollout status deployment coredns -n kube-system --timeout=60s
  ok "CoreDNS configured for node(s): $(IFS=,; echo "${node_tags[*]}")"
else
  warn "No RavenDB node Services found -- CoreDNS not configured, bootstrap will likely fail"
fi

# --- wait for RavenDB ---
# Before app manifests, not after: the agent pod's own startup used to race
# RavenDB's readiness (it starts trying to create the RavenDB *database*
# immediately), and a single failed attempt there was silently swallowed --
# confirmed in practice serving 500s forever with the cluster otherwise
# healthy. src/db/seed.py's ensure_database now retries that on its own, so
# this reordering is defense in depth (fewer pods ever need to exercise that
# retry path), not the only thing standing between here and that bug.
step "Waiting for RavenDB cluster (60-120s)"
echo "  Operator is: creating PVCs -> starting pods -> forming Raft quorum -> issuing TLS certs"
echo ""

raven_ready=false
for i in $(seq 1 40); do
  json=$(kubectl get ravendbcluster ravendb-cluster -n "$NS" -o json 2>/dev/null || true)
  if [[ -n "$json" ]]; then
    status=$(printf '%s' "$json" | python3 -c "
import json, sys
try:
    d = json.load(sys.stdin)
    conds = d.get('status', {}).get('conditions', [])
    ready = next((c for c in conds if c.get('type') == 'Ready'), None)
    print(ready.get('status') if ready else '')
except Exception:
    print('')
" 2>/dev/null || true)
    [[ "$status" == "True" ]] && { raven_ready=true; break; }
  fi
  printf '  [%2d/40] Not ready yet... (%s)\n' "$i" "$(date +%H:%M:%S)"
  sleep 5
done

if [[ "$raven_ready" == "true" ]]; then
  ok "RavenDB cluster Ready"
else
  # In CI/--no-wait this IS the smoke test -- a non-Ready cluster must fail
  # the run, not just warn and let the script carry on to a green exit 0.
  # Interactively, warn-and-continue is still useful: it leaves the cluster up
  # so a developer can dig in with the commands below instead of losing it.
  if [[ "$NO_WAIT" == "true" || "${CI:-}" == "true" ]]; then
    fail "RavenDB did not reach Ready within 200s (see kubectl describe ravendbcluster ravendb-cluster -n $NS)."
  fi
  warn "RavenDB did not reach Ready in time. Check:"
  warn "  kubectl describe ravendbcluster ravendb-cluster -n $NS"
  warn "  kubectl get pods -n $NS"
fi

# --- deploy app manifests ---
step "Deploying app manifests"
kubectl apply -f "$root/k8s/namespace.yaml"
kubectl apply -f "$secrets_file"
kubectl apply -k "$root/k8s/"
ok "Manifests applied"

# `kubectl apply` only triggers a new rollout when the manifest text itself
# changes -- with a mutable `hidden-city:latest` tag, a rebuilt image loaded
# under the same tag leaves the Deployment's pod template textually identical,
# so already-running pods keep serving the OLD image forever until something
# forces a restart (confirmed in practice: pods stayed up unchanged after a
# rebuild+reload, still serving stale code). Force it whenever this run
# actually rebuilt the image -- not on every run, so an unrelated
# --skip-operator/config-only re-run doesn't bounce pods for no reason.
if [[ "$image_rebuilt" == "true" ]]; then
  echo "  Restarting agent/worker to pick up the freshly built image..."
  kubectl rollout restart deployment/agent -n "$NS" >/dev/null
  kubectl rollout restart deployment/subscription-worker -n "$NS" >/dev/null
fi

# 1200s, not 120s: k8s/agent/deployment.yaml's own readiness/liveness probes
# already budget ~20 minutes for this exact case (its own comment: "_startup()
# runs the Travelpayouts bulk scraper synchronously before uvicorn serves...
# cold start is easily 10+ minutes"). This step's timeout used to be far
# shorter than what the pod itself is configured to tolerate -- harmless
# before database creation reliably succeeded (a failed, swallowed seed made
# startup fast, for the wrong reason), but a real bottleneck now that it does.
step "Waiting for agent deployment (up to ~20 min on a cold DB -- see k8s/agent/deployment.yaml)"
kubectl rollout status deployment/agent -n "$NS" --timeout=1200s || fail "agent deployment did not roll out in time."
ok "Agent deployment ready"

# --- port-forwards (skipped for --no-wait / CI: readiness above is the signal) ---
if [[ "$NO_WAIT" == "true" || "${CI:-}" == "true" ]]; then
  echo ""
  ok "Infra smoke test complete -- RavenDB and agent are Ready (--no-wait/CI: skipping port-forwards)"
  exit 0
fi

echo ""
echo "  Starting port-forwards..."
first_node_tag=$(get_raven_node_tags | head -1)

kubectl port-forward svc/agent-svc 8001:80 -n "$NS" >/dev/null 2>&1 &
pf_agent=$!
kubectl port-forward "svc/ravendb-$first_node_tag" 8081:443 -n "$NS" >/dev/null 2>&1 &
pf_raven=$!

cleanup() {
  echo ""
  echo "  Stopping port-forwards..."
  kill "$pf_agent" "$pf_raven" 2>/dev/null || true
  echo "  Done. Cluster '$CLUSTER_NAME' is still running."
  echo "  To delete it: bash k8s/start-k8s.sh --delete-cluster"
}
trap cleanup EXIT

echo ""
echo "  ============================================="
ok "Agent:          http://localhost:8001"
ok "Swagger UI:     http://localhost:8001/docs"
ok "RavenDB Studio: https://localhost:8081  (self-signed cert -- browser will warn, click through)"
echo "  ============================================="
echo ""
echo "  Press Ctrl+C to stop port-forwards."
echo "  To delete the cluster: bash k8s/start-k8s.sh --delete-cluster"
echo ""

wait
