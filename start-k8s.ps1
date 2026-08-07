# start-k8s.ps1 -- starts the full stack on a local kind cluster
#
# Usage:
#   .\start-k8s.ps1                  # collect secrets/certs + create cluster + operator + full deploy + port-forwards
#   .\start-k8s.ps1 -SkipBuild       # skip docker build (image already loaded)
#   .\start-k8s.ps1 -SkipOperator    # skip cert-manager/ingress-nginx/operator install (already installed)
#   .\start-k8s.ps1 -DeleteCluster   # delete the kind cluster and exit
#
# Prerequisites: kind, kubectl, helm, openssl are auto-installed via winget if missing.
# Docker Desktop is also auto-installed via winget, but needs one manual step
# (first launch: accept the license, finish WSL2/Hyper-V setup, possibly reboot) --
# the script installs it and asks you to re-run once it's running.
#
# Everything the deploy needs -- OpenAI/Travelpayouts/license values and the
# RavenDB TLS cert chain -- is collected or generated FIRST, before the kind
# cluster even exists. Nothing about the cluster/operator/RavenDB deploy can
# fail partway through for a missing secret or cert.
# Operator project: https://github.com/ravendb/ravendb-operator

param(
    [switch]$SkipBuild,
    [switch]$SkipOperator,
    [switch]$DeleteCluster,
    [string]$ClusterName = "hidden-city"
)

$ErrorActionPreference = "Stop"
$root = $PSScriptRoot
$NS = "hidden-city"
$ImageTag = "hidden-city:latest"
$CertManagerVersion = "v1.16.2"
$IngressNginxUrl = "https://raw.githubusercontent.com/kubernetes/ingress-nginx/main/deploy/static/provider/kind/deploy.yaml"

function Write-Step($n, $total, $msg) {
    Write-Host "`n[$n/$total] $msg" -ForegroundColor Cyan
}
function Write-Ok($msg)   { Write-Host "  OK: $msg" -ForegroundColor Green }
function Write-Warn($msg) { Write-Host "  WARN: $msg" -ForegroundColor Yellow }

# PowerShell 5.1 wraps a native command's stderr lines into terminating
# NativeCommandError objects whenever that stream is redirected (2>&1, 2>$null,
# etc.) and $ErrorActionPreference = "Stop" is in effect -- even when the
# command's own exit code is 0 and the stderr text is purely informational
# (e.g. "No kind clusters found."). Run any such call through this helper,
# which drops $ErrorActionPreference to SilentlyContinue in its own function
# scope only, so the redirect no longer aborts the script. $LASTEXITCODE from
# the wrapped command is still set normally afterwards.
function Invoke-Quiet {
    param([Parameter(Mandatory)][scriptblock]$Command)
    $ErrorActionPreference = "SilentlyContinue"
    & $Command
}

# Look for a value the user already provided somewhere else before asking again:
# 1. an environment variable in this session
# 2. the repo-root .env file (the Local/docker-compose flow's env source)
# 3. an optional plain file, whole contents as the value (e.g. license.json)
function Find-ExistingValue {
    param([string]$Name, [string]$FallbackFile = $null, [string]$Placeholder = $null)

    # A fresh .env copied from .env.example (by the Local flow's start.ps1) carries
    # its literal placeholder value (e.g. "sk-...") -- that must not be reused as
    # if it were a real key, so it's excluded at every source below.
    $fromEnv = [System.Environment]::GetEnvironmentVariable($Name)
    if ($fromEnv -and $fromEnv -ne $Placeholder) { return @{ Value = $fromEnv; Source = "environment variable `$env:$Name" } }

    $dotEnvPath = "$root\.env"
    if (Test-Path $dotEnvPath) {
        $line = Get-Content $dotEnvPath | Where-Object { $_ -match "^$Name=(.+)$" } | Select-Object -First 1
        if ($line -match "^$Name=(.+)$") {
            $val = $Matches[1].Trim()
            if ($val -and $val -ne $Placeholder) { return @{ Value = $val; Source = ".env" } }
        }
    }

    if ($FallbackFile -and (Test-Path "$root\$FallbackFile")) {
        $val = (Get-Content "$root\$FallbackFile" -Raw).Trim()
        if ($val) { return @{ Value = $val; Source = $FallbackFile } }
    }

    return $null
}

# Fill a "KEY: "REPLACE_ME"" line in the secrets YAML, escaping the value so it
# stays valid inside a double-quoted YAML string and isn't misread as a regex
# capture-group reference by -replace.
function Set-SecretPlaceholder {
    param([string]$Content, [string]$KeyName, [string]$RawValue)
    $yamlSafe = ($RawValue -replace '"', '\"') -replace '\$', '$$'
    return $Content -replace "${KeyName}:\s+`"REPLACE_ME`"", "${KeyName}: `"$yamlSafe`""
}

