# start.ps1 -- starts the full local stack in one command
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

function Find-Uv {
    $found = Get-Command uv -ErrorAction SilentlyContinue
    if ($found) { return $found.Source }
    $candidates = @(
        "$env:LOCALAPPDATA\uv\uv.exe",
        "$env:USERPROFILE\.local\bin\uv.exe",
        "$env:USERPROFILE\.cargo\bin\uv.exe"
    )
    foreach ($c in $candidates) {
        if (Test-Path $c) { return $c }
    }
    return $null
}

function Get-EnvValue($lines, $key) {
    $line = $lines | Where-Object { $_ -match "^$key=(.+)$" } | Select-Object -First 1
    if ($line -match "^$key=(.+)$") { return $Matches[1].Trim() }
    return $null
}

function Set-EnvValue($file, $key, $value) {
    $content = Get-Content $file
    if ($content -match "^$key=") {
        $content = $content -replace "^$key=.*", "$key=$value"
    } else {
        $content += "$key=$value"
    }
    $content | Set-Content $file
}

# --- venv setup (auto-create or recreate if wrong Python version) ---
$python  = "$root\.venv\Scripts\python.exe"
$uvicorn = "$root\.venv\Scripts\uvicorn.exe"

if (Test-Path $python) {
    $pyver = & $python -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')"
    $minor = [int]($pyver.Split('.')[1])
    if ($minor -ge 14) {
        Write-Warn "Python $pyver in .venv is not compatible (pyravendb requires <3.14). Recreating venv..."
        Remove-Item -Recurse -Force "$root\.venv"
    }
}

if (-not (Test-Path $python)) {
    $uv = Find-Uv
    if (-not $uv) {
        Write-Error "uv not found. Install it: winget install astral-sh.uv, then reopen this terminal."
        exit 1
    }
    Write-Host "`n  Creating venv (Python 3.11-3.13)..." -ForegroundColor Gray
    & $uv venv --python ">=3.11,<3.14" "$root\.venv"
    if ($LASTEXITCODE -ne 0) {
        Write-Error "Failed to create venv. Install Python 3.11, 3.12, or 3.13 and try again."
        exit 1
    }
    Write-Host "  Installing dependencies..." -ForegroundColor Gray
    & $uv pip install --python "$root\.venv\Scripts\python.exe" -e "$root[dev]"
    if ($LASTEXITCODE -ne 0) {
        Write-Error "Failed to install dependencies."
        exit 1
    }
    Write-Ok "venv ready"
}

$totalSteps = if ($SkipSeed) { 2 } else { 3 }

# --- step 0: .env + license ---
if (-not (Test-Path "$root\.env")) {
    Write-Warn ".env not found -- copying from .env.example"
    Copy-Item "$root\.env.example" "$root\.env"
}

if (Test-Path "$root\license.json") {
    $env:RAVEN_LICENSE = Get-Content "$root\license.json" -Raw
    Write-Ok "License loaded from license.json"
} else {
    Write-Warn "license.json not found -- RavenDB will run in Developer mode (3 GB limit, 1 node)."
    Write-Warn "To use your license: save the license JSON to license.json in the repo root."
}

# --- API keys check + optional prompt ---
$envLines = Get-Content "$root\.env"

$keysInfo = @(
    @{ Key = "ANTHROPIC_API_KEY";     Desc = "Anthropic API key (agent won't start without it)";  Required = $true  },
    @{ Key = "KIWI_API_KEY";          Desc = "Kiwi Tequila key (optional, live hidden city)";      Required = $false },
    @{ Key = "AMADEUS_CLIENT_ID";     Desc = "Amadeus client ID (optional, live direct prices)";   Required = $false },
    @{ Key = "AMADEUS_CLIENT_SECRET"; Desc = "Amadeus client secret";                              Required = $false }
)

$anyMissing = $false
foreach ($k in $keysInfo) {
    if (-not (Get-EnvValue $envLines $k.Key)) {
        if (-not $anyMissing) {
            Write-Host ""
            Write-Warn "Some API keys are missing in .env. Enter values now or press Enter to skip."
            Write-Host "  Skipped keys fall back to fixture data. See README for where to get them.`n" -ForegroundColor Yellow
            $anyMissing = $true
        }
        $label = if ($k.Required) { "[required]" } else { "[optional]" }
        $val = Read-Host "  $label $($k.Desc)"
        if ($val) {
            Set-EnvValue "$root\.env" $k.Key $val
            Write-Ok "$($k.Key) saved to .env"
        } elseif ($k.Required) {
            Write-Warn "$($k.Key) not set -- agent will fail to call the LLM"
        }
    }
}
if ($anyMissing) { Write-Host "" }

# --- step 1: RavenDB ---
Write-Step 1 $totalSteps "Starting RavenDB (docker compose)..."
docker compose up -d --wait ravendb
if ($LASTEXITCODE -ne 0) {
    Write-Error "RavenDB failed to start. Check: docker compose logs ravendb"
    exit 1
}
Write-Ok "RavenDB ready"

# --- step 2: seed (optional) ---
if (-not $SkipSeed) {
    Write-Step 2 $totalSteps "Seeding airports and fixture routes..."
    & $python -m scripts.seed_local
    if ($LASTEXITCODE -ne 0) {
        Write-Error "Seeding failed. Check the logs above."
        exit 1
    }
    Write-Ok "Data seeded"
}

# --- worker in a separate window (optional) ---
if ($Worker) {
    Write-Host "`n  Starting price-drop worker in a separate window..." -ForegroundColor Gray
    Start-Process powershell -ArgumentList "-NoExit", "-Command", "& '$python' -m src.worker.run"
    Write-Ok "Worker started (separate window)"
}

# --- last step: agent (foreground) ---
Write-Step $totalSteps $totalSteps "Starting agent at http://localhost:8000  (Ctrl+C to stop)"
Write-Host "  Swagger UI:    http://localhost:8000/docs" -ForegroundColor Gray
Write-Host "  RavenDB Studio: http://localhost:8080" -ForegroundColor Gray
Write-Host ""

& $uvicorn src.agent.app:app --reload --port 8000