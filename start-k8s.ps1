# start-k8s.ps1 -- starts the full stack on a local kind cluster
#
# Usage:
#   .\start-k8s.ps1                  # create cluster + operator + full deploy + port-forwards
#   .\start-k8s.ps1 -SkipBuild       # skip docker build (image already loaded)
#   .\start-k8s.ps1 -SkipOperator    # skip cert-manager/ingress-nginx/operator install (already installed)
#   .\start-k8s.ps1 -DeleteCluster   # delete the kind cluster and exit
#
# Prerequisites: kind, kubectl, helm are auto-installed via winget if missing.
# Docker Desktop is also auto-installed via winget, but needs one manual step
# (first launch: accept the license, finish WSL2/Hyper-V setup, possibly reboot) --
# the script installs it and asks you to re-run once it's running.
#
# TLS certs (license/cert-manager/ingress-nginx/operator are all automated below,
# but the RavenDB self-signed cert material is NOT auto-generated -- see step 2).
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
    param([string]$Name, [string]$FallbackFile = $null)

    $fromEnv = [System.Environment]::GetEnvironmentVariable($Name)
    if ($fromEnv) { return @{ Value = $fromEnv; Source = "environment variable `$env:$Name" } }

    $dotEnvPath = "$root\.env"
    if (Test-Path $dotEnvPath) {
        $line = Get-Content $dotEnvPath | Where-Object { $_ -match "^$Name=(.+)$" } | Select-Object -First 1
        if ($line -match "^$Name=(.+)$") {
            $val = $Matches[1].Trim()
            if ($val) { return @{ Value = $val; Source = ".env" } }
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

$totalSteps = 9
if ($SkipBuild)    { $totalSteps-- }
if ($SkipOperator) { $totalSteps-- }
$step = 0

# --- kind cluster ---
$step++
Write-Step $step $totalSteps "kind cluster '$ClusterName'"

$existing = Invoke-Quiet { kind get clusters 2>$null }
if ($existing -contains $ClusterName) {
    Write-Ok "Cluster already exists -- reusing"
} else {
    Write-Host "  Creating cluster (this takes ~1 min)..." -ForegroundColor Gray
    kind create cluster --name $ClusterName --config "$root\k8s\kind-config.yaml"
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
Write-Ok "Calico ready"

# --- secrets ---
$step++
Write-Step $step $totalSteps "Secrets"

kubectl create namespace $NS --dry-run=client -o yaml | kubectl apply -f - | Out-Null

$secretsFile = "$root\k8s\secrets.local.yaml"
if (-not (Test-Path $secretsFile)) {
    Copy-Item "$root\k8s\secrets.yaml" $secretsFile
    Write-Warn "Created k8s/secrets.local.yaml from template"
}

$secretsContent = Get-Content $secretsFile -Raw

# For each key, reuse a value already provided somewhere else (env var / repo-root
# .env / license.json) before ever asking interactively. Only OPENAI_API_KEY is
# required -- the others are left as "REPLACE_ME" if nothing is found, same as before.
$secretKeys = @(
    @{ Name = "OPENAI_API_KEY";       Required = $true;  FallbackFile = $null },
    @{ Name = "TRAVELPAYOUTS_TOKEN";  Required = $false; FallbackFile = $null },
    @{ Name = "RAVENDB_LICENSE";      Required = $false; FallbackFile = "license.json" }
)

foreach ($k in $secretKeys) {
    if ($secretsContent -notmatch "$($k.Name):\s+`"REPLACE_ME`"") {
        Write-Ok "$($k.Name) already set in k8s/secrets.local.yaml"
        continue
    }

    $found = Find-ExistingValue -Name $k.Name -FallbackFile $k.FallbackFile
    if ($found) {
        $secretsContent = Set-SecretPlaceholder $secretsContent $k.Name $found.Value
        Write-Ok "$($k.Name) reused from $($found.Source) -- not asking again"
        continue
    }

    if ($k.Required) {
        Write-Host ""
        Write-Host "  $($k.Name) is required for the agent to call GPT." -ForegroundColor Yellow
        $val = Read-Host "  Enter $($k.Name)"
        if ($val) {
            $secretsContent = Set-SecretPlaceholder $secretsContent $k.Name $val
            Write-Ok "$($k.Name) saved to k8s/secrets.local.yaml"
        } else {
            Write-Warn "$($k.Name) not set -- agent will fail to call GPT"
        }
    }
}

$secretsContent | Set-Content $secretsFile -Encoding utf8

# RavenDB cert/license secrets are NOT auto-generated here. openssl-generated
# certs do work (verified) -- server/client certs need
# keyUsage=digitalSignature,keyEncipherment + extendedKeyUsage=serverAuth
# (client: clientAuth), the CA needs
# basicConstraints=CA:TRUE + keyUsage=keyCertSign,cRLSign, and PFX files must
# use legacy SHA1/3DES encoding (openssl 3.x's SHA-256 default PKCS12 isn't
# readable by the operator: "pkcs12: unknown digest algorithm"). That's still
# manual because it's security-sensitive material this script shouldn't
# silently fabricate -- check for the four generic secrets the ravendb-cluster
# chart expects (k8s/ravendb/values.yaml) and stop with exact instructions if
# any are missing.
$requiredRavenSecrets = @(
    @{ Name = "ravendb-license";     Hint = "kubectl create secret generic ravendb-license -n $NS --from-file=license.json=<path-to-license.json>" },
    @{ Name = "ravendb-cert";        Hint = "kubectl create secret generic ravendb-cert -n $NS --from-file=server.pfx=<path-to-server.pfx>" },
    @{ Name = "ravendb-ca-cert";     Hint = "kubectl create secret generic ravendb-ca-cert -n $NS --from-file=ca.crt=<path-to-ca.crt>" },
    @{ Name = "ravendb-client-cert"; Hint = "kubectl create secret generic ravendb-client-cert -n $NS --from-file=client.pfx=<path-to-client.pfx>" }
)
$missingRaven = @()
foreach ($s in $requiredRavenSecrets) {
    Invoke-Quiet { kubectl get secret $s.Name -n $NS 2>$null } | Out-Null
    if ($LASTEXITCODE -ne 0) { $missingRaven += $s }
}
if ($missingRaven.Count -gt 0) {
    Write-Host ""
    Write-Warn "Missing RavenDB cert/license secrets in namespace '$NS':"
    foreach ($s in $missingRaven) {
        Write-Host "    - $($s.Name)" -ForegroundColor Yellow
        Write-Host "        $($s.Hint)" -ForegroundColor Gray
    }
    Write-Host ""
    Write-Host "  Generate these with openssl (CA + server cert with all node SANs +" -ForegroundColor Yellow
    Write-Host "  client cert -- see the comment above this check for the exact extensions" -ForegroundColor Yellow
    Write-Host "  and legacy PKCS12 flags needed), or via RavenDB's Setup Wizard, then re-run" -ForegroundColor Yellow
    Write-Host "  this script with -SkipBuild -SkipOperator to skip straight to cluster deploy." -ForegroundColor Yellow
    Write-Host "  See: https://github.com/ravendb/ravendb-operator/tree/main/examples/tls/selfsigned" -ForegroundColor Gray
    exit 1
}
Write-Ok "RavenDB license/cert secrets present"

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
    Write-Ok "cert-manager ready"

    Write-Host "  Installing ingress-nginx (kind provider)..." -ForegroundColor Gray
    kubectl apply -f $IngressNginxUrl
    kubectl wait --namespace ingress-nginx `
        --for=condition=ready pod `
        --selector=app.kubernetes.io/component=controller `
        --timeout=120s
    Write-Ok "ingress-nginx ready"

    Write-Host "  Adding RavenDB operator Helm repo..." -ForegroundColor Gray
    helm repo add ravendb-operator https://ravendb.github.io/ravendb-operator/helm | Out-Null
    helm repo update | Out-Null

    Write-Host "  Installing RavenDB Kubernetes Operator..." -ForegroundColor Gray
    helm upgrade --install ravendb-operator ravendb-operator/ravendb-operator `
        -n ravendb-operator-system --create-namespace
    kubectl rollout status deployment -n ravendb-operator-system -l app.kubernetes.io/name=ravendb-operator --timeout=120s
    kubectl wait --for=condition=Established crd/ravendbclusters.ravendb.ravendb.io --timeout=60s
    Write-Ok "Operator ready"
}

# --- RavenDB cluster (Helm chart, not kustomize) ---
$step++
Write-Step $step $totalSteps "Installing RavenDB cluster  (helm upgrade --install ravendb-cluster ...)"
helm upgrade --install ravendb-cluster ravendb-operator/ravendb-cluster `
    -n $NS --create-namespace -f "$root\k8s\ravendb\values.yaml"
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

$nodeTags = Get-Content "$root\k8s\ravendb\values.yaml" |
    Where-Object { $_ -match '^\s*-\s*tag:\s*(\S+)' -and $_ -notmatch '^\s*#' } |
    ForEach-Object { $Matches[1] }

$hostsLines = @()
foreach ($tag in $nodeTags) {
    $svcIp = $null
    for ($i = 1; $i -le 15; $i++) {
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
    $status = Invoke-Quiet {
        kubectl get ravendbcluster ravendb-cluster -n $NS `
            -o jsonpath='{.status.conditions[?(@.type=="Ready")].status}' 2>$null
    }
    if ($status -eq "True") { $ravenReady = $true; break }
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

$firstNodeTag = (Get-Content "$root\k8s\ravendb\values.yaml" |
    Where-Object { $_ -match '^\s*-\s*tag:\s*(\S+)' -and $_ -notmatch '^\s*#' } |
    ForEach-Object { $Matches[1] } | Select-Object -First 1)

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
