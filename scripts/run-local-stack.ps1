param(
  [string]$ListingId = "",
  [string]$AirbnbRoomId = "1305899249107196055",
  [switch]$ForceNightly,
  [switch]$AllNightly,
  [switch]$ForceAllNightly,
  [switch]$SkipChrome,
  [switch]$SkipSchedule,
  [switch]$SkipDataQuality,
  [int]$FrontendPort = 3000
)

$ErrorActionPreference = "Stop"

$RepoRoot = Resolve-Path (Join-Path $PSScriptRoot "..")
Set-Location $RepoRoot

function Read-DotEnvValue {
  param(
    [string[]]$Paths,
    [string]$Key
  )

  foreach ($path in $Paths) {
    if (-not (Test-Path $path)) {
      continue
    }

    $line = Get-Content $path |
      Where-Object { $_ -match "^\s*$([regex]::Escape($Key))\s*=" } |
      Select-Object -First 1

    if ($line) {
      $value = $line -replace "^\s*$([regex]::Escape($Key))\s*=\s*", ""
      return $value.Trim().Trim('"').Trim("'")
    }
  }

  return ""
}

function Start-StackWindow {
  param(
    [string]$Title,
    [string]$Command
  )

  $encodedTitle = $Title.Replace("'", "''")
  $stackScriptDir = Join-Path $RepoRoot ".local-stack"
  New-Item -ItemType Directory -Force -Path $stackScriptDir | Out-Null
  $scriptPath = Join-Path $stackScriptDir (($Title -replace "[^A-Za-z0-9_-]", "_") + ".ps1")
  $script = @"
`$Host.UI.RawUI.WindowTitle = '$encodedTitle'
Set-Location '$RepoRoot'
$Command
"@
  Set-Content -LiteralPath $scriptPath -Value $script -Encoding UTF8

  Start-Process powershell.exe -ArgumentList @(
    "-NoExit",
    "-ExecutionPolicy", "Bypass",
    "-File", $scriptPath
  )
}

function Wait-ForFrontend {
  param([int]$Port)

  $healthUrl = "http://localhost:$Port/api/health"
  $schedulerUrl = "http://localhost:$Port/api/internal/nightly/schedule"
  for ($i = 0; $i -lt 60; $i++) {
    try {
      Invoke-RestMethod -Method Get -Uri $healthUrl -TimeoutSec 2 | Out-Null

      try {
        Invoke-WebRequest `
          -Method Post `
          -Uri $schedulerUrl `
          -ContentType "application/json" `
          -Body "{}" `
          -TimeoutSec 2 | Out-Null
      } catch {
        $statusCode = $_.Exception.Response.StatusCode.value__
        if ($statusCode -eq 401 -or $statusCode -eq 500) {
          return
        }
        if ($statusCode -eq 404) {
          throw "Frontend is responding on port $Port, but $schedulerUrl returned 404. Stop the existing process on that port or rerun with -FrontendPort <free port>."
        }
        throw
      }
    } catch {
      if ($_.Exception.Message -like "Frontend is responding on port*") {
        throw
      }
      Start-Sleep -Seconds 2
    }
  }

  throw "Frontend did not become ready at $healthUrl with scheduler route $schedulerUrl"
}

$envFiles = @(
  (Join-Path $RepoRoot ".env.local"),
  (Join-Path $RepoRoot ".env"),
  (Join-Path $RepoRoot "worker\.env"),
  (Join-Path $RepoRoot "ml_sidecar\.env")
)

if (-not $env:WORKER_TARGET_ENV) {
  $env:WORKER_TARGET_ENV = "local"
}

if (-not $env:WORKER_ENV) {
  $env:WORKER_ENV = "local"
}

$internalSecret = $env:INTERNAL_API_SECRET
if (-not $internalSecret) {
  $internalSecret = Read-DotEnvValue -Paths $envFiles -Key "INTERNAL_API_SECRET"
}

Write-Host "Starting AiraHost local stack from $RepoRoot" -ForegroundColor Cyan

if (-not $SkipChrome) {
  $chromePath = "C:\Program Files\Google\Chrome\Application\chrome.exe"
  if (Test-Path $chromePath) {
    Write-Host "Starting Chrome CDP on :9222" -ForegroundColor Cyan
    Start-Process $chromePath -ArgumentList @(
      "--remote-debugging-port=9222",
      "--user-data-dir=$env:USERPROFILE\chrome-cdp-profile"
    )
  } else {
    Write-Warning "Chrome was not found at $chromePath. Start CDP Chrome manually or rerun with -SkipChrome."
  }
}

