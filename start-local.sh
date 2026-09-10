#!/usr/bin/env bash
# start-local.sh -- macOS/Linux/WSL2 equivalent of `.\start.ps1 -Mode Local`:
# venv + deps (uv), .env/license, docker-compose RavenDB, seed, agent.
#
# Usage:
#   bash start-local.sh                # full flow
#   bash start-local.sh --skip-seed    # skip seed_local (db already seeded)
#   bash start-local.sh --worker       # also start the price-drop worker in the background
#
# Requires: Docker (Docker Desktop on macOS, Docker Engine/Desktop on Linux), bash, curl.
set -euo pipefail

root="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$root"

SKIP_SEED=false
WORKER=false
for arg in "$@"; do
  case "$arg" in
    --skip-seed) SKIP_SEED=true ;;
    --worker) WORKER=true ;;
    *) echo "Unknown flag: $arg" >&2; exit 1 ;;
  esac
done

ok()   { echo "  OK: $*"; }
warn() { echo "  WARN: $*"; }
step() { echo -e "\n[$1/$2] $3"; }

echo
echo "  Make sure Docker is running before continuing."

# --- uv (pinned version from versions.env, single source of truth) ---
UV_VERSION="$(grep -E '^UV_VERSION=' versions.env | cut -d= -f2 || true)"

find_uv() {
  if command -v uv >/dev/null 2>&1; then command -v uv; return; fi
  for c in "$HOME/.local/bin/uv" "$HOME/.cargo/bin/uv"; do
    [ -x "$c" ] && { echo "$c"; return; }
  done
}

UV="$(find_uv || true)"
if [ -n "$UV" ] && [ -n "$UV_VERSION" ]; then
  actual="$("$UV" --version 2>/dev/null || true)"
  if [[ "$actual" != *"$UV_VERSION"* ]]; then
    warn "Found uv ($actual) but versions.env pins UV_VERSION=$UV_VERSION -- installing the pinned version"
    UV=""
  fi
fi
if [ -z "$UV" ]; then
  warn "uv not found or not at the pinned version -- installing ${UV_VERSION:-latest}..."
  curl -LsSf "https://astral.sh/uv/${UV_VERSION:+$UV_VERSION/}install.sh" | sh
  UV="$(find_uv || true)"
  [ -n "$UV" ] || { echo "  ERROR: uv installation failed. Reopen your shell and try again." >&2; exit 1; }
  ok "uv installed ($("$UV" --version))"
fi

# --- venv (auto-create or recreate if wrong Python version) ---
PY="$root/.venv/bin/python"
if [ -x "$PY" ]; then
  minor="$("$PY" -c 'import sys; print(sys.version_info.minor)')"
  if [ "$minor" -ge 14 ]; then
    warn "Python 3.$minor in .venv is not compatible (pyravendb requires <3.14). Recreating venv..."
    rm -rf "$root/.venv"
  fi
fi
if [ ! -x "$PY" ]; then
  echo -e "\n  Creating venv (Python 3.11-3.13)..."
  "$UV" venv --python ">=3.11,<3.14" "$root/.venv"
  echo "  Installing dependencies..."
  "$UV" pip install --python "$PY" -e ".[dev]"
  ok "venv ready"
fi

total_steps=3; [ "$SKIP_SEED" = true ] && total_steps=2

# --- step 0: .env + license ---
if [ ! -f "$root/.env" ]; then
  warn ".env not found -- copying from .env.example"
  cp "$root/.env.example" "$root/.env"
fi

if [ -f "$root/license.json" ]; then
  export RAVENDB_LICENSE="$(cat "$root/license.json")"
  ok "License loaded from license.json"
else
  warn "license.json not found -- RavenDB will run in Developer mode (3 GB limit, 1 node)."
  warn "To use your license: save the license JSON to license.json in the repo root."
fi

# --- API keys check + optional prompt ---
get_env_value() { grep -E "^$1=" "$root/.env" | head -1 | cut -d= -f2- || true; }
set_env_value() {
  if grep -qE "^$1=" "$root/.env"; then
    sed -i.bak "s|^$1=.*|$1=$2|" "$root/.env" && rm -f "$root/.env.bak"
  else
    echo "$1=$2" >> "$root/.env"
  fi
}

any_missing=false
declare -A KEY_DESC=(
  [OPENAI_API_KEY]="OpenAI API key (agent won't start without it)"
  [TRAVELPAYOUTS_TOKEN]="Travelpayouts / Aviasales Data API token (optional, bulk scraper)"
)
declare -A KEY_REQUIRED=(
  [OPENAI_API_KEY]=true
  [TRAVELPAYOUTS_TOKEN]=false
)
for key in OPENAI_API_KEY TRAVELPAYOUTS_TOKEN; do
  current="$(get_env_value "$key")"
  # A fresh .env copied from .env.example is blank -- that's "unset".
  if [ -z "$current" ]; then
    if [ "$any_missing" = false ]; then
      echo
      warn "Some API keys are missing in .env. Enter values now or press Enter to skip."
      echo "  Skipped keys fall back to fixture data. See README for where to get them."
      any_missing=true
    fi
    label="[optional]"; [ "${KEY_REQUIRED[$key]}" = true ] && label="[required]"
    read -r -p "  $label ${KEY_DESC[$key]}: " val
    if [ -n "$val" ]; then
      set_env_value "$key" "$val"
      ok "$key saved to .env"
    elif [ "${KEY_REQUIRED[$key]}" = true ]; then
      warn "$key not set -- agent will fail to call the LLM"
    fi
  fi
done
[ "$any_missing" = true ] && echo

# --- step 1: RavenDB ---
step 1 "$total_steps" "Starting RavenDB (docker compose)..."
docker compose up -d --wait ravendb
ok "RavenDB ready"
echo "  RavenDB Studio: http://localhost:8080"

# --- step 2: seed (optional) ---
if [ "$SKIP_SEED" = false ]; then
  step 2 "$total_steps" "Seeding airports and fixture routes..."
  "$PY" -m scripts.seed_local
  ok "Data seeded"
fi

# --- worker in the background (optional) ---
WORKER_PID=""
if [ "$WORKER" = true ]; then
  echo -e "\n  Starting price-drop worker in the background..."
  "$PY" -m src.worker.run &
  WORKER_PID=$!
  ok "Worker started (pid $WORKER_PID)"
fi

# --- last step: agent (foreground) ---
step "$total_steps" "$total_steps" "Starting agent at http://localhost:8001  (Ctrl+C to stop)"
echo "  Swagger UI:    http://localhost:8001/docs"
echo "  RavenDB Studio: http://localhost:8080"
echo

cleanup() {
  echo -e "\n  Stopping RavenDB (docker compose down) to release its ports..."
  docker compose down
  [ -n "$WORKER_PID" ] && kill "$WORKER_PID" 2>/dev/null || true
  ok "Stopped -- ports 8080/38888 released"
}
trap cleanup EXIT

"$root/.venv/bin/uvicorn" src.agent.app:app --reload --port 8001
