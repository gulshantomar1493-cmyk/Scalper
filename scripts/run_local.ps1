<#
  MarketScalper — local launcher (owner convenience, Windows/PowerShell).

  Starts the backend API (live Binance public feed) and a static server for
  the frontend, then prints the URL to open. Decision-support only — the app
  never executes trades.

  The database DSN comes from backend/config.yaml (git-ignored). The API
  token is set below (any non-empty string is fine for local use).

  Usage:   .\scripts\run_local.ps1
           .\scripts\run_local.ps1 -Token mysecret -ApiPort 8000 -WebPort 9000

  Stop:    .\scripts\run_local.ps1 -Stop      (stops the launched servers)
#>
param(
  [string]$Token   = "localdev",
  [int]   $ApiPort = 8000,
  [int]   $WebPort = 9000,
  [ValidateSet("binance","replay")][string]$Feed = "binance",
  [switch]$Stop
)

$ErrorActionPreference = "Stop"
$repo = Split-Path $PSScriptRoot -Parent
Set-Location $repo
$py = Join-Path $repo ".venv\Scripts\python.exe"

if ($Stop) {
  Get-CimInstance Win32_Process -Filter "Name='python.exe'" |
    Where-Object { $_.CommandLine -match "marketscalper\.main|http\.server $WebPort" } |
    ForEach-Object { Write-Host "stopping PID $($_.ProcessId)"; Stop-Process -Id $_.ProcessId -Force }
  Write-Host "stopped."
  return
}

$env:MARKETSCALPER_API_TOKEN = $Token
$env:MARKETSCALPER_FEED       = $Feed
$env:MARKETSCALPER_API_HOST   = "127.0.0.1"
$env:MARKETSCALPER_API_PORT   = "$ApiPort"

Write-Host "starting backend API  -> http://127.0.0.1:$ApiPort  (feed=$Feed)"
Start-Process -FilePath $py -ArgumentList "-m","marketscalper.main" -WindowStyle Hidden

Write-Host "starting frontend     -> http://127.0.0.1:$WebPort"
Start-Process -FilePath $py `
  -ArgumentList "-m","http.server","$WebPort","--directory","frontend" -WindowStyle Hidden

Start-Sleep -Seconds 4
$url = "http://127.0.0.1:$WebPort/?api=127.0.0.1:$ApiPort&token=$Token"
Write-Host ""
Write-Host "==> OPEN IN YOUR BROWSER:" -ForegroundColor Green
Write-Host "    $url" -ForegroundColor Green
Write-Host ""
Write-Host "Live BTC/ETH stream once candles close. For instant full analysis," -ForegroundColor Yellow
Write-Host "use the Replay controls (top bar) over a past date range." -ForegroundColor Yellow
Write-Host "Stop later with:  .\scripts\run_local.ps1 -Stop"
