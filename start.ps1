# start.ps1 -- single entry point: pick Local (docker-compose) or Kubernetes, then run it
#
# Usage:
#   .\start.ps1                    # prompts: [1] Local  [2] Kubernetes
#   .\start.ps1 -Mode Local        # skip the prompt, run the local docker-compose stack
#   .\start.ps1 -Mode K8s          # skip the prompt, run the kind/operator stack (start-k8s.ps1)
#   .\start.ps1 -Worker            # (Local) also start the price-drop worker in a separate window
#   .\start.ps1 -SkipSeed          # (Local) skip seeding when the database is already populated
#   .\start.ps1 -Mode K8s -SkipBuild -SkipOperator   # (K8s) forwarded to start-k8s.ps1
#   .\start.ps1 -DeleteCluster      # (K8s) delete the kind cluster and exit
#
# Requires PowerShell 7+ (pwsh.exe). Windows' built-in powershell.exe is 5.1,
# whose native-command stderr handling differs enough (see Invoke-Quiet below)
# that this script's semantics don't hold there. Rather than a hard `#Requires
# -Version 7.0` (which aborts under 5.1 before a single line of this script --
# including a self-relaunch -- ever runs), the version check below is the
# first thing this script actually executes, so plain `.\start.ps1` from a
# stock Windows PowerShell 5.1 prompt can detect that and re-exec itself
# under pwsh.exe automatically instead of just failing.

param(
    [ValidateSet("Local", "K8s", "")]
    [string]$Mode = "",
    [switch]$Worker,        # (Local) start the subscription worker in a separate window
    [switch]$SkipSeed,      # (Local) skip seed_local (when the database is already seeded)
    [switch]$SkipBuild,     # (K8s) skip docker build, forwarded to start-k8s.ps1
    [switch]$SkipOperator,  # (K8s) skip operator install, forwarded to start-k8s.ps1
    [switch]$DeleteCluster, # (K8s) delete the kind cluster and exit, forwarded to start-k8s.ps1
    [string]$ClusterName = "hidden-city"
)

if ($PSVersionTable.PSVersion.Major -lt 7) {
    $pwsh = Get-Command pwsh -ErrorAction SilentlyContinue
    if (-not $pwsh) {
        Write-Host "This script requires PowerShell 7+. Install it with:" -ForegroundColor Red
        Write-Host "  winget install Microsoft.PowerShell" -ForegroundColor Yellow
        Write-Host "then re-run .\start.ps1 (or run it via `pwsh -File .\start.ps1`)." -ForegroundColor Yellow
        exit 1
    }
    $relaunchArgs = @('-NoLogo', '-NoProfile', '-File', $MyInvocation.MyCommand.Path)
    foreach ($key in $PSBoundParameters.Keys) {
        $val = $PSBoundParameters[$key]
        if ($val -is [switch]) {
            if ($val.IsPresent) { $relaunchArgs += "-$key" }
        } else {
            $relaunchArgs += "-$key"
            $relaunchArgs += "$val"
        }
    }
    & $pwsh.Source @relaunchArgs
    exit $LASTEXITCODE
}

$ErrorActionPreference = "Stop"
$root = $PSScriptRoot

Write-Host ""
Write-Host "  Make sure Docker Desktop is running before continuing (both Local's" -ForegroundColor Yellow
Write-Host "  docker-compose and Kubernetes mode's kind cluster run as Docker containers)." -ForegroundColor Yellow

# --- k8s cluster teardown shortcut (no mode picker needed) ---
if ($DeleteCluster) {
    & "$root\start-k8s.ps1" -DeleteCluster -ClusterName $ClusterName
    exit $LASTEXITCODE
}

# --- pick Local vs Kubernetes ---
if (-not $Mode) {
    Write-Host ""
    Write-Host "  How do you want to run this demo?" -ForegroundColor Cyan
    Write-Host "    [1] Local       - docker-compose, fastest to start" -ForegroundColor Gray
    Write-Host "    [2] Kubernetes  - kind cluster + RavenDB Operator, full k8s demo" -ForegroundColor Gray
    Write-Host ""
    $choice = Read-Host "  Enter 1 or 2 (default: 1)"
    $Mode = if ($choice -eq "2") { "K8s" } else { "Local" }
}