Write-Host "Starting Next.js dev server" -ForegroundColor Cyan
Start-StackWindow -Title "airahost frontend" -Command "`$env:WORKER_TARGET_ENV='local'; `$env:PORT='$FrontendPort'; npm run dev -- -p $FrontendPort"

Write-Host "Starting nightly worker" -ForegroundColor Cyan
Start-StackWindow -Title "airahost nightly worker" -Command "`$env:WORKER_ENV='local'; `$env:WORKER_LANE='nightly'; python -m worker.main"

Write-Host "Starting ML sidecar worker" -ForegroundColor Cyan
Start-StackWindow -Title "airahost ml sidecar worker" -Command "python -m ml_sidecar.worker"

if (-not $SkipSchedule -and ($AllNightly -or $ForceAllNightly -or $ListingId -or $AirbnbRoomId)) {
  if (-not $internalSecret) {
    throw "INTERNAL_API_SECRET was not found in environment or .env files. Cannot schedule nightly."
  }

  if (($AllNightly -or $ForceAllNightly) -and $ListingId) {
    throw "-AllNightly and -ForceAllNightly cannot be combined with -ListingId."
  }

  Write-Host "Waiting for frontend before scheduling nightly..." -ForegroundColor Cyan
  Wait-ForFrontend -Port $FrontendPort

  $body = @{}
  if ($ForceAllNightly) {
    $body.force = $true
    $body.forceAll = $true
    Write-Host "Scheduling forced nightly for all eligible saved listings" -ForegroundColor Cyan
  } elseif ($AllNightly) {
    Write-Host "Scheduling nightly for all eligible saved listings" -ForegroundColor Cyan
  } elseif ($ListingId) {
    $body.listingId = $ListingId
    Write-Host "Scheduling nightly for saved listing $ListingId" -ForegroundColor Cyan
  } else {
    $body.airbnbRoomId = $AirbnbRoomId
    Write-Host "Scheduling nightly for Airbnb room $AirbnbRoomId" -ForegroundColor Cyan
  }
  if ($ForceNightly -and -not $ForceAllNightly) {
    $body.force = $true
  }

  $scheduleResult = Invoke-RestMethod `
    -Method Post `
    -Uri "http://localhost:$FrontendPort/api/internal/nightly/schedule" `
    -Headers @{ Authorization = "Bearer $internalSecret" } `
    -ContentType "application/json" `
    -Body ($body | ConvertTo-Json)

  $scheduleResult | ConvertTo-Json -Depth 10

  $reportIds = @(
    $scheduleResult.results |
      Where-Object { $_.scheduled -and $_.reportId } |
      ForEach-Object { $_.reportId }
  )

  if (-not $SkipDataQuality -and $reportIds.Count -gt 0) {
    $reportArgs = ($reportIds | ForEach-Object { "--report-id `"$_`"" }) -join " "
    $dqJson = "ml_sidecar\reports\data_quality_latest.json"
    $dqCsv = "ml_sidecar\reports\data_quality_issues_latest.csv"
    $dqHtml = "ml_sidecar\reports\nightly_quality_report.html"
    Write-Host "Starting data quality monitor for report(s): $($reportIds -join ', ')" -ForegroundColor Cyan
    Start-StackWindow `
      -Title "airahost data quality" `
      -Command "python -m ml_sidecar.data_quality $reportArgs --wait-ready --wait-timeout-seconds 7200 --poll-seconds 20 --output `"$dqJson`" --issues-csv `"$dqCsv`"; python .\ml_sidecar\quality_report_html.py; Write-Host ''; Write-Host 'Data quality JSON: $dqJson'; Write-Host 'Issues CSV: $dqCsv'; Write-Host 'Quality HTML: $dqHtml'"
  } elseif ($SkipDataQuality -and $reportIds.Count -gt 0) {
    Write-Host "Generating quality HTML from existing artifacts because -SkipDataQuality was set." -ForegroundColor Yellow
    python .\ml_sidecar\quality_report_html.py
  } elseif (-not $SkipDataQuality) {
    Write-Host "No scheduled report id was returned, so data quality was not started." -ForegroundColor Yellow
  }
} elseif (-not $SkipSchedule) {
  Write-Host "Services are up. Pass -ListingId <uuid> or -AirbnbRoomId <room_id> to schedule a nightly job." -ForegroundColor Yellow
}

Write-Host "Local stack launched." -ForegroundColor Green
