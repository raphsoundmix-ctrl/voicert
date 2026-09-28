<#
.SYNOPSIS
    make-equivalent for the VoiceRT bridge container on Windows.

.DESCRIPTION
    Wraps docker compose so the repo path -- "D:\VOICE RT\AI_Voice_Agent_Demo",
    which contains a space -- is always quoted. Every path here goes through
    $PSScriptRoot, so the script works from any working directory.

    Windows PowerShell 5.1 compatible: no '&&', no ternary, no '??'.

.EXAMPLE
    .\voicert.ps1 up
    .\voicert.ps1 up -Local
    .\voicert.ps1 logs
    .\voicert.ps1 test
    .\voicert.ps1 down
#>

[CmdletBinding()]
param(
    [Parameter(Position = 0)]
    [ValidateSet('up', 'down', 'logs', 'rebuild', 'test', 'status', 'health', 'shell', 'help')]
    [string]$Command = 'help',

    # Target the Ollama-backed service (profile: local) instead of the stub.
    [switch]$Local,

    # up: skip the implicit build.
    [switch]$NoBuild,

    # logs: how many lines of history before following.
    [int]$Tail = 200,

    # up/health: seconds to wait for the container to report healthy.
    [int]$HealthTimeout = 90
)

# Deliberately NOT 'Stop'. docker writes all build progress to stderr; in
# Windows PowerShell 5.1 those lines become ErrorRecords, and with
# ErrorActionPreference=Stop a *successful* build turns into a terminating
# NativeCommandError the moment anyone redirects this script's output
# (`.\voicert.ps1 test 2>&1 | ...`). Every failure below is caught by an
# explicit $LASTEXITCODE check instead, which is exact rather than lucky.
$ErrorActionPreference = 'Continue'
Set-StrictMode -Version 2.0

# --- paths ------------------------------------------------------------------
# $PSScriptRoot is "D:\VOICE RT\AI_Voice_Agent_Demo". Quote every use of it.
$ProjectRoot = $PSScriptRoot
$ComposeFile = Join-Path $ProjectRoot 'docker-compose.yml'
$EnvFile     = Join-Path $ProjectRoot '.env'
$EnvExample  = Join-Path $ProjectRoot '.env.example'

if (-not (Test-Path -LiteralPath $ComposeFile)) {
    throw "docker-compose.yml not found next to this script: `"$ComposeFile`""
}

# --- service selection ------------------------------------------------------
if ($Local) {
    $Service   = 'bridge-local'
    $Container = 'voicert-bridge-local'
    $Profiles  = @('--profile', 'local')
    $PortVar   = 'VOICERT_LOCAL_HOST_PORT'
    $PortDflt  = '8766'
}
else {
    $Service   = 'bridge'
    $Container = 'voicert-bridge'
    $Profiles  = @()
    $PortVar   = 'VOICERT_HOST_PORT'
    $PortDflt  = '8767'
}

function Write-Step {
    param([string]$Text)
    Write-Host "==> $Text" -ForegroundColor Cyan
}

function Write-Warn {
    param([string]$Text)
    Write-Host "!!  $Text" -ForegroundColor Yellow
}

function Invoke-Compose {
    param([string[]]$ComposeArgs)
    $full = @('compose', '-f', $ComposeFile) + $Profiles + $ComposeArgs
    & docker @full
    # Returns nothing on purpose. `docker compose --build` writes BuildKit
    # progress to stderr; in PowerShell those records become part of a
    # function's return value, so `return $LASTEXITCODE` here hands the
    # caller an array of build log lines with the exit code stapled to the
    # end -- and every exit-code check then fails on a successful build.
    # Read $LASTEXITCODE at the call site instead.
}

function Assert-Compose {
    param([string[]]$ComposeArgs)
    Invoke-Compose -ComposeArgs $ComposeArgs
    if ($LASTEXITCODE -ne 0) {
        throw "docker compose $($ComposeArgs -join ' ') failed with exit code $LASTEXITCODE"
    }
}

function Get-HostPort {
    # Same precedence Compose itself uses for ${VAR:-default}: a real
    # environment variable beats .env, which beats the compose default.
    $fromEnv = [Environment]::GetEnvironmentVariable($PortVar)
    if ($fromEnv) { return $fromEnv }
    if (Test-Path -LiteralPath $EnvFile) {
        $line = Select-String -LiteralPath $EnvFile -Pattern "^\s*$PortVar\s*=\s*(\S+)" -ErrorAction SilentlyContinue | Select-Object -First 1
        if ($line) { return $line.Matches[0].Groups[1].Value }
    }
    return $PortDflt
}

function Test-EnvFile {
    if (-not (Test-Path -LiteralPath $EnvFile)) {
        Write-Warn ".env not found. The stub bridge needs no keys, so this is fine."
        if (Test-Path -LiteralPath $EnvExample) {
            Write-Host "    Template: Copy-Item `"$EnvExample`" `"$EnvFile`""
        }
    }
}