if ($Mode -eq "K8s") {
    Write-Host "`n  Kubernetes mode selected -- handing off to start-k8s.ps1`n" -ForegroundColor Cyan
    & "$root\start-k8s.ps1" -SkipBuild:$SkipBuild -SkipOperator:$SkipOperator -ClusterName $ClusterName
    exit $LASTEXITCODE
}

Write-Host "`n  Local mode selected -- docker-compose stack`n" -ForegroundColor Cyan

function Write-Step($n, $total, $msg) {
    Write-Host "`n[$n/$total] $msg" -ForegroundColor Cyan
}

function Write-Ok($msg) {
    Write-Host "  OK: $msg" -ForegroundColor Green
}

function Write-Warn($msg) {
    Write-Host "  WARN: $msg" -ForegroundColor Yellow
}

# PowerShell 5.1 wraps a native command's stderr lines into terminating
# NativeCommandError objects whenever that stream is redirected (2>&1, 2>$null,
# etc.) and $ErrorActionPreference = "Stop" is in effect -- even when the
# command's own exit code is 0 and the stderr text is purely informational.
# `uv --version` below is exactly this shape. Route any such call through this
# helper, which drops $ErrorActionPreference to SilentlyContinue in its own
# function scope only, so the redirect no longer aborts the script.
# (Mirrors Invoke-Quiet in start-k8s.ps1 -- see that script for more detail.)
function Invoke-Quiet {
    param([Parameter(Mandatory)][scriptblock]$Command)
    $ErrorActionPreference = "SilentlyContinue"
    & $Command
}

# UV_VERSION from versions.env (repo root, single source of truth -- see its
# header comment). Falls back to $null (unpinned) only if the file is missing,
# so this script keeps working during initial checkout/bootstrap edge cases.
$UvVersion = (Get-Content "$root\versions.env" -ErrorAction SilentlyContinue |
    Where-Object { $_ -match '^\s*UV_VERSION\s*=\s*(\S+)' } |
    ForEach-Object { $Matches[1] } | Select-Object -First 1)

function Find-Uv {
    $found = Get-Command uv -ErrorAction SilentlyContinue
    if ($found) { return $found.Source }
    $candidates = @(
        "$env:LOCALAPPDATA\uv\uv.exe",
        "$env:USERPROFILE\.local\bin\uv.exe",
        "$env:USERPROFILE\.cargo\bin\uv.exe",
        "$env:LOCALAPPDATA\uv-pinned\$UvVersion\uv.exe"
    )
    foreach ($c in $candidates) {
        if (Test-Path $c) { return $c }
    }
    return $null
}

# Existence alone doesn't mean it's the version this repo is pinned to -- the
# same "existence, not capability" bug class as Resolve-OpenSslExe's PATH
# fallback and the license.json staleness issue in start-k8s.ps1. A
# globally-installed uv (e.g. from astral.sh's own install script, landing in
# .local/bin, entirely outside this project's control) used to be accepted by
# the caller the moment Find-Uv returned anything, without ever comparing its
# version to UV_VERSION -- confirmed hitting this for real: a machine with an
# older uv already on PATH silently diverged from the pin instead of
# triggering Install-Uv's pinning logic below.
function Resolve-PinnedUv {
    $uv = Find-Uv
    if ($uv -and $UvVersion) {
        $actual = Invoke-Quiet { & $uv --version 2>&1 }
        if ($actual -notmatch [regex]::Escape($UvVersion)) {
            Write-Warn "Found uv at $uv ($actual) but versions.env pins UV_VERSION=$UvVersion -- installing the pinned version instead..."
            $uv = $null
        }
    }
    return $uv
}

