# start-k8s.ps1 -- starts the full stack on a local kind cluster
#
# Usage:
#   .\start-k8s.ps1                  # create cluster + operator + full deploy + port-forwards
#   .\start-k8s.ps1 -SkipBuild       # skip docker build (image already loaded)
#   .\start-k8s.ps1 -SkipOperator    # skip operator install (already installed)
#   .\start-k8s.ps1 -DeleteCluster   # delete the kind cluster and exit
#
# Prerequisites:
#   kind:    winget install Kubernetes.kind
#   kubectl: winget install Kubernetes.kubectl
#   docker:  Docker Desktop running

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
$OperatorUrl = "https://github.com/ravendb/ravendb-k8s-operator/releases/latest/download/ravendb-operator.yaml"

function Write-Step($n, $total, $msg) {
    Write-Host "`n[$n/$total] $msg" -ForegroundColor Cyan
}
function Write-Ok($msg)   { Write-Host "  OK: $msg" -ForegroundColor Green }
function Write-Warn($msg) { Write-Host "  WARN: $msg" -ForegroundColor Yellow }

# --- delete cluster shortcut ---
if ($DeleteCluster) {
    Write-Host "`nDeleting kind cluster '$ClusterName'..." -ForegroundColor Cyan
    kind delete cluster --name $ClusterName
    Write-Ok "Cluster deleted"
    exit 0
}

# --- prerequisites ---
Write-Host ""
foreach ($cmd in @("kind", "kubectl", "docker")) {
    if (-not (Get-Command $cmd -ErrorAction SilentlyContinue)) {
        Write-Host "  ERROR: '$cmd' not found. Install it and reopen this terminal:" -ForegroundColor Red
        Write-Host "    kind:    winget install Kubernetes.kind" -ForegroundColor Gray
        Write-Host "    kubectl: winget install Kubernetes.kubectl" -ForegroundColor Gray
        Write-Host "    docker:  Docker Desktop (https://docs.docker.com/desktop/)" -ForegroundColor Gray
        exit 1
    }
}
Write-Ok "kind, kubectl, docker found"

$totalSteps = 6
if ($SkipBuild)    { $totalSteps-- }
if ($SkipOperator) { $totalSteps-- }
$step = 0

# --- kind cluster ---
$step++
Write-Step $step $totalSteps "kind cluster '$ClusterName'"

$existing = kind get clusters 2>&1
if ($existing -contains $ClusterName) {
    Write-Ok "Cluster already exists -- reusing"
} else {
    Write-Host "  Creating cluster (this takes ~1 min)..." -ForegroundColor Gray
    kind create cluster --name $ClusterName --config "$root\k8s\kind-config.yaml"
    Write-Ok "Cluster created"
}
kubectl config use-context "kind-$ClusterName" | Out-Null

# --- secrets ---
$step++
Write-Step $step $totalSteps "Secrets"

$secretsFile = "$root\k8s\secrets.local.yaml"
if (-not (Test-Path $secretsFile)) {
    Copy-Item "$root\k8s\secrets.yaml" $secretsFile
    Write-Warn "Created k8s/secrets.local.yaml from template"
}

$secretsContent = Get-Content $secretsFile -Raw
if ($secretsContent -match 'OPENAI_API_KEY:\s+"REPLACE_ME"') {
    Write-Host ""
    Write-Host "  OPENAI_API_KEY is required for the agent to call GPT." -ForegroundColor Yellow
    $key = Read-Host "  Enter your OpenAI API key"
    if ($key) {
        $secretsContent = $secretsContent -replace 'OPENAI_API_KEY:\s+"REPLACE_ME"', "OPENAI_API_KEY: `"$key`""
        $secretsContent | Set-Content $secretsFile -Encoding utf8
        Write-Ok "OPENAI_API_KEY saved to k8s/secrets.local.yaml"
    } else {
        Write-Warn "OPENAI_API_KEY not set -- agent will fail to call GPT"
    }
} else {
    Write-Ok "OPENAI_API_KEY already set"
}

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

# --- operator ---
if (-not $SkipOperator) {
    $step++
    Write-Step $step $totalSteps "Installing RavenDB Kubernetes Operator"

    kubectl apply -f $OperatorUrl
    kubectl rollout status deployment/ravendb-operator-controller-manager `
        -n ravendb-operator-system --timeout=120s
    kubectl wait --for=condition=Established `
        crd/ravendbclusters.ravendb.com --timeout=60s
    Write-Ok "Operator ready"
}

# --- deploy ---
$step++
Write-Step $step $totalSteps "Deploying manifests"

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
    $status = kubectl get ravendbcluster ravendb-cluster -n $NS `
        -o jsonpath='{.status.conditions[?(@.type=="Ready")].status}' 2>$null
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

kubectl rollout status deployment/agent -n $NS --timeout=120s
Write-Ok "Agent deployment ready"

# --- port-forwards ---
Write-Host "`n  Starting port-forwards..." -ForegroundColor Gray

$pfAgent = Start-Process kubectl `
    -ArgumentList @("port-forward", "svc/agent-svc", "8000:80", "-n", $NS) `
    -PassThru -WindowStyle Hidden

$pfRaven = Start-Process kubectl `
    -ArgumentList @("port-forward", "svc/ravendb-cluster-svc", "8080:8080", "-n", $NS) `
    -PassThru -WindowStyle Hidden

# --- done ---
Write-Host ""
Write-Host "  =============================================" -ForegroundColor Green
Write-Ok "Agent:          http://localhost:8000"
Write-Ok "Swagger UI:     http://localhost:8000/docs"
Write-Ok "RavenDB Studio: http://localhost:8080"
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
