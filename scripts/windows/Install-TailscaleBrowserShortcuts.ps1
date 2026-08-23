param(
    [Parameter(Mandatory = $true)]
    [string]$ScriptDirectory
)

$ErrorActionPreference = "Stop"
$desktop = [Environment]::GetFolderPath("CommonDesktopDirectory")
$shell = New-Object -ComObject WScript.Shell

function New-TailnetShortcut {
    param([string]$Name, [string]$Script)
    $shortcut = $shell.CreateShortcut((Join-Path $desktop "$Name.lnk"))
    $shortcut.TargetPath = "$env:SystemRoot\System32\WindowsPowerShell\v1.0\powershell.exe"
    $shortcut.Arguments = "-NoProfile -ExecutionPolicy Bypass -File `"$(Join-Path $ScriptDirectory $Script)`""
    $shortcut.WorkingDirectory = $ScriptDirectory
    $shortcut.IconLocation = "$env:SystemRoot\System32\SHELL32.dll,18"
    $shortcut.Description = "$Name through the canonical WSL Tailscale identity"
    $shortcut.Save()
}

New-TailnetShortcut -Name "Enable Tailscale Browser Access" -Script "Enable-TailscaleBrowserAccess.ps1"
New-TailnetShortcut -Name "Disable Tailscale Browser Access" -Script "Disable-TailscaleBrowserAccess.ps1"

Write-Host "Installed shared desktop controls in $desktop"
