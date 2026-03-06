#Requires -Version 5.1
<#
.SYNOPSIS
    Remove all SKCapstone scheduled tasks from Windows Task Scheduler.

.DESCRIPTION
    Unregisters every task under the \SKCapstone\ folder and removes the
    wrapper batch scripts from ~/.skcapstone/logs/task-wrappers/.

.PARAMETER KeepLogs
    If set, leaves log files intact. By default logs are preserved;
    this switch is a no-op placeholder for symmetry with install.

.EXAMPLE
    .\uninstall-tasks.ps1
#>

[CmdletBinding()]
param(
    [switch]$KeepLogs
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$TaskFolder = '\SKCapstone'
$LogDir     = Join-Path $env:USERPROFILE '.skcapstone\logs'

Write-Host '=== SKCapstone Task Scheduler Removal ===' -ForegroundColor Cyan
Write-Host ''

# ---------------------------------------------------------------------------
# Remove all tasks under \SKCapstone\
# ---------------------------------------------------------------------------
$taskNames = @(
    'SKCapstone-Daemon',
    'SKCapstone-MemoryCompress',
    'SKCapstone-SyncWatcher',
    'SKCapstone-Heartbeat',
    'SKCapstone-Housekeeping'
)

$removed = 0
$notFound = 0

foreach ($name in $taskNames) {
    try {
        $task = Get-ScheduledTask -TaskName $name -TaskPath "$TaskFolder\" -ErrorAction SilentlyContinue
        if ($task) {
            # Stop the task if it is running
            if ($task.State -eq 'Running') {
                Stop-ScheduledTask -TaskName $name -TaskPath "$TaskFolder\" -ErrorAction SilentlyContinue
                Write-Host "  Stopped running task: $name" -ForegroundColor Yellow
            }
            Unregister-ScheduledTask -TaskName $name -TaskPath "$TaskFolder\" -Confirm:$false
            Write-Host "  Removed: $TaskFolder\$name" -ForegroundColor Green
            $removed++
        } else {
            Write-Host "  Not found: $TaskFolder\$name" -ForegroundColor DarkGray
            $notFound++
        }
    } catch {
        Write-Host "  Not found: $TaskFolder\$name" -ForegroundColor DarkGray
        $notFound++
    }
}

# ---------------------------------------------------------------------------
# Try to remove the empty task folder
# ---------------------------------------------------------------------------
try {
    # Check if any tasks remain in the folder
    $remaining = Get-ScheduledTask -TaskPath "$TaskFolder\" -ErrorAction SilentlyContinue
    if (-not $remaining) {
        # The folder auto-removes when the last task is unregistered in most
        # Windows versions, but we log the state for clarity.
        Write-Host ''
        Write-Host "  Task folder $TaskFolder is now empty." -ForegroundColor Green
    }
} catch {
    # Folder already gone
}

# ---------------------------------------------------------------------------
# Remove wrapper scripts
# ---------------------------------------------------------------------------
$wrapperDir = Join-Path $LogDir 'task-wrappers'
if (Test-Path $wrapperDir) {
    $wrappers = Get-ChildItem -Path $wrapperDir -Filter '*.bat' -ErrorAction SilentlyContinue
    if ($wrappers) {
        foreach ($w in $wrappers) {
            Remove-Item $w.FullName -Force
            Write-Host "  Removed wrapper: $($w.Name)" -ForegroundColor Green
        }
        # Remove directory if empty
        $remaining = Get-ChildItem -Path $wrapperDir -ErrorAction SilentlyContinue
        if (-not $remaining) {
            Remove-Item $wrapperDir -Force
            Write-Host "  Removed wrapper directory" -ForegroundColor Green
        }
    }
} else {
    Write-Host "  No wrapper directory found" -ForegroundColor DarkGray
}

# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------
Write-Host ''
Write-Host "=== Done: $removed removed, $notFound not found ===" -ForegroundColor Cyan
Write-Host ''
if ($removed -gt 0) {
    Write-Host 'All SKCapstone scheduled tasks have been removed.' -ForegroundColor Green
    Write-Host "Log files preserved at: $LogDir" -ForegroundColor DarkGray
} else {
    Write-Host 'No SKCapstone tasks were found to remove.' -ForegroundColor Yellow
}
