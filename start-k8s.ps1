# start-k8s.ps1 -- starts the full stack on a local kind cluster
#
# Usage:
#   .\start-k8s.ps1                  # collect secrets/certs + create cluster + operator + full deploy + port-forwards
#   .\start-k8s.ps1 -SkipBuild       # skip docker build (image already loaded)
#   .\start-k8s.ps1 -SkipOperator    # skip cert-manager/ingress-nginx/operator install (already installed)
#   .\start-k8s.ps1 -DeleteCluster   # delete the kind cluster and exit
#
# Prerequisites: kind, kubectl, helm are fetched deterministically into .tools/
# (see Get-ToolBinary) -- no admin rights, no PATH mutation, exact versions pinned
# in versions.env. openssl is still installed via winget (FireDaemon.OpenSSL).
# Docker Desktop is also auto-installed via winget, but needs one manual step
# (first launch: accept the license, finish WSL2/Hyper-V setup, possibly reboot) --
# the script installs it and asks you to re-run once it's running.
#
# Every tool/image/manifest version this script touches is pinned in
# versions.env at the repo root -- see that file's header comment. Never inline
# a version or a moving branch ref (master/main/latest) directly in this script.
#
# Everything the deploy needs -- OpenAI/Travelpayouts/license values and the
# RavenDB TLS cert chain -- is collected or generated FIRST, before the kind
# cluster even exists. Nothing about the cluster/operator/RavenDB deploy can
# fail partway through for a missing secret or cert.
# Operator project: https://github.com/ravendb/ravendb-operator
#
# Requires PowerShell 7+ (pwsh.exe). Windows' built-in powershell.exe is 5.1,
# whose native-command stderr handling differs enough (see Invoke-Quiet below)
# that this script's semantics don't hold there -- run it via `pwsh.exe .\start-k8s.ps1`.
#Requires -Version 7.0

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

# Parses versions.env's plain `KEY=value` lines (blank/`#`-comment lines and
# trailing ` # inline comments` skipped) into a hashtable. The single source of
# truth for every pinned version below -- see versions.env's header comment.
function Read-VersionsEnv([string]$Path) {
    $table = @{}
    foreach ($line in Get-Content $Path) {
        if ($line -notmatch '^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)$') { continue }
        $value = ($Matches[2] -replace '\s+#.*$', '').Trim()
        $table[$Matches[1]] = $value
    }
    return $table
}
$Versions = Read-VersionsEnv "$root\versions.env"

$CertManagerVersion = $Versions.CERT_MANAGER_VERSION
$IngressNginxUrl = "https://raw.githubusercontent.com/kubernetes/ingress-nginx/$($Versions.INGRESS_NGINX_VERSION)/deploy/static/provider/kind/deploy.yaml"

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

