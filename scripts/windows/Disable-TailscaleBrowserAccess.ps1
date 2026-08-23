$ErrorActionPreference = "Stop"

$internetKey = "HKCU:\Software\Microsoft\Windows\CurrentVersion\Internet Settings"
$stateKey = "HKCU:\Software\SKCapstone\TailnetBrowserProxy"
$state = Get-ItemProperty -Path $stateKey -ErrorAction SilentlyContinue

if ($null -ne $state -and $state.Active -eq 1) {
    if ($state.HadAutoConfigURL -eq 1) {
        New-ItemProperty -Path $internetKey -Name AutoConfigURL -Value $state.PreviousAutoConfigURL -PropertyType String -Force | Out-Null
    } else {
        Remove-ItemProperty -Path $internetKey -Name AutoConfigURL -ErrorAction SilentlyContinue
    }
    if ($state.HadProxyEnable -eq 1) {
        New-ItemProperty -Path $internetKey -Name ProxyEnable -Value ([int]$state.PreviousProxyEnable) -PropertyType DWord -Force | Out-Null
    } else {
        Remove-ItemProperty -Path $internetKey -Name ProxyEnable -ErrorAction SilentlyContinue
    }
    if ($state.HadAutoDetect -eq 1) {
        New-ItemProperty -Path $internetKey -Name AutoDetect -Value ([int]$state.PreviousAutoDetect) -PropertyType DWord -Force | Out-Null
    } else {
        Remove-ItemProperty -Path $internetKey -Name AutoDetect -ErrorAction SilentlyContinue
    }
    New-ItemProperty -Path $stateKey -Name Active -Value 0 -PropertyType DWord -Force | Out-Null
}

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

Write-Host "Tailscale browser access disabled for $env:USERNAME."
Write-Host "The shared WSL proxy service remains available to other Windows profiles."
