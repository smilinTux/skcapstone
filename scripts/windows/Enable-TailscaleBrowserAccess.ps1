$ErrorActionPreference = "Stop"

$service = "skcapstone-tailnet-browser-proxy.service"
$pacUrl = "http://127.0.0.1:1056/proxy.pac"
$internetKey = "HKCU:\Software\Microsoft\Windows\CurrentVersion\Internet Settings"
$stateKey = "HKCU:\Software\SKCapstone\TailnetBrowserProxy"

& wsl.exe -u root -e systemctl start $service
if ($LASTEXITCODE -ne 0) {
    throw "WSL proxy service failed to start"
}

$healthy = $false
for ($attempt = 0; $attempt -lt 20; $attempt++) {
    try {
        $response = Invoke-RestMethod -Uri "http://127.0.0.1:1056/healthz" -TimeoutSec 2
        if ($response.ok) {
            $healthy = $true
            break
        }
    } catch {
        Start-Sleep -Milliseconds 500
    }
}
if (-not $healthy) {
    throw "WSL proxy health check did not become ready"
}

New-Item -Path $stateKey -Force | Out-Null
$active = (Get-ItemProperty -Path $stateKey -Name Active -ErrorAction SilentlyContinue).Active
if ($active -ne 1) {
    $current = Get-ItemProperty -Path $internetKey
    $names = $current.PSObject.Properties.Name
    $hadPac = $names -contains "AutoConfigURL"
    $hadProxyEnable = $names -contains "ProxyEnable"
    $hadAutoDetect = $names -contains "AutoDetect"
    New-ItemProperty -Path $stateKey -Name HadAutoConfigURL -Value ([int]$hadPac) -PropertyType DWord -Force | Out-Null
    if ($hadPac) {
        New-ItemProperty -Path $stateKey -Name PreviousAutoConfigURL -Value $current.AutoConfigURL -PropertyType String -Force | Out-Null
    }
    New-ItemProperty -Path $stateKey -Name HadProxyEnable -Value ([int]$hadProxyEnable) -PropertyType DWord -Force | Out-Null
    if ($hadProxyEnable) {
        New-ItemProperty -Path $stateKey -Name PreviousProxyEnable -Value ([int]$current.ProxyEnable) -PropertyType DWord -Force | Out-Null
    }
    New-ItemProperty -Path $stateKey -Name HadAutoDetect -Value ([int]$hadAutoDetect) -PropertyType DWord -Force | Out-Null
    if ($hadAutoDetect) {
        New-ItemProperty -Path $stateKey -Name PreviousAutoDetect -Value ([int]$current.AutoDetect) -PropertyType DWord -Force | Out-Null
    }
}

New-ItemProperty -Path $internetKey -Name AutoConfigURL -Value $pacUrl -PropertyType String -Force | Out-Null
New-ItemProperty -Path $internetKey -Name ProxyEnable -Value 0 -PropertyType DWord -Force | Out-Null
New-ItemProperty -Path $internetKey -Name AutoDetect -Value 0 -PropertyType DWord -Force | Out-Null
New-ItemProperty -Path $stateKey -Name Active -Value 1 -PropertyType DWord -Force | Out-Null

Add-Type @"
using System;
using System.Runtime.InteropServices;
public class SKInternetSettings {
    [DllImport("wininet.dll", SetLastError = true)]
    public static extern bool InternetSetOption(IntPtr h, int option, IntPtr buffer, int length);
}
"@
[SKInternetSettings]::InternetSetOption([IntPtr]::Zero, 39, [IntPtr]::Zero, 0) | Out-Null
[SKInternetSettings]::InternetSetOption([IntPtr]::Zero, 37, [IntPtr]::Zero, 0) | Out-Null

Write-Host "Tailscale browser access enabled for $env:USERNAME."
Write-Host "Only tailnet destinations use the WSL proxy."