function Wait-Healthy {
    param([int]$TimeoutSeconds)
    Write-Step "waiting for $Container to report healthy (timeout ${TimeoutSeconds}s)"
    $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
    while ((Get-Date) -lt $deadline) {
        $state = & docker inspect --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}nohealth{{end}}' $Container 2>$null
        if ($LASTEXITCODE -ne 0) { Start-Sleep -Milliseconds 700; continue }
        $state = "$state".Trim()
        if ($state -eq 'healthy')  { Write-Host "    healthy" -ForegroundColor Green; return $true }
        if ($state -eq 'nohealth') { Write-Warn "container has no healthcheck"; return $true }
        if ($state -eq 'unhealthy') {
            Write-Warn "container is UNHEALTHY -- last probe output:"
            & docker inspect --format '{{range .State.Health.Log}}{{.Output}}{{end}}' $Container
            return $false
        }
        Start-Sleep -Milliseconds 700
    }
    Write-Warn "timed out waiting for healthy"
    return $false
}

switch ($Command) {

    'up' {
        Test-EnvFile
        $upArgs = @('up', '-d')
        if (-not $NoBuild) { $upArgs += '--build' }
        if ($Local) { $upArgs += $Service }   # start only the local service
        Write-Step "starting $Service"
        Assert-Compose -ComposeArgs $upArgs
        $ok = Wait-Healthy -TimeoutSeconds $HealthTimeout
        $port = Get-HostPort
        Write-Host ""
        Write-Host "Unity NPC inspector -> host 127.0.0.1   port $port" -ForegroundColor Green
        Write-Host "Logs:  .\voicert.ps1 logs$(if ($Local) { ' -Local' })"
        if (-not $ok) { exit 1 }
    }

    'down' {
        Write-Step 'stopping and removing containers (named volumes are kept)'
        Assert-Compose -ComposeArgs @('down')
        Write-Host "    to drop the model volume too: docker compose -f `"$ComposeFile`" down -v" -ForegroundColor DarkGray
    }

    'logs' {
        Write-Step "following logs for $Service (Ctrl+C to detach; the container keeps running)"
        # Not Assert-Compose: Ctrl+C here returns a non-zero code and that is
        # the normal way to leave a log follow.
        Invoke-Compose -ComposeArgs @('logs', '-f', "--tail=$Tail", $Service)
    }

    'rebuild' {
        Write-Step 'rebuilding image from scratch (no layer cache)'
        Assert-Compose -ComposeArgs @('build', '--no-cache', '--pull', $Service)
        Write-Step 'recreating container'
        Assert-Compose -ComposeArgs @('up', '-d', '--force-recreate', $Service)
        $ok = Wait-Healthy -TimeoutSeconds $HealthTimeout
        if (-not $ok) { exit 1 }
    }

    'test' {
        # 1) The repo's own suite, inside the image, against the exact
        #    interpreter the bridge will run on.
        Write-Step 'building the test stage'
        & docker build --target test -t voicert-bridge:test "$ProjectRoot"
        if ($LASTEXITCODE -ne 0) { throw "test image build failed ($LASTEXITCODE)" }

        Write-Step 'running pytest in the container'
        & docker run --rm voicert-bridge:test
        if ($LASTEXITCODE -ne 0) { throw "pytest failed ($LASTEXITCODE)" }

        # 2) The protocol probe, against the container that is actually up.
        $running = & docker ps --filter "name=^/$Container$" --filter 'status=running' --format '{{.Names}}' 2>$null
        if ("$running".Trim() -eq $Container) {
            Write-Step "protocol handshake against the running $Container"
            & docker exec $Container python /opt/voicert/healthcheck.py
            if ($LASTEXITCODE -ne 0) { throw "handshake probe failed ($LASTEXITCODE)" }
        }
        else {
            Write-Warn "$Container is not running; skipped the live handshake probe."
            Write-Host "    start it first: .\voicert.ps1 up$(if ($Local) { ' -Local' })"
        }
        Write-Host "all tests passed" -ForegroundColor Green
    }

    'status' {
        Invoke-Compose -ComposeArgs @('ps')
        Write-Host ""
        Write-Host "host port for $Service : $(Get-HostPort)"
    }

    'health' {
        # The real handshake, not a port check: HELLO in, READY out.
        Write-Step "HELLO/READY probe inside $Container"
        & docker exec $Container python /opt/voicert/healthcheck.py
        exit $LASTEXITCODE
    }

    'shell' {
        Write-Step "shell in $Container (non-root, uid 10001)"
        & docker exec -it $Container /bin/sh
    }

    default {
        Write-Host @"
voicert.ps1 <command> [-Local] [-NoBuild] [-Tail N] [-HealthTimeout N]

  up        build if needed, start, wait for the HELLO/READY healthcheck
  down      stop and remove containers (named volumes survive)
  logs      follow container logs
  rebuild   --no-cache --pull rebuild, then force-recreate
  test      pytest inside the image, then a live protocol handshake
  status    docker compose ps + the host port to put in the Unity inspector
  health    run the HELLO/READY probe once and exit with its code
  shell     /bin/sh inside the running container

  -Local    act on the Ollama-backed service (profile: local, port 8766)

Project: "$ProjectRoot"
"@
    }
}
