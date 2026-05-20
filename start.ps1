# start.ps1 — starts the full local stack in one command
#
# Usage:
#   .\start.ps1             # RavenDB + seed + agent
#   .\start.ps1 -Worker     # also starts the price-drop worker in a separate window
#   .\start.ps1 -SkipSeed   # skip seeding when the database is already populated

param(
    [switch]$Worker,    # start the subscription worker in a separate window
    [switch]$SkipSeed   # skip seed_local (when the database is already seeded)
)

$ErrorActionPreference = "Stop"
$root = $PSScriptRoot

function Write-Step($n, $total, $msg) {
    Write-Host "`n[$n/$total] $msg" -ForegroundColor Cyan
}

function Write-Ok($msg) {
    Write-Host "  OK: $msg" -ForegroundColor Green
}

function Write-Warn($msg) {
    Write-Host "  WARN: $msg" -ForegroundColor Yellow
}

$totalSteps = if ($SkipSeed) { 2 } else { 3 }

# --- step 0: .env ---
if (-not (Test-Path "$root\.env")) {
    Write-Warn ".env not found — copying from .env.example"
    Copy-Item "$root\.env.example" "$root\.env"
    Write-Warn "Fill in API keys in .env if you need live flight search."
}

# --- step 1: RavenDB ---
Write-Step 1 $totalSteps "Starting RavenDB (docker compose)..."
docker compose up -d ravendb
if ($LASTEXITCODE -ne 0) {
    Write-Error "docker compose up failed. Is Docker Desktop running?"
    exit 1
}

Write-Host "  Waiting for RavenDB at http://localhost:8080..." -ForegroundColor Gray
$timeout = 60
$elapsed = 0
$ready = $false
while (-not $ready -and $elapsed -lt $timeout) {
    Start-Sleep -Seconds 2
    $elapsed += 2
    try {
        $null = Invoke-WebRequest -Uri "http://localhost:8080/alive" -TimeoutSec 2 -ErrorAction Stop
        $ready = $true
    } catch { }
}

if (-not $ready) {
    Write-Error "RavenDB did not respond within $timeout seconds. Check: docker compose logs ravendb"
    exit 1
}
Write-Ok "RavenDB ready"

# --- step 2: seed (optional) ---
if (-not $SkipSeed) {
    Write-Step 2 $totalSteps "Seeding airports and fixture routes..."
    uv run python -m scripts.seed_local
    if ($LASTEXITCODE -ne 0) {
        Write-Error "Seeding failed. Check the logs above."
        exit 1
    }
    Write-Ok "Data seeded"
}

# --- worker in a separate window (optional) ---
if ($Worker) {
    Write-Host "`n  Starting price-drop worker in a separate window..." -ForegroundColor Gray
    Start-Process powershell -ArgumentList "-NoExit", "-Command", "cd '$root'; uv run python -m src.worker.run"
    Write-Ok "Worker started (separate window)"
}

# --- last step: agent (foreground) ---
Write-Step $totalSteps $totalSteps "Starting agent at http://localhost:8000  (Ctrl+C to stop)"
Write-Host "  Swagger UI:    http://localhost:8000/docs" -ForegroundColor Gray
Write-Host "  RavenDB Studio: http://localhost:8080`n" -ForegroundColor Gray

uv run uvicorn src.agent.app:app --reload --port 8000