function Refresh-Path {
    # Winget updates the Machine/User PATH env vars, but this already-running
    # process doesn't see that until we re-read them from the registry.
    $machine = [System.Environment]::GetEnvironmentVariable("Path", "Machine")
    $user    = [System.Environment]::GetEnvironmentVariable("Path", "User")
    $env:Path = "$machine;$user"
}

function Ensure-CliTool($cmd, $wingetId, $displayName) {
    if (Get-Command $cmd -ErrorAction SilentlyContinue) { return $true }

    if (-not (Get-Command winget -ErrorAction SilentlyContinue)) {
        Write-Host "  ERROR: '$cmd' not found and winget isn't available to install it." -ForegroundColor Red
        Write-Host "    Install $displayName manually: winget install -e --id $wingetId" -ForegroundColor Gray
        return $false
    }

    Write-Warn "$displayName not found -- installing via winget ($wingetId)..."
    winget install -e --id $wingetId --accept-package-agreements --accept-source-agreements
    Refresh-Path

    if (Get-Command $cmd -ErrorAction SilentlyContinue) {
        Write-Ok "$displayName installed"
        return $true
    }
    Write-Warn "$displayName installed but not visible on PATH in this terminal session yet."
    return $false
}

# FireDaemon's installer places files under a version-suffixed folder name
# (e.g. "FireDaemon OpenSSL 3", "FireDaemon OpenSSL 4") -- there is no plain
# "OpenSSL" folder -- and per the winget package's own tracked issue
# (microsoft/winget-pkgs#130257) it does NOT add that folder to PATH. That
# makes `Get-Command openssl` unreliable in both directions: it can resolve to
# Git for Windows' own openssl.exe (mingw64\bin), whose libcrypto doesn't
# match FireDaemon's legacy.dll and breaks the `-legacy` PKCS12 export in
# Ensure-RavenDbCerts with a DSO_load error; or, right after installing
# FireDaemon via winget, it can still resolve to nothing at all. Search
# FireDaemon's actual install locations directly instead of trusting PATH,
# preferring the highest version if more than one is present.
function Resolve-OpenSslExe {
    $roots = @($env:ProgramFiles, ${env:ProgramFiles(x86)}) | Where-Object { $_ }
    foreach ($root in $roots) {
        $exe = Get-ChildItem -Path $root -Directory -Filter "FireDaemon OpenSSL*" -ErrorAction SilentlyContinue |
            Sort-Object Name -Descending |
            ForEach-Object { Join-Path $_.FullName "bin\openssl.exe" } |
            Where-Object { Test-Path $_ } |
            Select-Object -First 1
        if ($exe) { return $exe }
    }
    $onPath = Get-Command openssl -ErrorAction SilentlyContinue
    if ($onPath) { return $onPath.Source }
    return $null
}

# Reads the active (uncommented) node tags out of k8s/ravendb/values.yaml,
# e.g. @("a") today, @("a","b","c") if b/c get uncommented later. Used for
# the cert SAN list, CoreDNS setup, and picking the port-forward node.
function Get-RavenNodeTags {
    Get-Content "$root\k8s\ravendb\values.yaml" |
        Where-Object { $_ -match '^\s*-\s*tag:\s*(\S+)' -and $_ -notmatch '^\s*#' } |
        ForEach-Object { $Matches[1] }
}

