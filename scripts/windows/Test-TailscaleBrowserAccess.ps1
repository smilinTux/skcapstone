param(
    [Parameter(Mandatory = $true)]
    [string]$TestUrl,
    [Parameter(Mandatory = $true)]
    [string]$ExpectedText,
    [string]$BrowserPath = "C:\Program Files\Google\Chrome\Application\chrome.exe"
)

$ErrorActionPreference = "Stop"
if (-not (Test-Path $BrowserPath)) {
    throw "Browser not found: $BrowserPath"
}

$profile = Join-Path $env:TEMP ("skcapstone-tailnet-proxy-" + [Guid]::NewGuid().ToString("N"))
$debugPort = Get-Random -Minimum 19000 -Maximum 19999
$arguments = @(
    "--headless=new",
    "--disable-gpu",
    "--no-first-run",
    "--user-data-dir=$profile",
    "--proxy-server=http://127.0.0.1:1055",
    "--remote-debugging-port=$debugPort",
    $TestUrl
)
$process = Start-Process -FilePath $BrowserPath -ArgumentList $arguments -PassThru

try {
    $matched = $false
    for ($attempt = 0; $attempt -lt 30; $attempt++) {
        try {
            $pages = Invoke-RestMethod -Uri "http://127.0.0.1:$debugPort/json" -TimeoutSec 2
            $page = $pages | Where-Object { $_.type -eq "page" -and $_.url -eq $TestUrl } | Select-Object -First 1
            if ($null -ne $page -and $page.title.Contains($ExpectedText)) {
                $matched = $true
                break
            }
        } catch {
        }
        Start-Sleep -Milliseconds 500
    }
    if (-not $matched) {
        $observed = $pages | Select-Object type,title,url | ConvertTo-Json -Compress
        throw "Expected browser page title was not found. observed=$observed"
    }
    Write-Host "Browser tailnet proxy test passed: $TestUrl"
} finally {
    if (-not $process.HasExited) {
        & taskkill.exe /PID $process.Id /T /F | Out-Null
    }
}
