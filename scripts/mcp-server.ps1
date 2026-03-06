#Requires -Version 5.1
<#
.SYNOPSIS
    SKCapstone MCP server launcher for Windows (PowerShell).
    Task: e5f81637

.DESCRIPTION
    Auto-detects the Python virtualenv and launches the MCP server
    on stdio transport. Works with Cursor, Claude Desktop, Claude
    Code CLI, Windsurf, Aider, Cline, or any stdio MCP client.

.PARAMETER VenvPath
    Override the virtualenv path (instead of auto-detection).

.EXAMPLE
    .\scripts\mcp-server.ps1
    .\scripts\mcp-server.ps1 -VenvPath C:\Users\me\.skenv

.NOTES
    Environment overrides:
      SKCAPSTONE_VENV  - force a specific virtualenv
      SKMEMORY_HOME    - override memory storage location
#>

[CmdletBinding()]
param(
    [string]$VenvPath
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Definition
$SkcapstoneDir = Split-Path -Parent $ScriptDir

# --- Locate the Python interpreter ---
function Find-Python {
    # 1. Explicit parameter or env var
    $ExplicitVenv = if ($VenvPath) { $VenvPath } else { $env:SKCAPSTONE_VENV }
    if ($ExplicitVenv) {
        $py = Join-Path $ExplicitVenv 'Scripts\python.exe'
        if (Test-Path $py) { return $py }
        $py = Join-Path $ExplicitVenv 'bin\python'
        if (Test-Path $py) { return $py }
        Write-Warning "SKCAPSTONE_VENV=$ExplicitVenv set but python not found there, falling back."
    }

    # 2. Standard skenv location on Windows
    $SkenvWin = Join-Path $env:LOCALAPPDATA 'skenv\Scripts\python.exe'
    if (Test-Path $SkenvWin) {
        $check = & $SkenvWin -c "import skcapstone" 2>&1
        if ($LASTEXITCODE -eq 0) { return $SkenvWin }
    }

    # 3. Standard skenv location (Unix-style, e.g. WSL path in Windows)
    $SkenvUnix = Join-Path $HOME '.skenv\bin\python'
    if (Test-Path $SkenvUnix) {
        $check = & $SkenvUnix -c "import skcapstone" 2>&1
        if ($LASTEXITCODE -eq 0) { return $SkenvUnix }
    }

    # 4. Project-local .venv
    $LocalVenv = Join-Path $SkcapstoneDir '.venv\Scripts\python.exe'
    if (Test-Path $LocalVenv) {
        $check = & $LocalVenv -c "import skcapstone" 2>&1
        if ($LASTEXITCODE -eq 0) { return $LocalVenv }
    }

    # 5. System Python
    foreach ($cmd in @('python', 'python3')) {
        $sysPath = Get-Command $cmd -ErrorAction SilentlyContinue
        if ($sysPath) {
            $check = & $sysPath.Source -c "import skcapstone" 2>&1
            if ($LASTEXITCODE -eq 0) { return $sysPath.Source }
        }
    }

    return $null
}

$Python = Find-Python
if (-not $Python) {
    Write-Error @"
Could not find a Python interpreter with skcapstone installed.

Install with:
  .\scripts\install.ps1

Or point to an existing venv:
  `$env:SKCAPSTONE_VENV = 'C:\path\to\venv'
  .\scripts\mcp-server.ps1
"@
    exit 1
}

Write-Verbose "Using Python: $Python"

# --- Set environment variables ---
if (-not $env:SKMEMORY_HOME) {
    $env:SKMEMORY_HOME = Join-Path $HOME '.skcapstone\memory'
}
if (-not $env:SKCAPSTONE_HOME) {
    $env:SKCAPSTONE_HOME = Join-Path $HOME '.skcapstone'
}

# Ensure skcapstone is importable
$SrcPath = Join-Path $SkcapstoneDir 'src'
if ($env:PYTHONPATH) {
    $env:PYTHONPATH = "$SrcPath;$($env:PYTHONPATH)"
} else {
    $env:PYTHONPATH = $SrcPath
}

# --- Launch MCP server on stdio ---
& $Python -m skcapstone.mcp_server @args
exit $LASTEXITCODE