# Generates a local self-signed CA + server + client certificate chain for
# RavenDB's TLS requirement, writing it into k8s/ravendb/certs/. Skipped
# entirely if a chain is already present there -- these files are gitignored
# and meant to be reused across runs (or hand-generated once), never silently
# regenerated on top of an existing chain.
#
# Extensions match what the RavenDB Kubernetes Operator requires (verified
# working previously in this repo): the CA needs
# basicConstraints=CA:TRUE + keyUsage=keyCertSign,cRLSign; server/client certs
# need keyUsage=digitalSignature,keyEncipherment +
# extendedKeyUsage=serverAuth (server) / clientAuth (client), with the server
# cert's SAN list covering every active node's plain and "-tcp" hostname.
# PFX files are packaged with -legacy (SHA1/3DES) since the operator can't
# read openssl 3.x's SHA-256 PKCS12 default ("pkcs12: unknown digest
# algorithm"). If openssl's -legacy flag behaves differently on the version
# that ends up installed, re-verify the operator actually accepts the
# generated .pfx files (kubectl describe ravendbcluster / kubectl logs).
function Ensure-RavenDbCerts {
    param([string]$CertsDir, [string[]]$NodeTags)

    $required = @("ca.crt", "server.pfx", "client.pfx")
    $allPresent = $true
    foreach ($f in $required) {
        if (-not (Test-Path "$CertsDir\$f")) { $allPresent = $false; break }
    }
    if ($allPresent) {
        Write-Ok "RavenDB TLS certs already present in k8s/ravendb/certs -- reusing"
        return
    }

    # Not routed through Ensure-CliTool: that helper's "already installed?"
    # check is Get-Command-based, which is unreliable for this package in
    # both directions -- see Resolve-OpenSslExe above. Resolve directly, only
    # falling back to a winget install if genuinely not found anywhere.
    $openssl = Resolve-OpenSslExe
    if (-not $openssl) {
        if (Get-Command winget -ErrorAction SilentlyContinue) {
            Write-Warn "OpenSSL not found -- installing via winget (FireDaemon.OpenSSL)..."
            winget install -e --id FireDaemon.OpenSSL --accept-package-agreements --accept-source-agreements
            $openssl = Resolve-OpenSslExe
        } else {
            Write-Host "  ERROR: openssl not found and winget isn't available to install it." -ForegroundColor Red
            Write-Host "    Install it manually: winget install -e --id FireDaemon.OpenSSL" -ForegroundColor Gray
        }
    }
    if (-not $openssl) {
        Write-Host "  ERROR: openssl is required to generate RavenDB TLS certs and could not be located" -ForegroundColor Red
        Write-Host "    (checked Program Files\FireDaemon OpenSSL*\bin and PATH)." -ForegroundColor Gray
        exit 1
    }

    # Fail fast with a clear message if the legacy provider (needed for the
    # -legacy PKCS12 export below) can't load, instead of surfacing a cryptic
    # DSO_load stack trace deep inside pkcs12 export. Usually means a second,
    # mismatched openssl install (e.g. Git for Windows') was shadowing
    # FireDaemon's on PATH -- see Resolve-OpenSslExe above.
    $legacyProbe = & $openssl list -providers -legacy 2>&1
    if ($LASTEXITCODE -ne 0 -or ($legacyProbe -join "`n") -notmatch "legacy") {
        Write-Host "  ERROR: openssl's 'legacy' provider failed to load (needed for PKCS12 export)." -ForegroundColor Red
        Write-Host "    Resolved openssl: $openssl" -ForegroundColor Gray
        Write-Host "    Reinstall it with: winget install -e --id FireDaemon.OpenSSL --force" -ForegroundColor Gray
        Write-Host "    $legacyProbe" -ForegroundColor Gray
        exit 1
    }

    New-Item -ItemType Directory -Force -Path $CertsDir | Out-Null
    Write-Host "  Generating self-signed RavenDB TLS chain in k8s/ravendb/certs..." -ForegroundColor Gray

    $sanEntries = ($NodeTags | ForEach-Object { "DNS:$_.hiddencity.local,DNS:$_-tcp.hiddencity.local" }) -join ","

    @"
[req]
distinguished_name = dn
x509_extensions = ext
[dn]
[ext]
basicConstraints = critical, CA:TRUE
keyUsage = critical, keyCertSign, cRLSign
"@ | Set-Content "$CertsDir\ca-ext.cnf" -Encoding utf8

    @"
[req]
distinguished_name = dn
req_extensions = ext
[dn]
[ext]
subjectAltName = $sanEntries
keyUsage = critical, digitalSignature, keyEncipherment
extendedKeyUsage = serverAuth, clientAuth
"@ | Set-Content "$CertsDir\server-san.cnf" -Encoding utf8

    @"
[req]
distinguished_name = dn
req_extensions = ext
[dn]
[ext]
keyUsage = critical, digitalSignature, keyEncipherment
extendedKeyUsage = clientAuth
"@ | Set-Content "$CertsDir\client-ext.cnf" -Encoding utf8

    # CA
    & $openssl genrsa -out "$CertsDir\ca.key" 4096 2>$null
    & $openssl req -x509 -new -nodes -key "$CertsDir\ca.key" -sha256 -days 3650 `
        -out "$CertsDir\ca.crt" -subj "/CN=RavenDB Demo CA" `
        -extensions ext -config "$CertsDir\ca-ext.cnf"

    # Server (CN = first node tag, full node list carried in the SAN instead)
    & $openssl genrsa -out "$CertsDir\server.key" 2048 2>$null
    & $openssl req -new -key "$CertsDir\server.key" -out "$CertsDir\server.csr" `
        -subj "/CN=$($NodeTags[0]).hiddencity.local" -config "$CertsDir\server-san.cnf"
    & $openssl x509 -req -in "$CertsDir\server.csr" -CA "$CertsDir\ca.crt" -CAkey "$CertsDir\ca.key" `
        -CAcreateserial -out "$CertsDir\server.crt" -days 825 -sha256 `
        -extfile "$CertsDir\server-san.cnf" -extensions ext

    # Client
    & $openssl genrsa -out "$CertsDir\client.key" 2048 2>$null
    & $openssl req -new -key "$CertsDir\client.key" -out "$CertsDir\client.csr" `
        -subj "/CN=hidden-city-client" -config "$CertsDir\client-ext.cnf"
    & $openssl x509 -req -in "$CertsDir\client.csr" -CA "$CertsDir\ca.crt" -CAkey "$CertsDir\ca.key" `
        -CAcreateserial -out "$CertsDir\client.crt" -days 825 -sha256 `
        -extfile "$CertsDir\client-ext.cnf" -extensions ext

    # Legacy-encoding PKCS12 -- required by the operator, see function comment above
    & $openssl pkcs12 -export -legacy -out "$CertsDir\server.pfx" `
        -inkey "$CertsDir\server.key" -in "$CertsDir\server.crt" `
        -certfile "$CertsDir\ca.crt" -passout pass:
    & $openssl pkcs12 -export -legacy -out "$CertsDir\client.pfx" `
        -inkey "$CertsDir\client.key" -in "$CertsDir\client.crt" `
        -certfile "$CertsDir\ca.crt" -passout pass:

    if (-not (Test-Path "$CertsDir\server.pfx") -or -not (Test-Path "$CertsDir\client.pfx")) {
        Write-Host "  ERROR: RavenDB cert generation failed -- see openssl output above." -ForegroundColor Red
        exit 1
    }
    Write-Ok "RavenDB TLS chain generated"
}