function Install-Uv {
    Write-Warn "uv not found or not at the pinned version -- installing $UvVersion automatically..."
    $winget = Get-Command winget -ErrorAction SilentlyContinue
    if ($winget -and $UvVersion) {
        winget install --id astral-sh.uv -e --version $UvVersion --silent --accept-package-agreements --accept-source-agreements
    }
    $uv = Find-Uv
    if ($uv -and $UvVersion) {
        $actual = Invoke-Quiet { & $uv --version 2>&1 }
        if ($actual -notmatch [regex]::Escape($UvVersion)) {
            Write-Warn "winget installed uv but not at the pinned version $UvVersion (got: $actual)"
            $uv = $null
        }
    }
    if (-not $uv) {
        # winget unavailable, or it didn't produce the exact pinned version --
        # download that release's own asset directly rather than falling back
        # to the "always latest" install.ps1 (the same unpinned-installer
        # pattern this whole effort exists to remove).
        Write-Host "  Downloading uv $UvVersion directly from GitHub releases..." -ForegroundColor Gray
        $dest = "$env:LOCALAPPDATA\uv-pinned\$UvVersion"
        New-Item -ItemType Directory -Force -Path $dest | Out-Null
        $zipPath = "$dest\uv.zip"
        Invoke-WebRequest -Uri "https://github.com/astral-sh/uv/releases/download/$UvVersion/uv-x86_64-pc-windows-msvc.zip" -OutFile $zipPath
        Expand-Archive -Path $zipPath -DestinationPath $dest -Force
        Remove-Item $zipPath -Force
        $uv = Find-Uv
    }
    if (-not $uv) {
        Write-Error "uv installation finished but the executable could not be located. Reopen this terminal and try again."
        exit 1
    }
    Write-Ok "uv installed ($(Invoke-Quiet { & $uv --version 2>&1 }))"
    return $uv
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
    $uv = Resolve-PinnedUv
    if (-not $uv) {
        $uv = Install-Uv
    }
    Write-Host "`n  Creating venv and installing dependencies from uv.lock..." -ForegroundColor Gray
    Push-Location $root
    & $uv sync --frozen --extra dev --python ">=3.11,<3.14"
    $installExit = $LASTEXITCODE
    Pop-Location
    if ($installExit -ne 0) {
        Write-Error "Failed to create venv / install dependencies. Install Python 3.11, 3.12, or 3.13 and try again."
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
    $env:RAVENDB_LICENSE = Get-Content "$root\license.json" -Raw
    Write-Ok "License loaded from license.json"
} else {
    Write-Warn "license.json not found -- RavenDB will run in Developer mode (3 GB limit, 1 node)."
    Write-Warn "To use your license: save the license JSON to license.json in the repo root."
}

# --- API keys check + optional prompt ---
$envLines = Get-Content "$root\.env"

$keysInfo = @(
    @{ Key = "OPENAI_API_KEY";       Desc = "OpenAI API key (agent won't start without it)";                       Required = $true;  Placeholder = "sk-..." },
    @{ Key = "TRAVELPAYOUTS_TOKEN";  Desc = "Travelpayouts / Aviasales Data API token (optional, bulk scraper)";   Required = $false; Placeholder = "..." }
)

$anyMissing = $false
foreach ($k in $keysInfo) {
    $currentValue = Get-EnvValue $envLines $k.Key
    # A fresh .env copied from .env.example carries its literal placeholder
    # value (e.g. "sk-...") -- that must be treated as unset, not as a real key.
    if (-not $currentValue -or $currentValue -eq $k.Placeholder) {
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
Write-Host "  RavenDB Studio: http://localhost:8080" -ForegroundColor Gray

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
$workerProcess = $null
if ($Worker) {
    Write-Host "`n  Starting price-drop worker in a separate window..." -ForegroundColor Gray
    $workerProcess = Start-Process powershell -ArgumentList "-NoExit", "-Command", "& '$python' -m src.worker.run" -PassThru
    Write-Ok "Worker started (separate window)"
}

# --- last step: agent (foreground) ---
Write-Step $totalSteps $totalSteps "Starting agent at http://localhost:8001  (Ctrl+C to stop)"
Write-Host "  Swagger UI:    http://localhost:8001/docs" -ForegroundColor Gray
Write-Host "  RavenDB Studio: http://localhost:8080" -ForegroundColor Gray
Write-Host ""

# Ctrl+C (or any exit) runs the finally block -- without this, docker compose's
# RavenDB container keeps holding port 8080 (and 38888) after the "demo" looks
# stopped, which is exactly what silently conflicted with the Kubernetes mode's
# own port-forward on 8080 earlier.
try {
    & $uvicorn src.agent.app:app --reload --port 8001
} finally {
    Write-Host "`n  Stopping RavenDB (docker compose down) to release its ports..." -ForegroundColor Gray
    docker compose down
    if ($workerProcess -and -not $workerProcess.HasExited) {
        $workerProcess.Kill()
    }
    Write-Ok "Stopped -- ports 8080/38888 released"
}