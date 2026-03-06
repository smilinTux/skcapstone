#Requires -Version 5.1
<#
.SYNOPSIS
    Register Windows Task Scheduler tasks for SKCapstone services.

.DESCRIPTION
    Creates scheduled tasks equivalent to the Linux systemd services and timers:

      - SKCapstone Daemon        (runs on user logon, restarts on failure)
      - Memory Rehydration       (weekly memory compression via skcapstone memory compress)
      - Sync Watcher             (every 2 minutes, polls sync inbox)
      - Health Monitor Heartbeat (every 60 seconds, emits heartbeat + queue drain)
      - Housekeeping             (daily log rotation and temp cleanup)

    All tasks run under the current user, use the skenv Python, and log to
    ~/.skcapstone/logs/.

.PARAMETER SkenvPath
    Path to the skenv virtualenv. Defaults to $env:LOCALAPPDATA\skenv.
    Falls back to $env:USERPROFILE\.skenv if the LOCALAPPDATA path does not exist.

.PARAMETER Force
    Remove existing SKCapstone tasks before re-registering them.

.EXAMPLE
    .\install-tasks.ps1
    .\install-tasks.ps1 -Force
    .\install-tasks.ps1 -SkenvPath C:\Users\me\.skenv
#>

[CmdletBinding()]
param(
    [string]$SkenvPath,
    [switch]$Force
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
$TaskFolder  = '\SKCapstone'
$LogDir      = Join-Path $env:USERPROFILE '.skcapstone\logs'

# Resolve skenv
if (-not $SkenvPath) {
    $candidate1 = Join-Path $env:LOCALAPPDATA 'skenv'
    $candidate2 = Join-Path $env:USERPROFILE '.skenv'
    if (Test-Path $candidate1) {
        $SkenvPath = $candidate1
    } elseif (Test-Path $candidate2) {
        $SkenvPath = $candidate2
    } else {
        Write-Host "ERROR: Cannot find skenv virtualenv at:" -ForegroundColor Red
        Write-Host "  $candidate1" -ForegroundColor Red
        Write-Host "  $candidate2" -ForegroundColor Red
        Write-Host "Run scripts\install.ps1 first, or pass -SkenvPath." -ForegroundColor Red
        exit 1
    }
}

$PythonExe      = Join-Path $SkenvPath 'Scripts\python.exe'
$SKCapstoneExe  = Join-Path $SkenvPath 'Scripts\skcapstone.exe'
$SKCommExe      = Join-Path $SkenvPath 'Scripts\skcomm.exe'

if (-not (Test-Path $PythonExe)) {
    Write-Host "ERROR: Python not found at $PythonExe" -ForegroundColor Red
    exit 1
}

# Ensure log directory exists
if (-not (Test-Path $LogDir)) {
    New-Item -ItemType Directory -Path $LogDir -Force | Out-Null
}

Write-Host '=== SKCapstone Task Scheduler Setup ===' -ForegroundColor Cyan
Write-Host "  skenv:    $SkenvPath"
Write-Host "  logs:     $LogDir"
Write-Host ''

# ---------------------------------------------------------------------------
# Helper: Remove existing task if -Force or if it already exists
# ---------------------------------------------------------------------------
function Remove-ExistingTask {
    param([string]$TaskName)
    $fullPath = "$TaskFolder\$TaskName"
    try {
        $existing = Get-ScheduledTask -TaskName $TaskName -TaskPath "$TaskFolder\" -ErrorAction SilentlyContinue
        if ($existing) {
            if ($Force) {
                Unregister-ScheduledTask -TaskName $TaskName -TaskPath "$TaskFolder\" -Confirm:$false
                Write-Host "  Removed existing task: $fullPath" -ForegroundColor Yellow
            } else {
                Write-Host "  SKIP: $fullPath already exists (use -Force to replace)" -ForegroundColor Yellow
                return $false
            }
        }
    } catch {
        # Task does not exist, which is fine
    }
    return $true
}

# ---------------------------------------------------------------------------
# Helper: Build a wrapper batch script that logs output
# ---------------------------------------------------------------------------
function New-WrapperScript {
    param(
        [string]$Name,
        [string]$Command,
        [string]$Arguments
    )
    $wrapperDir = Join-Path $LogDir 'task-wrappers'
    if (-not (Test-Path $wrapperDir)) {
        New-Item -ItemType Directory -Path $wrapperDir -Force | Out-Null
    }
    $wrapperPath = Join-Path $wrapperDir "$Name.bat"
    $logFile = Join-Path $LogDir "$Name.log"
    $content = @"
@echo off
echo [%date% %time%] === $Name started === >> "$logFile"
"$Command" $Arguments >> "$logFile" 2>&1
echo [%date% %time%] === $Name finished (exit code: %ERRORLEVEL%) === >> "$logFile"
"@
    Set-Content -Path $wrapperPath -Value $content -Encoding ASCII
    return $wrapperPath
}

$registered = 0
$skipped    = 0

# ---------------------------------------------------------------------------
# Task 1: SKCapstone Daemon (equivalent to skcapstone.service)
#   Trigger: At user logon
#   Action:  skcapstone daemon start --foreground
#   Restart: Re-launch on failure (via repetition interval)
# ---------------------------------------------------------------------------
Write-Host '[1/5] SKCapstone Daemon...' -ForegroundColor Green

if (Remove-ExistingTask 'SKCapstone-Daemon') {
    $wrapper = New-WrapperScript -Name 'skcapstone-daemon' `
        -Command $SKCapstoneExe `
        -Arguments 'daemon start --foreground'

    $action  = New-ScheduledTaskAction -Execute 'cmd.exe' `
        -Argument "/c `"$wrapper`""

    $trigger = New-ScheduledTaskTrigger -AtLogOn
    $trigger.Delay = 'PT15S'  # 15-second delay after logon

    $settings = New-ScheduledTaskSettingsSet `
        -AllowStartIfOnBatteries `
        -DontStopIfGoingOnBatteries `
        -ExecutionTimeLimit ([TimeSpan]::Zero) `
        -RestartCount 3 `
        -RestartInterval (New-TimeSpan -Seconds 30) `
        -StartWhenAvailable `
        -MultipleInstances IgnoreNew

    $principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType Interactive -RunLevel Limited

    Register-ScheduledTask -TaskName 'SKCapstone-Daemon' `
        -TaskPath $TaskFolder `
        -Action $action `
        -Trigger $trigger `
        -Settings $settings `
        -Principal $principal `
        -Description 'SKCapstone Sovereign Agent Daemon — equivalent to skcapstone.service' | Out-Null

    Write-Host '  Registered: At logon, auto-restart on failure'
    $registered++
} else {
    $skipped++
}

# ---------------------------------------------------------------------------
# Task 2: Memory Rehydration / Compression (equivalent to skcapstone-memory-compress.timer)
#   Trigger: Weekly (Sunday 03:00)
#   Action:  skcapstone memory compress
# ---------------------------------------------------------------------------
Write-Host '[2/5] Memory Rehydration (weekly)...' -ForegroundColor Green

if (Remove-ExistingTask 'SKCapstone-MemoryCompress') {
    $wrapper = New-WrapperScript -Name 'skcapstone-memory-compress' `
        -Command $SKCapstoneExe `
        -Arguments 'memory compress'

    $action  = New-ScheduledTaskAction -Execute 'cmd.exe' `
        -Argument "/c `"$wrapper`""

    $trigger = New-ScheduledTaskTrigger -Weekly -DaysOfWeek Sunday -At '3:00AM'
    # Add 1-hour random delay equivalent to RandomizedDelaySec=1h
    $trigger.RandomDelay = 'PT1H'

    $settings = New-ScheduledTaskSettingsSet `
        -AllowStartIfOnBatteries `
        -DontStopIfGoingOnBatteries `
        -ExecutionTimeLimit (New-TimeSpan -Hours 2) `
        -StartWhenAvailable `
        -MultipleInstances IgnoreNew

    $principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType Interactive -RunLevel Limited

    Register-ScheduledTask -TaskName 'SKCapstone-MemoryCompress' `
        -TaskPath $TaskFolder `
        -Action $action `
        -Trigger $trigger `
        -Settings $settings `
        -Principal $principal `
        -Description 'Weekly LLM memory compression — equivalent to skcapstone-memory-compress.timer' | Out-Null

    Write-Host '  Registered: Weekly (Sunday 03:00, +1h jitter)'
    $registered++
} else {
    $skipped++
}

# ---------------------------------------------------------------------------
# Task 3: Sync Watcher (poll sync inbox every 2 minutes)
#   Equivalent to the sync_inbox_scan scheduled task (30s in-process, but
#   the external fallback polls less aggressively)
# ---------------------------------------------------------------------------
Write-Host '[3/5] Sync Watcher (every 2 min)...' -ForegroundColor Green

if (Remove-ExistingTask 'SKCapstone-SyncWatcher') {
    $wrapper = New-WrapperScript -Name 'skcapstone-sync-watcher' `
        -Command $PythonExe `
        -Arguments "-m skcapstone.cli sync poll"

    $action  = New-ScheduledTaskAction -Execute 'cmd.exe' `
        -Argument "/c `"$wrapper`""

    # Trigger: repeating every 2 minutes, starting at logon
    $trigger = New-ScheduledTaskTrigger -AtLogOn
    $trigger.Delay = 'PT30S'
    $trigger.Repetition = (New-ScheduledTaskTrigger -Once -At '00:00' `
        -RepetitionInterval (New-TimeSpan -Minutes 2)).Repetition

    $settings = New-ScheduledTaskSettingsSet `
        -AllowStartIfOnBatteries `
        -DontStopIfGoingOnBatteries `
        -ExecutionTimeLimit (New-TimeSpan -Minutes 5) `
        -StartWhenAvailable `
        -MultipleInstances IgnoreNew

    $principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType Interactive -RunLevel Limited

    Register-ScheduledTask -TaskName 'SKCapstone-SyncWatcher' `
        -TaskPath $TaskFolder `
        -Action $action `
        -Trigger $trigger `
        -Settings $settings `
        -Principal $principal `
        -Description 'Sync inbox poller — fallback for filesystem watcher' | Out-Null

    Write-Host '  Registered: Every 2 minutes after logon'
    $registered++
} else {
    $skipped++
}

# ---------------------------------------------------------------------------
# Task 4: Health Monitor / Heartbeat (equivalent to skcomm-heartbeat.timer
#         and skcomm-queue-drain.timer combined)
#   Trigger: Every 60 seconds after logon
#   Action:  skcomm heartbeat && skcomm queue drain
# ---------------------------------------------------------------------------
Write-Host '[4/5] Health Monitor Heartbeat (every 60s)...' -ForegroundColor Green

if (Remove-ExistingTask 'SKCapstone-Heartbeat') {
    $wrapperDir = Join-Path $LogDir 'task-wrappers'
    if (-not (Test-Path $wrapperDir)) {
        New-Item -ItemType Directory -Path $wrapperDir -Force | Out-Null
    }
    $wrapperPath = Join-Path $wrapperDir 'skcapstone-heartbeat.bat'
    $logFile = Join-Path $LogDir 'skcapstone-heartbeat.log'
    # Combined heartbeat + queue drain (mirrors the two systemd timers)
    $content = @"
@echo off
echo [%date% %time%] === heartbeat started === >> "$logFile"
"$SKCommExe" heartbeat >> "$logFile" 2>&1
echo [%date% %time%] heartbeat exit: %ERRORLEVEL% >> "$logFile"
"$SKCommExe" queue drain >> "$logFile" 2>&1
echo [%date% %time%] queue-drain exit: %ERRORLEVEL% >> "$logFile"
echo [%date% %time%] === heartbeat finished === >> "$logFile"
"@
    Set-Content -Path $wrapperPath -Value $content -Encoding ASCII

    $action  = New-ScheduledTaskAction -Execute 'cmd.exe' `
        -Argument "/c `"$wrapperPath`""

    $trigger = New-ScheduledTaskTrigger -AtLogOn
    $trigger.Delay = 'PT30S'
    $trigger.Repetition = (New-ScheduledTaskTrigger -Once -At '00:00' `
        -RepetitionInterval (New-TimeSpan -Minutes 1)).Repetition

    $settings = New-ScheduledTaskSettingsSet `
        -AllowStartIfOnBatteries `
        -DontStopIfGoingOnBatteries `
        -ExecutionTimeLimit (New-TimeSpan -Minutes 2) `
        -StartWhenAvailable `
        -MultipleInstances IgnoreNew

    $principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType Interactive -RunLevel Limited

    Register-ScheduledTask -TaskName 'SKCapstone-Heartbeat' `
        -TaskPath $TaskFolder `
        -Action $action `
        -Trigger $trigger `
        -Settings $settings `
        -Principal $principal `
        -Description 'Heartbeat + queue drain — equivalent to skcomm-heartbeat.timer + skcomm-queue-drain.timer' | Out-Null

    Write-Host '  Registered: Every 60 seconds after logon'
    $registered++
} else {
    $skipped++
}

# ---------------------------------------------------------------------------
# Task 5: Housekeeping (log rotation + temp cleanup)
#   Trigger: Daily at 04:00
#   Action:  Rotate logs, purge old temp files
# ---------------------------------------------------------------------------
Write-Host '[5/5] Housekeeping (daily)...' -ForegroundColor Green

if (Remove-ExistingTask 'SKCapstone-Housekeeping') {
    $wrapperDir = Join-Path $LogDir 'task-wrappers'
    if (-not (Test-Path $wrapperDir)) {
        New-Item -ItemType Directory -Path $wrapperDir -Force | Out-Null
    }
    $wrapperPath = Join-Path $wrapperDir 'skcapstone-housekeeping.bat'
    $logFile = Join-Path $LogDir 'skcapstone-housekeeping.log'
    $skHome  = Join-Path $env:USERPROFILE '.skcapstone'
    $content = @"
@echo off
echo [%date% %time%] === housekeeping started === >> "$logFile"

REM --- Log rotation: compress logs older than 7 days ---
echo Rotating logs... >> "$logFile"
forfiles /P "$LogDir" /M *.log /D -7 /C "cmd /c if @fsize GTR 0 (echo Archiving @file >> \"$logFile\" & copy @path @path.bak >nul & type nul > @path)" 2>nul

REM --- Purge archived logs older than 30 days ---
echo Purging old archives... >> "$logFile"
forfiles /P "$LogDir" /M *.bak /D -30 /C "cmd /c echo Deleting @file >> \"$logFile\" & del @path" 2>nul

REM --- Clean temp/cache files ---
echo Cleaning temp files... >> "$logFile"
if exist "$skHome\tmp" (
    forfiles /P "$skHome\tmp" /D -3 /C "cmd /c echo Deleting @file >> \"$logFile\" & del @path" 2>nul
)
if exist "$skHome\cache" (
    forfiles /P "$skHome\cache" /D -14 /C "cmd /c echo Deleting @file >> \"$logFile\" & del @path" 2>nul
)

echo [%date% %time%] === housekeeping finished === >> "$logFile"
"@
    Set-Content -Path $wrapperPath -Value $content -Encoding ASCII

    $action  = New-ScheduledTaskAction -Execute 'cmd.exe' `
        -Argument "/c `"$wrapperPath`""

    $trigger = New-ScheduledTaskTrigger -Daily -At '4:00AM'
    $trigger.RandomDelay = 'PT30M'

    $settings = New-ScheduledTaskSettingsSet `
        -AllowStartIfOnBatteries `
        -DontStopIfGoingOnBatteries `
        -ExecutionTimeLimit (New-TimeSpan -Minutes 30) `
        -StartWhenAvailable `
        -MultipleInstances IgnoreNew

    $principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType Interactive -RunLevel Limited

    Register-ScheduledTask -TaskName 'SKCapstone-Housekeeping' `
        -TaskPath $TaskFolder `
        -Action $action `
        -Trigger $trigger `
        -Settings $settings `
        -Principal $principal `
        -Description 'Daily log rotation + temp file cleanup' | Out-Null

    Write-Host '  Registered: Daily at 04:00 (+30min jitter)'
    $registered++
} else {
    $skipped++
}

# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------
Write-Host ''
Write-Host "=== Done: $registered registered, $skipped skipped ===" -ForegroundColor Cyan
Write-Host ''
Write-Host 'Registered tasks (view with: Get-ScheduledTask -TaskPath \SKCapstone\):' -ForegroundColor Green
Write-Host '  \SKCapstone\SKCapstone-Daemon          — on logon, auto-restart'
Write-Host '  \SKCapstone\SKCapstone-MemoryCompress   — weekly (Sun 03:00)'
Write-Host '  \SKCapstone\SKCapstone-SyncWatcher      — every 2 min'
Write-Host '  \SKCapstone\SKCapstone-Heartbeat        — every 60s'
Write-Host '  \SKCapstone\SKCapstone-Housekeeping     — daily (04:00)'
Write-Host ''
Write-Host "Logs:     $LogDir"
Write-Host "Wrappers: $LogDir\task-wrappers\"
Write-Host ''
Write-Host 'To remove all tasks: .\uninstall-tasks.ps1' -ForegroundColor Yellow