# --- delete cluster shortcut ---
if ($DeleteCluster) {
    Write-Host "`nDeleting kind cluster '$ClusterName'..." -ForegroundColor Cyan
    kind delete cluster --name $ClusterName
    Write-Ok "Cluster deleted"
    exit 0
}

# --- prerequisites (auto-install missing CLI tools via winget) ---
Write-Host ""
$cliReady = $true
foreach ($t in @(
    @{ Cmd = "kind";    Id = "Kubernetes.kind";    Name = "kind" },
    @{ Cmd = "kubectl"; Id = "Kubernetes.kubectl"; Name = "kubectl" },
    @{ Cmd = "helm";    Id = "Helm.Helm";          Name = "helm" }
)) {
    if (-not (Ensure-CliTool $t.Cmd $t.Id $t.Name)) { $cliReady = $false }
}

# Docker Desktop is a heavier install (WSL2/Hyper-V, admin rights, a GUI first-run
# to accept the license and pick a backend) -- winget can kick it off, but it
# can't be driven unattended past that point, so we install and stop rather than
# pretending the rest of the script can continue.
if (-not (Get-Command docker -ErrorAction SilentlyContinue)) {
    if (Get-Command winget -ErrorAction SilentlyContinue) {
        Write-Warn "docker not found -- installing Docker Desktop via winget..."
        winget install -e --id Docker.DockerDesktop --accept-package-agreements --accept-source-agreements
        Write-Host ""
        Write-Host "  Docker Desktop was installed. It needs one manual step: launch it," -ForegroundColor Yellow
        Write-Host "  accept the license, let it finish WSL2/Hyper-V setup (may prompt for a" -ForegroundColor Yellow
        Write-Host "  reboot), and wait for 'Docker Desktop is running' before continuing." -ForegroundColor Yellow
        Write-Host "  Then re-run: .\start-k8s.ps1" -ForegroundColor Yellow
    } else {
        Write-Host "  ERROR: docker not found and winget isn't available." -ForegroundColor Red
        Write-Host "    Install Docker Desktop manually: https://docs.docker.com/desktop/" -ForegroundColor Gray
    }
    exit 1
}

# Docker CLI present doesn't mean the daemon is running yet.
Invoke-Quiet { docker info *> $null } | Out-Null
if ($LASTEXITCODE -ne 0) {
    Write-Host "  ERROR: Docker is installed but the daemon isn't responding." -ForegroundColor Red
    Write-Host "    Start Docker Desktop, wait for it to say 'Docker Desktop is running', and re-run this script." -ForegroundColor Gray
    exit 1
}

if (-not $cliReady) {
    Write-Host ""
    Write-Warn "One or more tools were just installed but aren't on PATH in this terminal yet."
    Write-Warn "Close and reopen your terminal, then re-run: .\start-k8s.ps1"
    exit 1
}
Write-Ok "kind, kubectl, helm, docker found"

$totalSteps = 10
if ($SkipBuild)    { $totalSteps-- }
if ($SkipOperator) { $totalSteps-- }
$step = 0

