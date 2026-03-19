#Requires -Version 5.1
<#
.SYNOPSIS
    Sovereign Agent Suite Installer for Windows.

.DESCRIPTION
    Installs all SK* packages into a dedicated virtualenv at
    $env:LOCALAPPDATA\skenv.  This keeps the system Python clean.

.PARAMETER Dev
    Include dev/test tools (pytest, ruff, black, pytest-cov).

.PARAMETER Force
    Remove and recreate the virtualenv from scratch.

.EXAMPLE
    .\install.ps1
    .\install.ps1 -Dev
    .\install.ps1 -Force -Dev
#>

[CmdletBinding()]
param(
    [switch]$Dev,
    [switch]$Force
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$SKENV = Join-Path $env:LOCALAPPDATA 'skenv'
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Definition
$RepoRoot = Split-Path -Parent $ScriptDir

Write-Host '=== Sovereign Agent Suite Installer (Windows) ===' -ForegroundColor Cyan
Write-Host ''

# ---------------------------------------------------------------------------
# Step 1: Check prerequisites — Python 3.10+
# ---------------------------------------------------------------------------
$Python = $null
foreach ($candidate in @('python3', 'python')) {
    try {
        $verOutput = & $candidate -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')" 2>$null
        if ($LASTEXITCODE -eq 0 -and $verOutput) {
            $parts = $verOutput.Split('.')
            $major = [int]$parts[0]
            $minor = [int]$parts[1]
            if ($major -ge 3 -and $minor -ge 10) {
                $Python = $candidate
                break
            }
        }
    } catch {
        # candidate not found, try next
    }
}

if (-not $Python) {
    Write-Host 'ERROR: Python 3.10+ required. Found none on PATH.' -ForegroundColor Red
    Write-Host 'Download from https://www.python.org/downloads/' -ForegroundColor Red
    exit 1
}

$pyVersion = & $Python --version 2>&1
Write-Host "[1/6] Using $Python ($pyVersion)" -ForegroundColor Green

# ---------------------------------------------------------------------------
# Step 2: Create virtualenv
# ---------------------------------------------------------------------------
if ($Force -and (Test-Path $SKENV)) {
    Write-Host '[2/6] Removing existing venv (-Force)...' -ForegroundColor Yellow
    Remove-Item -Recurse -Force $SKENV
}

if (-not (Test-Path $SKENV)) {
    Write-Host "[2/6] Creating virtualenv at $SKENV..."
    & $Python -m venv $SKENV
    if ($LASTEXITCODE -ne 0) {
        Write-Host 'ERROR: Failed to create virtualenv.' -ForegroundColor Red
        exit 1
    }
} else {
    Write-Host "[2/6] Virtualenv exists at $SKENV"
}

$Pip = Join-Path $SKENV 'Scripts\pip.exe'
& $Pip install --upgrade pip -q 2>$null | Out-Null

# ---------------------------------------------------------------------------
# Step 3: Install SK* packages
# ---------------------------------------------------------------------------
Write-Host '[3/6] Installing SK* packages...'

$ParentDir = Split-Path -Parent $RepoRoot
$PillarDir = Join-Path $ParentDir 'pillar-repos'

function Install-Pkg {
    <#
    .SYNOPSIS
        Install a package editable from local paths, falling back to PyPI.
    #>
    param(
        [string]$Name,
        [string]$Extras,
        [string[]]$Paths
    )

    foreach ($path in $Paths) {
        if (Test-Path $path) {
            if ($Extras) {
                & $Pip install -e "${path}[$Extras]" -q 2>$null | Out-Null
                if ($LASTEXITCODE -eq 0) {
                    Write-Host "  $Name (editable: $path)"
                    return
                }
                # Retry without extras
                & $Pip install -e $path -q 2>$null | Out-Null
                if ($LASTEXITCODE -eq 0) {
                    Write-Host "  $Name (editable, no extras: $path)"
                    return
                }
            } else {
                & $Pip install -e $path -q 2>$null | Out-Null
                if ($LASTEXITCODE -eq 0) {
                    Write-Host "  $Name (editable: $path)"
                    return
                }
            }
        }
    }

    # Fall back to PyPI
    if ($Extras) {
        & $Pip install "${Name}[$Extras]" -q 2>$null | Out-Null
        if ($LASTEXITCODE -eq 0) {
            Write-Host "  $Name (PyPI)"
            return
        }
        & $Pip install $Name -q 2>$null | Out-Null
        if ($LASTEXITCODE -eq 0) {
            Write-Host "  $Name (PyPI, no extras)"
            return
        }
    } else {
        & $Pip install $Name -q 2>$null | Out-Null
        if ($LASTEXITCODE -eq 0) {
            Write-Host "  $Name (PyPI)"
            return
        }
    }

    Write-Host "  $Name (FAILED -- skipping)" -ForegroundColor Red
}

# Core packages (in dependency order)
Install-Pkg -Name 'capauth'          -Extras 'all'                      -Paths @((Join-Path $PillarDir 'capauth'), (Join-Path $ParentDir 'capauth'))
Install-Pkg -Name 'cloud9-protocol'  -Extras ''                         -Paths @((Join-Path $PillarDir 'cloud9'), (Join-Path $ParentDir 'cloud9'))
Install-Pkg -Name 'skmemory'         -Extras ''                         -Paths @((Join-Path $PillarDir 'skmemory'), (Join-Path $ParentDir 'skmemory'))
Install-Pkg -Name 'skcomm'           -Extras 'cli,crypto,discovery,api' -Paths @((Join-Path $PillarDir 'skcomm'), (Join-Path $ParentDir 'skcomm'))
Install-Pkg -Name 'skcapstone'       -Extras ''                         -Paths @($RepoRoot)
Install-Pkg -Name 'skchat-sovereign' -Extras 'all'                      -Paths @((Join-Path $ParentDir 'skchat'))
Install-Pkg -Name 'skseal'           -Extras ''                         -Paths @((Join-Path $ParentDir 'skseal'))
Install-Pkg -Name 'skskills'         -Extras ''                         -Paths @((Join-Path $ParentDir 'skskills'))
Install-Pkg -Name 'sksecurity'       -Extras ''                         -Paths @((Join-Path $PillarDir 'sksecurity'), (Join-Path $PillarDir 'SKSecurity'), (Join-Path $ParentDir 'sksecurity'), (Join-Path $ParentDir 'SKSecurity'))
Install-Pkg -Name 'skseed'           -Extras ''                         -Paths @((Join-Path $PillarDir 'skseed'), (Join-Path $ParentDir 'skseed'))

# ---------------------------------------------------------------------------
# Step 4: Dev tools (optional)
# ---------------------------------------------------------------------------
if ($Dev) {
    Write-Host '[4/6] Installing dev tools...'
    & $Pip install pytest pytest-cov ruff black -q 2>$null | Out-Null
    Write-Host '  pytest, pytest-cov, ruff, black'
} else {
    Write-Host '[4/6] Skipping dev tools (use -Dev to include)'
}

# ---------------------------------------------------------------------------
# Step 5: Register skills & MCP servers
# ---------------------------------------------------------------------------
Write-Host '[5/6] Registering skills and MCP servers...'
$skcapstoneExe = Join-Path $SKENV 'Scripts\skcapstone.exe'
if (Test-Path $skcapstoneExe) {
    try {
        & $skcapstoneExe register 2>$null | Out-Null
        if ($LASTEXITCODE -eq 0) {
            Write-Host '  Registration complete'
        } else {
            Write-Host '  (registration skipped -- run "skcapstone register" manually)'
        }
    } catch {
        Write-Host '  (registration skipped -- run "skcapstone register" manually)'
    }
} else {
    Write-Host '  (skcapstone not found -- registration skipped)'
}

# ---------------------------------------------------------------------------
# Step 6: Verify installation & PATH setup
# ---------------------------------------------------------------------------
Write-Host '[6/6] Verifying installation...'

$ScriptsDir = Join-Path $SKENV 'Scripts'
$failures = 0
foreach ($cmd in @('capauth', 'skmemory', 'skcapstone', 'skcomm')) {
    $exe = Join-Path $ScriptsDir "$cmd.exe"
    if (Test-Path $exe) {
        try {
            & $exe --version 2>$null | Out-Null
            if ($LASTEXITCODE -eq 0) {
                Write-Host "  $cmd OK" -ForegroundColor Green
            } else {
                Write-Host "  $cmd FAILED" -ForegroundColor Yellow
                $failures++
            }
        } catch {
            Write-Host "  $cmd FAILED" -ForegroundColor Yellow
            $failures++
        }
    } else {
        Write-Host "  $cmd not found" -ForegroundColor Yellow
        $failures++
    }
}

Write-Host ''

# Add Scripts dir to user PATH if not present
$userPath = [Environment]::GetEnvironmentVariable('PATH', 'User')
if ($userPath -and $userPath.Split(';') -contains $ScriptsDir) {
    Write-Host "PATH already includes $ScriptsDir"
} else {
    Write-Host "Adding $ScriptsDir to user PATH..." -ForegroundColor Yellow
    if ($userPath) {
        $newPath = "$ScriptsDir;$userPath"
    } else {
        $newPath = $ScriptsDir
    }
    [Environment]::SetEnvironmentVariable('PATH', $newPath, 'User')
    # Also update current session
    $env:PATH = "$ScriptsDir;$env:PATH"
    Write-Host "  Added to user PATH. Restart your terminal for it to take effect."
}

Write-Host ''
if ($failures -eq 0) {
    Write-Host '=== Installation complete ===' -ForegroundColor Green
} else {
    Write-Host "=== Installation complete with $failures warning(s) ===" -ForegroundColor Yellow
}
Write-Host ''
Write-Host "Commands available: skcomm, skcapstone, capauth, skchat, skseal, skmemory, skskills, sksecurity, skseed"
Write-Host "Venv location:     $SKENV"
Write-Host "To activate:       & $SKENV\Scripts\Activate.ps1"