# Downloads one specific version of a tool straight from the project's own
# release URL into .tools/<Name>/<Version>/, skipping the download if that exact
# versioned path is already cached (still deterministic -- the path is
# version-qualified, so bumping versions.env naturally invalidates the cache).
# Returns the containing directory; callers prepend it to $env:Path for this
# process only. No admin rights, no permanent PATH mutation, and no risk of a
# second install shadowing it -- unlike winget-installed FireDaemon OpenSSL
# above, which is why openssl stays on the winget path instead of this one (it
# ships as an installer, not a single downloadable binary/zip).
function Get-ToolBinary {
    param(
        [Parameter(Mandatory)][string]$Name,
        [Parameter(Mandatory)][string]$Version,
        [Parameter(Mandatory)][string]$Url,
        [Parameter(Mandatory)][string]$ExeName,
        [string]$ZipEntry = $null
    )
    $dir = "$root\.tools\$Name\$Version"
    $exePath = "$dir\$ExeName"
    if (Test-Path $exePath) { return $dir }

    New-Item -ItemType Directory -Force -Path $dir | Out-Null
    Write-Host "  Fetching $Name $Version..." -ForegroundColor Gray

    if ($ZipEntry) {
        $zipPath = "$dir\download.zip"
        Invoke-WebRequest -Uri $Url -OutFile $zipPath
        Expand-Archive -Path $zipPath -DestinationPath $dir -Force
        Move-Item -Force "$dir\$ZipEntry" $exePath
        Remove-Item $zipPath -Force
        Get-ChildItem $dir -Directory | Remove-Item -Recurse -Force
    } else {
        Invoke-WebRequest -Uri $Url -OutFile $exePath
    }

    if (-not (Test-Path $exePath)) {
        Write-Host "  ERROR: failed to fetch $Name $Version from $Url" -ForegroundColor Red
        exit 1
    }
    return $dir
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

# Runs every check up front and reports a full pass/fail table, instead of
# failing one check at a time minutes into a `kind create cluster` run. Machine
# state (Docker/ports) and cross-file literal pin consistency
# (Dockerfile/docker-compose.yml/values.yaml/kind-config.yaml vs versions.env
# -- see versions.env's header comment for why these can't just be templated
# from one source).
#
# No Python-version check here (unlike k8s/start-k8s.sh's preflight): this
# script never shells out to python for anything -- Set-SecretPlaceholder uses
# .NET regex, RavenDB readiness uses ConvertFrom-Json, both native PowerShell.
# pyproject.toml's requires-python (>=3.11,<3.14) is a real constraint only for
# the Local/docker-compose flow's `uv venv`, which is a different script.
function Invoke-Preflight {
    $checks = @()

    # Docker daemon/memory: soft-checked here (installing Docker Desktop, if
    # missing, happens later in the script) -- the hard, fatal check is still
    # the existing one further down once Docker is guaranteed installed.
    if (Get-Command docker -ErrorAction SilentlyContinue) {
        $memBytes = Invoke-Quiet { docker info --format '{{.MemTotal}}' 2>$null }
        if ($LASTEXITCODE -ne 0 -or -not $memBytes) {
            $checks += @{ Ok = $true; Msg = "Docker daemon not responding yet -- checked again below" }
        } else {
            $memGiB = [math]::Round([int64]$memBytes / 1GB, 1)
            if ($memGiB -lt 2) {
                $checks += @{ Ok = $false; Msg = "Docker memory ${memGiB}GiB is below the ~2GiB minimum for kind + Calico + RavenDB" }
            } else {
                if ($memGiB -lt 4) { Write-Warn "Docker memory ${memGiB}GiB is below the recommended 4GiB+ -- may be tight" }
                $checks += @{ Ok = $true; Msg = "Docker memory: ${memGiB}GiB" }
            }
        }
    } else {
        $checks += @{ Ok = $true; Msg = "docker not yet installed -- installed below" }
    }

    # Ports this script's own port-forwards bind to at the end of a run.
    foreach ($port in @(8001, 8081)) {
        $inUse = Get-NetTCPConnection -LocalPort $port -ErrorAction SilentlyContinue
        $msg = "Port $port free"
        if ($inUse) { $msg = "Port $port already in use" }
        $checks += @{ Ok = (-not $inUse); Msg = $msg }
    }

    $dockerfile = Get-Content "$root\Dockerfile" -Raw
    $fromMatch = [regex]::Match($dockerfile, 'FROM python:([\w.\-]+)')
    $checks += @{
        Ok  = ($fromMatch.Success -and $fromMatch.Groups[1].Value -eq $Versions.PYTHON_IMAGE_TAG)
        Msg = "Dockerfile FROM python:$($fromMatch.Groups[1].Value) matches versions.env PYTHON_IMAGE_TAG=$($Versions.PYTHON_IMAGE_TAG)"
    }
    $uvMatch = [regex]::Match($dockerfile, 'uv==([\d.]+)')
    $checks += @{
        Ok  = ($uvMatch.Success -and $uvMatch.Groups[1].Value -eq $Versions.UV_VERSION)
        Msg = "Dockerfile uv==$($uvMatch.Groups[1].Value) matches versions.env UV_VERSION=$($Versions.UV_VERSION)"
    }

    $composeMatch = [regex]::Match((Get-Content "$root\docker-compose.yml" -Raw), 'image:\s*ravendb/ravendb:([\w.\-]+)')
    $valuesMatch = [regex]::Match((Get-Content "$root\k8s\ravendb\values.yaml" -Raw), 'image:\s*ravendb/ravendb:([\w.\-]+)')
    $ravenOk = $composeMatch.Success -and $valuesMatch.Success `
        -and $composeMatch.Groups[1].Value -eq $Versions.RAVENDB_IMAGE_TAG `
        -and $valuesMatch.Groups[1].Value -eq $Versions.RAVENDB_IMAGE_TAG
    $checks += @{
        Ok  = $ravenOk
        Msg = "RavenDB image tag agrees: docker-compose.yml=$($composeMatch.Groups[1].Value) values.yaml=$($valuesMatch.Groups[1].Value) versions.env=$($Versions.RAVENDB_IMAGE_TAG)"
    }

    $kindImgMatch = [regex]::Match((Get-Content "$root\k8s\kind-config.yaml" -Raw), 'image:\s*(\S+)')
    $checks += @{
        Ok  = ($kindImgMatch.Success -and $kindImgMatch.Groups[1].Value -eq $Versions.KIND_NODE_IMAGE)
        Msg = "k8s/kind-config.yaml node image matches versions.env KIND_NODE_IMAGE"
    }

    # Checked here, not just later when the ravendb-license k8s Secret gets
    # created from this file: the RavenDB operator's admission webhook always
    # requires spec.licenseSecretRef to resolve, so a missing license.json is
    # guaranteed to fail eventually. Was found deep in the script's own
    # secrets/certs step (well past the point where a stale, already-cached
    # RAVENDB_LICENSE value in k8s/secrets.local.yaml can make "OK: RAVENDB_LICENSE
    # already set" print even though this file no longer exists on disk --
    # those are two independent, unsynchronized representations of the license,
    # see Ensure-RavenDbCerts's caller below). Checking it here means the whole
    # run fails in seconds, before Docker/kind/cert work even starts.
    $licenseJsonOk = Test-Path "$root\license.json"
    $checks += @{
        Ok  = $licenseJsonOk
        Msg = if ($licenseJsonOk) {
            "license.json present at repo root"
        } else {
            "license.json missing at repo root (required for the ravendb-license k8s Secret)"
        }
    }

    Write-Host "`n  Preflight:" -ForegroundColor Cyan
    $failed = $false
    foreach ($c in $checks) {
        if ($c.Ok) { Write-Host "    OK    $($c.Msg)" -ForegroundColor Green }
        else { Write-Host "    FAIL  $($c.Msg)" -ForegroundColor Red; $failed = $true }
    }
    Write-Host ""
    if ($failed) {
        Write-Host "  ERROR: preflight failed -- fix the above before continuing." -ForegroundColor Red
        exit 1
    }
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

    $required = @("ca.crt", "server.crt", "server.pfx", "client.pfx")
    $allPresent = $true
    foreach ($f in $required) {
        if (-not (Test-Path "$CertsDir\$f")) { $allPresent = $false; break }
    }

    # Not routed through Ensure-CliTool: that helper's "already installed?"
    # check is Get-Command-based, which is unreliable for this package in
    # both directions -- see Resolve-OpenSslExe above. Resolve directly, only
    # falling back to a winget install if genuinely not found anywhere.
    # Resolved unconditionally (even on the reuse path below) since the SAN
    # validation on an already-present chain also needs it.
    $openssl = Resolve-OpenSslExe
    if (-not $openssl) {
        if (Get-Command winget -ErrorAction SilentlyContinue) {
            Write-Warn "OpenSSL not found -- installing via winget (FireDaemon.OpenSSL $($Versions.OPENSSL_VERSION))..."
            winget install -e --id FireDaemon.OpenSSL --version $Versions.OPENSSL_VERSION --accept-package-agreements --accept-source-agreements
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

    if ($allPresent) {
        # Otherwise the chain would be reused forever as long as the files
        # exist, even after values.yaml's active node tags change to include
        # one the cert never covered (e.g. uncommenting b/c) -- catch that
        # here with a clear message instead of a confusing TLS handshake
        # failure deep inside RavenDB's own
        # AssertServerCanContactItselfWhenAuthIsOn startup check.
        #
        # Subset check, not exact match: the cert covering MORE than the
        # currently active tags is harmless (e.g. it was generated for a/b/c
        # and values.yaml later scaled down to just a for a license limit --
        # RavenDB doesn't care about unused SAN entries). Only a MISSING SAN
        # for a currently active tag is the actual risk.
        $expectedSans = ($NodeTags | ForEach-Object { "DNS:$_.hiddencity.local", "DNS:$_-tcp.hiddencity.local" }) | Sort-Object
        $sanOutput = & $openssl x509 -in "$CertsDir\server.crt" -noout -ext subjectAltName 2>$null
        $actualSans = [regex]::Matches(($sanOutput -join " "), '(DNS|IP Address):[^\s,]+') |
            ForEach-Object { $_.Value } | Sort-Object

        $missingSans = @($expectedSans | Where-Object { $actualSans -notcontains $_ })
        if ($actualSans.Count -gt 0 -and $missingSans.Count -eq 0) {
            Write-Ok "RavenDB TLS certs already present in k8s/ravendb/certs -- reusing"
            return
        }

        Write-Host "  ERROR: k8s/ravendb/certs/server.crt is missing SANs for the current node tags." -ForegroundColor Red
        Write-Host "    Missing: $($missingSans -join ', ')" -ForegroundColor Gray
        Write-Host "    Cert covers: $($actualSans -join ', ')" -ForegroundColor Gray
        Write-Host "    Fix: delete k8s/ravendb/certs and re-run to regenerate for the current node tags." -ForegroundColor Gray
        Write-Host "    If a cluster is already bootstrapped, adding nodes needs the operator's manual" -ForegroundColor Gray
        Write-Host "    topology-change procedure -- regenerating local cert files alone won't add them." -ForegroundColor Gray
        exit 1
    }

    # Fail fast with a clear message if the legacy provider (needed for the
    # -legacy PKCS12 export below) can't load, instead of surfacing a cryptic
    # DSO_load stack trace deep inside pkcs12 export.
    #
    # This runs the REAL operation (a throwaway `pkcs12 -export -legacy`) as
    # the probe, not a proxy check. An earlier version probed via
    # `openssl list -providers -legacy`, which looked like a capability check
    # but isn't valid input for every build's `list` subcommand even when the
    # legacy provider itself loads and works fine -- confirmed for real on
    # FireDaemon OpenSSL 4.0.1: `list -providers -legacy` fails with
    # "Unknown option: -legacy" while `pkcs12 -export -legacy` (the actual
    # command used below) succeeds outright on that same install. That false
    # positive blocked an already-working setup and sent it into a pointless
    # winget reinstall loop. Test the thing you actually need, not a stand-in
    # for it.
    function Test-OpenSslLegacyPkcs12 {
        param([string]$OpenSslExe)
        $tmp = Join-Path ([System.IO.Path]::GetTempPath()) ("osslcheck-" + [guid]::NewGuid())
        New-Item -ItemType Directory -Force -Path $tmp | Out-Null
        try {
            & $OpenSslExe req -x509 -newkey rsa:2048 -keyout "$tmp\k.key" -out "$tmp\k.crt" `
                -days 1 -nodes -subj "/CN=legacy-probe" *> $null
            & $OpenSslExe pkcs12 -export -legacy -out "$tmp\k.pfx" `
                -inkey "$tmp\k.key" -in "$tmp\k.crt" -passout pass: *> $null
            return $LASTEXITCODE -eq 0
        } finally {
            Remove-Item -Recurse -Force $tmp -ErrorAction SilentlyContinue
        }
    }

    $legacyOk = Test-OpenSslLegacyPkcs12 -OpenSslExe $openssl

    if (-not $legacyOk) {
        # A genuinely incapable/broken openssl (e.g. an old "OpenSSL-Win64"
        # install with no provider architecture at all, or a FireDaemon
        # install missing legacy.dll) was resolved. Don't just error out
        # here: install/repair FireDaemon specifically and re-resolve --
        # Resolve-OpenSslExe checks FireDaemon's own install directory before
        # ever falling back to PATH, so this bypasses whatever broken openssl
        # was shadowing it, the same way the "not found at all" branch above
        # already does.
        if (Get-Command winget -ErrorAction SilentlyContinue) {
            Write-Warn "Resolved openssl ($openssl) can't do a -legacy PKCS12 export -- installing FireDaemon.OpenSSL $($Versions.OPENSSL_VERSION) instead..."
            winget install -e --id FireDaemon.OpenSSL --version $Versions.OPENSSL_VERSION --accept-package-agreements --accept-source-agreements
            $openssl = Resolve-OpenSslExe
            if ($openssl) {
                $legacyOk = Test-OpenSslLegacyPkcs12 -OpenSslExe $openssl
            }
        }
    }

    if (-not $legacyOk) {
        Write-Host "  ERROR: openssl can't do a -legacy PKCS12 export (needed for the operator's cert format)." -ForegroundColor Red
        Write-Host "    Resolved openssl: $openssl" -ForegroundColor Gray
        Write-Host "    A different/older openssl install may be shadowing FireDaemon's, or this" -ForegroundColor Gray
        Write-Host "    install's legacy provider module is missing/broken -- check with" -ForegroundColor Gray
        Write-Host "    'where.exe openssl' and '& `"$openssl`" list -providers -provider legacy', then re-run." -ForegroundColor Gray
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
subjectKeyIdentifier = hash
"@ | Set-Content "$CertsDir\ca-ext.cnf" -Encoding utf8

    # Two extension sections: req_ext (no AKI -- used while generating the CSR,
    # before any CA is in scope) and sign_ext (adds authorityKeyIdentifier --
    # used only when x509 signs the CSR, where -CA/-CAkey give it something to
    # point at). Folding authorityKeyIdentifier into the section req_extensions
    # references makes `openssl req -new` itself fail ("no issuer certificate"),
    # since req has no -CA at that point -- confirmed reproducing it locally.
    @"
[req]
distinguished_name = dn
req_extensions = req_ext
[dn]
[req_ext]
subjectAltName = $sanEntries
keyUsage = critical, digitalSignature, keyEncipherment
extendedKeyUsage = serverAuth, clientAuth
[sign_ext]
subjectAltName = $sanEntries
keyUsage = critical, digitalSignature, keyEncipherment
extendedKeyUsage = serverAuth, clientAuth
authorityKeyIdentifier = keyid,issuer
"@ | Set-Content "$CertsDir\server-san.cnf" -Encoding utf8

    @"
[req]
distinguished_name = dn
req_extensions = req_ext
[dn]
[req_ext]
keyUsage = critical, digitalSignature, keyEncipherment
extendedKeyUsage = clientAuth
[sign_ext]
keyUsage = critical, digitalSignature, keyEncipherment
extendedKeyUsage = clientAuth
authorityKeyIdentifier = keyid,issuer
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
        -extfile "$CertsDir\server-san.cnf" -extensions sign_ext

    # Client
    & $openssl genrsa -out "$CertsDir\client.key" 2048 2>$null
    & $openssl req -new -key "$CertsDir\client.key" -out "$CertsDir\client.csr" `
        -subj "/CN=hidden-city-client" -config "$CertsDir\client-ext.cnf"
    & $openssl x509 -req -in "$CertsDir\client.csr" -CA "$CertsDir\ca.crt" -CAkey "$CertsDir\ca.key" `
        -CAcreateserial -out "$CertsDir\client.crt" -days 825 -sha256 `
        -extfile "$CertsDir\client-ext.cnf" -extensions sign_ext

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

Write-Host ""
Write-Host "  Make sure Docker Desktop is running before continuing (kind, the RavenDB" -ForegroundColor Yellow
Write-Host "  cluster, and the app image all run as Docker containers)." -ForegroundColor Yellow

$toolsArch = "amd64"

# --- delete cluster shortcut ---
# Fetches only kind (not kubectl/helm -- unneeded for this) before deleting.
# Before this fetch existed here, -DeleteCluster on a machine that had never
# fetched kind into .tools/ yet failed outright with "kind: command not
# found" -- confirmed by actually running it, a real bug found by testing.
# Before the .tools/ mechanism existed this never surfaced, since kind was
# winget-installed globally on PATH regardless of step order.
if ($DeleteCluster) {
    $kindDir = Get-ToolBinary -Name "kind" -Version $Versions.KIND_VERSION -ExeName "kind.exe" `
        -Url "https://kind.sigs.k8s.io/dl/$($Versions.KIND_VERSION)/kind-windows-$toolsArch"
    $env:Path = "$kindDir;$env:Path"
    Write-Host "`nDeleting kind cluster '$ClusterName'..." -ForegroundColor Cyan
    kind delete cluster --name $ClusterName
    Write-Ok "Cluster deleted"
    exit 0
}

# --- preflight: fail in seconds, not eight minutes into `kind create cluster` ---
Invoke-Preflight

# --- prerequisites: kind/kubectl/helm fetched deterministically into .tools/ ---
# Not winget: exact pinned versions (versions.env), no admin rights, no PATH
# mutation beyond this process, and immune to the class of cross-installer PATH
# conflict that broke openssl's -legacy provider earlier in this script (see
# Resolve-OpenSslExe). Prepending the fetched dirs to $env:Path for this process
# only means every bare `kind`/`kubectl`/`helm` call below the rest of this
# script already uses resolves to the exact pinned binary.
Write-Host ""
$kindDir = Get-ToolBinary -Name "kind" -Version $Versions.KIND_VERSION -ExeName "kind.exe" `
    -Url "https://kind.sigs.k8s.io/dl/$($Versions.KIND_VERSION)/kind-windows-$toolsArch"
$kubectlDir = Get-ToolBinary -Name "kubectl" -Version $Versions.KUBECTL_VERSION -ExeName "kubectl.exe" `
    -Url "https://dl.k8s.io/release/$($Versions.KUBECTL_VERSION)/bin/windows/$toolsArch/kubectl.exe"
$helmDir = Get-ToolBinary -Name "helm" -Version $Versions.HELM_VERSION -ExeName "helm.exe" `
    -Url "https://get.helm.sh/helm-$($Versions.HELM_VERSION)-windows-$toolsArch.zip" `
    -ZipEntry "windows-$toolsArch\helm.exe"
$env:Path = "$kindDir;$kubectlDir;$helmDir;$env:Path"
Write-Ok "kind $($Versions.KIND_VERSION), kubectl $($Versions.KUBECTL_VERSION), helm $($Versions.HELM_VERSION) ready (.tools/)"

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

Write-Ok "docker found"

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

# license.json's presence is already asserted in Invoke-Preflight, at the very
# start of the run -- no need to re-check it here.

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
# NOT `kubectl config use-context` -- a kind cluster's existence (a set of
# Docker containers) and its kubeconfig entry (a file on whatever machine/user
# ran `kind create cluster`) are independent state. The "reusing" branch above
# only proves the cluster exists in Docker; if this environment's kubeconfig
# never had the context (confirmed hitting this for real while testing
# k8s/start-k8s.sh from WSL2 Ubuntu against a cluster this script had created
# from Windows on the same shared Docker Desktop engine), `use-context` fails
# with "no context exists". `kind export kubeconfig` (re)writes the entry and
# sets it current either way -- safe and idempotent whether the cluster was
# just created above or reused.
kind export kubeconfig --name $ClusterName | Out-Null

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
    kubectl create -f "https://raw.githubusercontent.com/projectcalico/calico/$($Versions.CALICO_VERSION)/manifests/tigera-operator.yaml" 2>$null
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
        # Was warn-and-skip: the RavenDB Helm install a few steps later
        # references all four of these by name (spec.licenseSecretRef,
        # certificate refs), so a silently-skipped secret here is guaranteed
        # to resurface as a cryptic admission-webhook or cert-mount failure
        # downstream -- the exact same "existence, not capability" shape as
        # the license.json and openssl bugs above. license.json's existence
        # is already asserted in Invoke-Preflight; the three cert files are
        # asserted right after generation in Ensure-RavenDbCerts. Reaching
        # this branch means one disappeared between then and now (or a real
        # bug in this script) -- either way, fail loudly here instead of
        # producing a cluster that looks like it's coming up and isn't.
        Write-Host "  ERROR: can't create secret $($s.Name) -- source file missing: $($s.SourceFile)" -ForegroundColor Red
        exit 1
    }
    $fromFileArg = "--from-file=$($s.FromFile)=$($s.SourceFile)"
    $yaml = kubectl create secret generic $s.Name -n $NS $fromFileArg --dry-run=client -o yaml
    $yaml | kubectl apply -f - | Out-Null
}
Write-Ok "RavenDB license/cert secrets applied"

# --- docker build ---
$imageRebuilt = $false
if (-not $SkipBuild) {
    $step++
    Write-Step $step $totalSteps "Building Docker image  ->  $ImageTag"
    docker build -t $ImageTag "$root"
    if ($LASTEXITCODE -ne 0) { Write-Host "  ERROR: docker build failed" -ForegroundColor Red; exit 1 }
    Write-Ok "Image built"

    Write-Host "  Loading image into kind cluster..." -ForegroundColor Gray
    kind load docker-image $ImageTag --name $ClusterName
    Write-Ok "Image loaded into kind"
    $imageRebuilt = $true
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
    # 300s, not 120s: on a freshly-created kind node (e.g. right after
    # -DeleteCluster), containerd has nothing cached -- every image (Calico,
    # cert-manager, this controller, its webhook cert-gen job) pulls from
    # scratch. Confirmed hitting the 120s ceiling for real on a cold cluster:
    # `kubectl describe pod` showed the image pull alone took 2m0.48s --
    # 0.48s over the old timeout, plus repeated FailedMount retries on the
    # webhook-cert secret while the admission Jobs were still creating it
    # (expected, self-resolving). Not a config or probe problem, just not
    # enough runway for a cold pull.
    kubectl wait --namespace ingress-nginx `
        --for=condition=ready pod `
        --selector=app.kubernetes.io/component=controller `
        --timeout=300s
    if ($LASTEXITCODE -ne 0) { Write-Host "  ERROR: ingress-nginx controller pod did not become ready in time." -ForegroundColor Red; exit 1 }
    Write-Ok "ingress-nginx ready"

    Write-Host "  Adding RavenDB operator Helm repo..." -ForegroundColor Gray
    helm repo add ravendb-operator https://ravendb.github.io/ravendb-operator/helm | Out-Null
    helm repo update | Out-Null

    Write-Host "  Installing RavenDB Kubernetes Operator..." -ForegroundColor Gray
    helm upgrade --install ravendb-operator ravendb-operator/ravendb-operator `
        -n ravendb-operator-system --create-namespace `
        --version $($Versions.RAVENDB_OPERATOR_CHART_VERSION)
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
    -n $NS --create-namespace -f "$root\k8s\ravendb\values.yaml" `
    --version $($Versions.RAVENDB_CLUSTER_CHART_VERSION)
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

# --- wait for RavenDB ---
# Before app manifests, not after: the agent pod's own startup used to race
# RavenDB's readiness (it starts trying to create the RavenDB *database*
# immediately), and a single failed attempt there was silently swallowed --
# confirmed in practice serving 500s forever with the cluster otherwise
# healthy. src/db/seed.py's ensure_database now retries that on its own, so
# this reordering is defense in depth (fewer pods ever need to exercise that
# retry path), not the only thing standing between here and that bug.
$step++
Write-Step $step $totalSteps "Waiting for RavenDB cluster (60-120s)"
Write-Host "  Operator is: creating PVCs -> starting pods -> forming Raft quorum -> issuing TLS certs" -ForegroundColor Gray
Write-Host ""

# The Operator publishes 9 status conditions (bootstrap, cert wiring, Raft
# formation, etc.) beyond just "Ready" -- surfacing them while we wait (and in
# full on timeout) gives a developer something to act on immediately instead
# of being told to go run kubectl describe themselves after the fact.
function Get-RavenConditions {
    # -o json + ConvertFrom-Json, not -o jsonpath: PowerShell's native-argument
    # quoting strips the embedded double quotes a jsonpath filter needs
    # (@.type=="Ready") before kubectl.exe ever sees them (verified -- the
    # jsonpath form always returned empty/exit 1 even once the cluster was
    # genuinely Ready), which silently kept this check permanently "not ready".
    $json = Invoke-Quiet { kubectl get ravendbcluster ravendb-cluster -n $NS -o json 2>$null } | Out-String
    if (-not $json) { return $null }
    try {
        return ($json | ConvertFrom-Json).status.conditions
    } catch {
        return $null
    }
}

function Format-RavenCondition($cond) {
    $line = "$($cond.type)=$($cond.status)"
    if ($cond.reason) { $line += " ($($cond.reason))" }
    if ($cond.message) { $line += ": $($cond.message)" }
    return $line
}

$ravenReady = $false
for ($i = 1; $i -le 40; $i++) {
    $conditions = Get-RavenConditions
    $readyCond = $conditions | Where-Object { $_.type -eq "Ready" }
    if ($readyCond -and $readyCond.status -eq "True") { $ravenReady = $true; break }

    Write-Host ("  [{0,2}/40] Not ready yet... ({1})" -f $i, (Get-Date -Format "HH:mm:ss")) -ForegroundColor Gray
    foreach ($cond in ($conditions | Where-Object { $_.status -ne "True" })) {
        Write-Host "           $(Format-RavenCondition $cond)" -ForegroundColor Gray
    }
    Start-Sleep 5
}

if ($ravenReady) {
    Write-Ok "RavenDB cluster Ready"
} else {
    Write-Warn "RavenDB did not reach Ready in time. Full status conditions:"
    foreach ($cond in (Get-RavenConditions)) {
        Write-Warn "  $(Format-RavenCondition $cond)"
    }
    Write-Warn "Also check:"
    Write-Warn "  kubectl describe ravendbcluster ravendb-cluster -n $NS"
    Write-Warn "  kubectl get pods -n $NS"
}

# --- deploy app manifests ---
$step++
Write-Step $step $totalSteps "Deploying app manifests"

kubectl apply -f "$root\k8s\namespace.yaml"
kubectl apply -f $secretsFile
kubectl apply -k "$root\k8s\"
Write-Ok "Manifests applied"

# `kubectl apply` only triggers a new rollout when the manifest text itself
# changes -- with a mutable `hidden-city:latest` tag, a rebuilt image loaded
# under the same tag leaves the Deployment's pod template textually identical,
# so already-running pods keep serving the OLD image forever until something
# forces a restart (confirmed in practice: pods stayed up unchanged after a
# rebuild+reload, still serving stale code). Force it whenever this run
# actually rebuilt the image -- not on every run, so an unrelated
# --skip-operator/config-only re-run doesn't bounce pods for no reason.
if ($imageRebuilt) {
    Write-Host "  Restarting agent/worker to pick up the freshly built image..." -ForegroundColor Gray
    kubectl rollout restart deployment/agent -n $NS | Out-Null
    kubectl rollout restart deployment/subscription-worker -n $NS | Out-Null
}

$step++
Write-Step $step $totalSteps "Waiting for agent deployment (up to ~3 min on a cold DB -- see k8s/agent/deployment.yaml)"
# 180s: the Travelpayouts bulk scraper no longer blocks readiness (it runs as
# a background task in _startup() -- see app.py) -- what's left gating it is
# ensure_database's own retry budget (up to 90s) plus two quick API
# validation calls. This used to be 1200s to match the scraper blocking
# startup for ~20 min; confirmed hitting that ceiling for real on a cold,
# 2-replica rollout before the background-task fix (both pods scraping ~80
# origins concurrently, pushing past even that generous allowance).
kubectl rollout status deployment/agent -n $NS --timeout=180s
if ($LASTEXITCODE -ne 0) { Write-Host "  ERROR: agent deployment did not roll out in time." -ForegroundColor Red; exit 1 }
Write-Ok "Agent deployment ready"

# --- port-forwards ---
# The chart creates one Service per node tag (ravendb-<tag>, e.g. ravendb-a),
# not a single "ravendb-cluster-svc" -- pick the first configured tag. RavenDB
# only listens on HTTPS (443), even in mode: None (self-signed, not plaintext).
# Local port 8081 (not 8080) deliberately avoids clashing with Local mode's
# docker-compose RavenDB, which also binds host port 8080 -- but this only
# avoids the RavenDB port clash. `.\start.ps1 -Mode Local` and
# `.\start.ps1 -Mode K8s` still CANNOT run at the same time: both bind the
# agent to local port 8001 (Local mode's uvicorn, K8s mode's port-forward of
# svc/agent-svc), so the second one to start fails to bind that port. Stop one
# mode fully before starting the other.
Write-Host "`n  Starting port-forwards..." -ForegroundColor Gray

$firstNodeTag = (Get-RavenNodeTags | Select-Object -First 1)

$pfAgent = Start-Process kubectl `
    -ArgumentList @("port-forward", "svc/agent-svc", "8001:80", "-n", $NS) `
    -PassThru -WindowStyle Hidden

$pfRaven = Start-Process kubectl `
    -ArgumentList @("port-forward", "svc/ravendb-$firstNodeTag", "8081:443", "-n", $NS) `
    -PassThru -WindowStyle Hidden

# --- done ---
Write-Host ""
Write-Host "  =============================================" -ForegroundColor Green
Write-Ok "Agent:          http://localhost:8001"
Write-Ok "Swagger UI:     http://localhost:8001/docs"
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