# --- secrets + certs preflight (no cluster required yet) ---
# Everything below needs no live cluster: app secrets go straight into
# k8s/secrets.local.yaml, and the RavenDB TLS chain is plain local files.
# Gathering all of it first means the cluster/operator/RavenDB steps that
# follow can't fail partway through for a missing key or cert.
$step++
Write-Step $step $totalSteps "Secrets & RavenDB TLS certs"

$secretsFile = "$root\k8s\secrets.local.yaml"
if (-not (Test-Path $secretsFile)) {
    Copy-Item "$root\k8s\secrets.yaml" $secretsFile
    Write-Warn "Created k8s/secrets.local.yaml from template"
}

$secretsContent = Get-Content $secretsFile -Raw

# For each key, reuse a value already provided somewhere else (env var / repo-root
# .env / license.json) before ever asking interactively. OPENAI_API_KEY is required;
# TRAVELPAYOUTS_TOKEN is optional but still prompted for (Enter to skip), matching
# the Local flow's start.ps1 behavior -- RAVENDB_LICENSE is optional and silent.
$secretKeys = @(
    @{ Name = "OPENAI_API_KEY";       Required = $true;  FallbackFile = $null;             Placeholder = "sk-..."; Prompt = $true },
    @{ Name = "TRAVELPAYOUTS_TOKEN";  Required = $false; FallbackFile = $null;             Placeholder = "..."; Prompt = $true },
    @{ Name = "RAVENDB_LICENSE";      Required = $false; FallbackFile = "license.json";    Placeholder = $null; Prompt = $false }
)

