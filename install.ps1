$ErrorActionPreference = 'Stop'
Set-StrictMode -Version 2

$repository = 'https://github.com/ThisisPeggy/Unfold-Hermes-Connector'
$pluginName = 'hermes-browser'
$gatewayStopped = $false
$revision = if ($env:HERMES_BROWSER_CONNECTOR_COMMIT) {
    $env:HERMES_BROWSER_CONNECTOR_COMMIT.Trim().ToLowerInvariant()
} else {
    'origin/main'
}
if ($revision -ne 'origin/main' -and $revision -notmatch '^[0-9a-f]{40}$') {
    throw 'HERMES_BROWSER_CONNECTOR_COMMIT must be a 40-character Git commit.'
}

function Invoke-Checked {
    param([scriptblock]$Command, [string]$Message)
    & $Command
    if ($LASTEXITCODE -ne 0) { throw $Message }
}

function Get-HermesHome {
    if ($env:HERMES_HOME) { return [Environment]::ExpandEnvironmentVariables($env:HERMES_HOME) }
    if (-not $env:LOCALAPPDATA) { throw 'LOCALAPPDATA is unavailable. Set HERMES_HOME and try again.' }
    return (Join-Path $env:LOCALAPPDATA 'hermes')
}

function Test-GitCheckout {
    param([string]$Path)
    try {
        $gitDir = Join-Path $Path '.git'
        return ((Test-Path -LiteralPath (Join-Path $gitDir 'HEAD') -PathType Leaf) -and
            (Test-Path -LiteralPath (Join-Path $gitDir 'config') -PathType Leaf) -and
            (Test-Path -LiteralPath (Join-Path $gitDir 'objects') -PathType Container))
    } catch {
        return $false
    }
}

function Move-BrokenConnector {
    param([string]$Path, [string]$HermesHome)
    $backupRoot = Join-Path $HermesHome 'plugin-backups'
    $backupName = 'hermes-browser-' + (Get-Date -Format 'yyyyMMdd-HHmmss')
    $backupPath = Join-Path $backupRoot $backupName
    try {
        New-Item -ItemType Directory -Path $backupRoot -Force | Out-Null
        Move-Item -LiteralPath $Path -Destination $backupPath -ErrorAction Stop
        Write-Host "Moved the incomplete Connector to $backupPath"
    } catch {
        throw @"
The existing Connector is incomplete and Windows denied access to it:
  $Path

Close programs using that folder and run these commands once from PowerShell opened as Administrator:
  takeown.exe /F `"$Path`" /R /D Y
  icacls.exe `"$Path`" /grant `"${env:USERNAME}:(OI)(CI)F`" /T /C

Then run the install command again.
"@
    }
}

try {
    Get-Command hermes -ErrorAction Stop | Out-Null
    Get-Command git -ErrorAction Stop | Out-Null

    & hermes gateway stop *> $null
    $gatewayStopped = $true

    $hermesHome = Get-HermesHome
    $pluginDir = Join-Path (Join-Path $hermesHome 'plugins') $pluginName

    if ((Test-Path -LiteralPath $pluginDir) -and (Test-GitCheckout $pluginDir)) {
        Write-Host 'Updating Unfold Hermes Connector...'
        Get-ChildItem -LiteralPath $pluginDir -Force -Recurse -File -ErrorAction SilentlyContinue |
            ForEach-Object { if ($_.IsReadOnly) { $_.IsReadOnly = $false } }
    } else {
        if (Test-Path -LiteralPath $pluginDir) {
            Write-Host 'Repairing an incomplete Connector installation...'
            Move-BrokenConnector $pluginDir $hermesHome
        }
        Write-Host 'Installing Unfold Hermes Connector...'
        Invoke-Checked { hermes plugins install $repository --enable } 'Connector installation failed.'
    }

    Invoke-Checked { git -C $pluginDir remote set-url origin $repository } 'Could not update the Connector repository URL.'
    if ($revision -eq 'origin/main') {
        Invoke-Checked { git -C $pluginDir fetch --prune origin } 'Could not download the Connector update.'
    } else {
        Invoke-Checked { git -C $pluginDir fetch --no-tags origin $revision } 'Could not download the reviewed Connector revision.'
    }
    Invoke-Checked { git -C $pluginDir checkout --force $revision } 'Could not activate the requested Connector revision.'
    Invoke-Checked { hermes plugins enable $pluginName --no-allow-tool-override } 'Connector update succeeded, but enabling it failed.'

    $python = Get-Command py -ErrorAction SilentlyContinue
    if ($python) {
        Invoke-Checked { py -3 (Join-Path $pluginDir 'connect.py') } 'Connector pairing failed.'
    } else {
        Invoke-Checked { python3 (Join-Path $pluginDir 'connect.py') } 'Connector pairing failed.'
    }
    Write-Host 'Unfold Hermes Connector is ready.' -ForegroundColor Green
} finally {
    if ($gatewayStopped) { & hermes gateway restart }
}