foreach ($k in $secretKeys) {
    if ($secretsContent -notmatch "$($k.Name):\s+`"REPLACE_ME`"") {
        Write-Ok "$($k.Name) already set in k8s/secrets.local.yaml"
        continue
    }

    $found = Find-ExistingValue -Name $k.Name -FallbackFile $k.FallbackFile -Placeholder $k.Placeholder
    if ($found) {
        $secretsContent = Set-SecretPlaceholder $secretsContent $k.Name $found.Value
        Write-Ok "$($k.Name) reused from $($found.Source) -- not asking again"
        continue
    }

    if ($k.Prompt) {
        Write-Host ""
        if ($k.Required) {
            Write-Host "  $($k.Name) is required for the agent to call GPT." -ForegroundColor Yellow
        } else {
            Write-Host "  [optional] $($k.Name) -- press Enter to skip." -ForegroundColor Yellow
        }
        $val = Read-Host "  Enter $($k.Name)"
        if ($val) {
            $secretsContent = Set-SecretPlaceholder $secretsContent $k.Name $val
            Write-Ok "$($k.Name) saved to k8s/secrets.local.yaml"
        } elseif ($k.Required) {
            Write-Warn "$($k.Name) not set -- agent will fail to call GPT"
        }
    }
}

$secretsContent | Set-Content $secretsFile -Encoding utf8

# RavenDB TLS cert chain -- generated locally if not already present (see
# Ensure-RavenDbCerts above for exactly what's required and why).
$ravenCertsDir = "$root\k8s\ravendb\certs"
$ravenNodeTags = Get-RavenNodeTags
Ensure-RavenDbCerts -CertsDir $ravenCertsDir -NodeTags $ravenNodeTags

if (-not (Test-Path "$root\license.json")) {
    Write-Warn "license.json not found at repo root -- the ravendb-license Secret can't be created."
    Write-Warn "Save your RavenDB license JSON to license.json in the repo root and re-run."
}

# --- kind cluster ---
$step++
Write-Step $step $totalSteps "kind cluster '$ClusterName'"

$existing = Invoke-Quiet { kind get clusters 2>$null }
if ($existing -contains $ClusterName) {
    Write-Ok "Cluster already exists -- reusing"
} else {
    Write-Host "  Creating cluster (this takes ~1 min)..." -ForegroundColor Gray
    kind create cluster --name $ClusterName --config "$root\k8s\kind-config.yaml"
    if ($LASTEXITCODE -ne 0) {
        Write-Host "  ERROR: kind create cluster failed -- see output above." -ForegroundColor Red
        exit 1
    }
    Write-Ok "Cluster created"
}
kubectl config use-context "kind-$ClusterName" | Out-Null

# kind-config.yaml disables kindnet (the default CNI) -- it doesn't reliably
# hairpin a pod's own traffic back to itself through its own Service ClusterIP,
# which RavenDB's self-signed-cert startup check needs
# (AssertServerCanContactItselfWhenAuthIsOn). Calico handles this correctly.
#
# Installed via the Tigera Operator, not the raw calico.yaml manifest -- the
# raw manifest's install-cni init container fails with "found no writeable
# directory" / permission denied on Docker Desktop for Windows kind nodes.
# The operator's install path doesn't hit that. kubectl create is used (not
# apply) since the CRDs are large; safe to skip if already installed.
Write-Host "  Installing Calico CNI (via Tigera Operator)..." -ForegroundColor Gray
Invoke-Quiet {
    kubectl create -f https://raw.githubusercontent.com/projectcalico/calico/master/manifests/tigera-operator.yaml 2>$null
} | Out-Null
kubectl -n tigera-operator rollout status deployment/tigera-operator --timeout=90s

# The operator Deployment reporting Ready doesn't mean its CRDs exist yet -- the
# operator registers Installation/APIServer itself, asynchronously, a few seconds
# after its pod passes its readiness probe. "kubectl wait --for=condition=
# Established" only waits for a *condition* on an already-existing object -- if
# the CRD doesn't exist at all yet it fails immediately with NotFound instead of
# waiting for it to appear (verified: this raced and failed even with a bare
# Established wait right after rollout status). So poll for each CRD to exist
# first, then wait for Established.
foreach ($crdName in @("installations.operator.tigera.io", "apiservers.operator.tigera.io")) {
    $crdFound = $false
    for ($i = 1; $i -le 30; $i++) {
        Invoke-Quiet { kubectl get crd $crdName 2>$null } | Out-Null
        if ($LASTEXITCODE -eq 0) { $crdFound = $true; break }
        Start-Sleep 2
    }
    if (-not $crdFound) {
        Write-Host "  ERROR: CRD $crdName never appeared -- Tigera operator install likely failed." -ForegroundColor Red
        exit 1
    }
}
kubectl wait --for=condition=Established crd/installations.operator.tigera.io crd/apiservers.operator.tigera.io --timeout=60s

$calicoInstalled = Invoke-Quiet { kubectl get installation.operator.tigera.io default 2>$null }
if (-not $calicoInstalled) {
    $calicoCr = @"
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
"@
    $calicoCr | kubectl create -f -
}
kubectl wait --for=condition=Ready node --all --timeout=180s | Out-Null
if ($LASTEXITCODE -ne 0) {
    Write-Host "  ERROR: nodes did not become Ready -- Calico CNI install likely failed." -ForegroundColor Red
    exit 1
}
Write-Ok "Calico ready"

# --- namespace + k8s Secret objects (needs the cluster to exist) ---
$step++
Write-Step $step $totalSteps "Applying secrets to the cluster"

kubectl create namespace $NS --dry-run=client -o yaml | kubectl apply -f - | Out-Null
kubectl apply -f $secretsFile | Out-Null
Write-Ok "hidden-city-secrets applied"

# RavenDB cert/license secrets, created (idempotently) from the local files
# gathered in the preflight step above -- no more manual kubectl instructions.
$ravenSecrets = @(
    @{ Name = "ravendb-license";     SourceFile = "$root\license.json";           FromFile = "license.json" },
    @{ Name = "ravendb-cert";        SourceFile = "$ravenCertsDir\server.pfx";    FromFile = "server.pfx" },
    @{ Name = "ravendb-ca-cert";     SourceFile = "$ravenCertsDir\ca.crt";        FromFile = "ca.crt" },
    @{ Name = "ravendb-client-cert"; SourceFile = "$ravenCertsDir\client.pfx";    FromFile = "client.pfx" }
)
foreach ($s in $ravenSecrets) {
    if (-not (Test-Path $s.SourceFile)) {
        Write-Warn "Skipping secret $($s.Name) -- source file missing: $($s.SourceFile)"
        continue
    }
    $fromFileArg = "--from-file=$($s.FromFile)=$($s.SourceFile)"
    $yaml = kubectl create secret generic $s.Name -n $NS $fromFileArg --dry-run=client -o yaml
    $yaml | kubectl apply -f - | Out-Null
}
Write-Ok "RavenDB license/cert secrets applied"

# --- docker build ---
if (-not $SkipBuild) {
    $step++
    Write-Step $step $totalSteps "Building Docker image  ->  $ImageTag"
    docker build -t $ImageTag "$root"
    if ($LASTEXITCODE -ne 0) { Write-Host "  ERROR: docker build failed" -ForegroundColor Red; exit 1 }
    Write-Ok "Image built"

    Write-Host "  Loading image into kind cluster..." -ForegroundColor Gray
    kind load docker-image $ImageTag --name $ClusterName
    Write-Ok "Image loaded into kind"
}

# --- cert-manager + ingress-nginx + operator (all via Helm/kubectl) ---
if (-not $SkipOperator) {
    $step++
    Write-Step $step $totalSteps "Installing cert-manager, ingress-nginx, and the RavenDB Operator"

    Write-Host "  Installing cert-manager ($CertManagerVersion)..." -ForegroundColor Gray
    kubectl apply -f "https://github.com/cert-manager/cert-manager/releases/download/$CertManagerVersion/cert-manager.yaml"
    kubectl wait --for=condition=Available deployment --all -n cert-manager --timeout=120s
    if ($LASTEXITCODE -ne 0) { Write-Host "  ERROR: cert-manager did not become Available in time." -ForegroundColor Red; exit 1 }
    Write-Ok "cert-manager ready"

    Write-Host "  Installing ingress-nginx (kind provider)..." -ForegroundColor Gray
    kubectl apply -f $IngressNginxUrl
    kubectl wait --namespace ingress-nginx `
        --for=condition=ready pod `
        --selector=app.kubernetes.io/component=controller `
        --timeout=120s
    if ($LASTEXITCODE -ne 0) { Write-Host "  ERROR: ingress-nginx controller pod did not become ready in time." -ForegroundColor Red; exit 1 }
    Write-Ok "ingress-nginx ready"

    Write-Host "  Adding RavenDB operator Helm repo..." -ForegroundColor Gray
    helm repo add ravendb-operator https://ravendb.github.io/ravendb-operator/helm | Out-Null
    helm repo update | Out-Null

    Write-Host "  Installing RavenDB Kubernetes Operator..." -ForegroundColor Gray
    helm upgrade --install ravendb-operator ravendb-operator/ravendb-operator `
        -n ravendb-operator-system --create-namespace
    if ($LASTEXITCODE -ne 0) { Write-Host "  ERROR: helm install of ravendb-operator failed." -ForegroundColor Red; exit 1 }
    kubectl rollout status deployment -n ravendb-operator-system -l app.kubernetes.io/name=ravendb-operator --timeout=120s
    if ($LASTEXITCODE -ne 0) { Write-Host "  ERROR: ravendb-operator deployment did not roll out in time." -ForegroundColor Red; exit 1 }
    kubectl wait --for=condition=Established crd/ravendbclusters.ravendb.ravendb.io --timeout=60s
    if ($LASTEXITCODE -ne 0) { Write-Host "  ERROR: ravendbclusters CRD was not Established in time." -ForegroundColor Red; exit 1 }
    Write-Ok "Operator ready"
}

# --- RavenDB cluster (Helm chart, not kustomize) ---
$step++
Write-Step $step $totalSteps "Installing RavenDB cluster  (helm upgrade --install ravendb-cluster ...)"
helm upgrade --install ravendb-cluster ravendb-operator/ravendb-cluster `
    -n $NS --create-namespace -f "$root\k8s\ravendb\values.yaml"
if ($LASTEXITCODE -ne 0) { Write-Host "  ERROR: helm install of ravendb-cluster failed." -ForegroundColor Red; exit 1 }
Write-Ok "RavenDB cluster chart applied"

# --- CoreDNS: make each node's public hostname resolve inside the cluster ---
# There's no real DNS for *.hiddencity.local -- both the bootstrap job and
# RavenDB's own self-signed-cert startup check (AssertServerCanContactItself-
# WhenAuthIsOn) need <tag>.hiddencity.local / <tag>-tcp.hiddencity.local to
# resolve from inside the cluster. Point each at its stable ravendb-<tag>
# Service ClusterIP (not the pod IP -- that changes on every pod restart).
# This only works because Calico (installed above via Tigera Operator, not
# kindnet) correctly hairpins a pod's traffic back to itself through a
# Service; kindnet silently times out on that path instead.
$step++
Write-Step $step $totalSteps "Configuring CoreDNS for RavenDB node hostnames"

$nodeTags = Get-RavenNodeTags

$hostsLines = @()
foreach ($tag in $nodeTags) {
    $svcIp = $null
    for ($i = 1; $i -le 45; $i++) {
        $svcIp = Invoke-Quiet { kubectl get svc "ravendb-$tag" -n $NS -o jsonpath='{.spec.clusterIP}' 2>$null }
        if ($svcIp) { break }
        Start-Sleep 2
    }
    if (-not $svcIp) {
        Write-Warn "Could not find Service ravendb-$tag -- skipping its DNS entry"
        continue
    }
    $hostsLines += "           $svcIp $tag.hiddencity.local $tag-tcp.hiddencity.local"
}

if ($hostsLines.Count -gt 0) {
    $hostsEntries = ($hostsLines -join "`n")
    $corefileYaml = @"
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
$hostsEntries
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
"@
    $corefileYaml | kubectl apply -f - | Out-Null
    kubectl rollout restart deployment coredns -n kube-system | Out-Null
    kubectl rollout status deployment coredns -n kube-system --timeout=60s
    Write-Ok "CoreDNS configured for node(s): $($nodeTags -join ', ')"
} else {
    Write-Warn "No RavenDB node Services found -- CoreDNS not configured, bootstrap will likely fail"
}

# --- deploy app manifests ---
$step++
Write-Step $step $totalSteps "Deploying app manifests"

kubectl apply -f "$root\k8s\namespace.yaml"
kubectl apply -f $secretsFile
kubectl apply -k "$root\k8s\"
Write-Ok "Manifests applied"

# --- wait for RavenDB ---
$step++
Write-Step $step $totalSteps "Waiting for RavenDB cluster (60-120s)"
Write-Host "  Operator is: creating PVCs -> starting pods -> forming Raft quorum -> issuing TLS certs" -ForegroundColor Gray
Write-Host ""

$ravenReady = $false
for ($i = 1; $i -le 40; $i++) {
    # -o json + ConvertFrom-Json, not -o jsonpath: PowerShell's native-argument
    # quoting strips the embedded double quotes a jsonpath filter needs
    # (@.type=="Ready") before kubectl.exe ever sees them (verified -- the
    # jsonpath form always returned empty/exit 1 even once the cluster was
    # genuinely Ready), which silently kept this check permanently "not ready".
    $json = Invoke-Quiet { kubectl get ravendbcluster ravendb-cluster -n $NS -o json 2>$null } | Out-String
    if ($json) {
        try {
            $readyCond = ($json | ConvertFrom-Json).status.conditions | Where-Object { $_.type -eq "Ready" }
            if ($readyCond -and $readyCond.status -eq "True") { $ravenReady = $true; break }
        } catch {}
    }
    Write-Host ("  [{0,2}/40] Not ready yet... ({1})" -f $i, (Get-Date -Format "HH:mm:ss")) -ForegroundColor Gray
    Start-Sleep 5
}

if ($ravenReady) {
    Write-Ok "RavenDB cluster Ready"
} else {
    Write-Warn "RavenDB did not reach Ready in time. Check:"
    Write-Warn "  kubectl describe ravendbcluster ravendb-cluster -n $NS"
    Write-Warn "  kubectl get pods -n $NS"
}

$step++
Write-Step $step $totalSteps "Waiting for agent deployment"
kubectl rollout status deployment/agent -n $NS --timeout=120s
if ($LASTEXITCODE -ne 0) { Write-Host "  ERROR: agent deployment did not roll out in time." -ForegroundColor Red; exit 1 }
Write-Ok "Agent deployment ready"

# --- port-forwards ---
# The chart creates one Service per node tag (ravendb-<tag>, e.g. ravendb-a),
# not a single "ravendb-cluster-svc" -- pick the first configured tag. RavenDB
# only listens on HTTPS (443), even in mode: None (self-signed, not plaintext).
# Local port 8081 (not 8080) deliberately avoids clashing with Local mode's
# docker-compose RavenDB, which also binds host port 8080 -- run
# `.\start.ps1 -Mode Local` and `.\start.ps1 -Mode K8s` side by side without
# either stealing the other's port.
Write-Host "`n  Starting port-forwards..." -ForegroundColor Gray

$firstNodeTag = (Get-RavenNodeTags | Select-Object -First 1)

$pfAgent = Start-Process kubectl `
    -ArgumentList @("port-forward", "svc/agent-svc", "8000:80", "-n", $NS) `
    -PassThru -WindowStyle Hidden

$pfRaven = Start-Process kubectl `
    -ArgumentList @("port-forward", "svc/ravendb-$firstNodeTag", "8081:443", "-n", $NS) `
    -PassThru -WindowStyle Hidden

# --- done ---
Write-Host ""
Write-Host "  =============================================" -ForegroundColor Green
Write-Ok "Agent:          http://localhost:8000"
Write-Ok "Swagger UI:     http://localhost:8000/docs"
Write-Ok "RavenDB Studio: https://localhost:8081  (self-signed cert -- browser will warn, click through)"
Write-Host "  =============================================" -ForegroundColor Green
Write-Host ""
Write-Host "  Press Ctrl+C to stop port-forwards." -ForegroundColor Gray
Write-Host "  To delete the cluster: .\start-k8s.ps1 -DeleteCluster" -ForegroundColor Gray
Write-Host ""

try {
    while ($true) { Start-Sleep 5 }
} finally {
    Write-Host "`n  Stopping port-forwards..." -ForegroundColor Gray
    if ($pfAgent  -and -not $pfAgent.HasExited)  { $pfAgent.Kill()  }
    if ($pfRaven  -and -not $pfRaven.HasExited)  { $pfRaven.Kill()  }
    Write-Host "  Done. Cluster '$ClusterName' is still running." -ForegroundColor Gray
    Write-Host "  To delete it: .\start-k8s.ps1 -DeleteCluster" -ForegroundColor Gray
}
