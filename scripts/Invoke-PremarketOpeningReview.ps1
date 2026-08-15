[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][ValidatePattern('^\d{8}$')][string]$ExecutionTradeDate,
    [Parameter(Mandatory = $true)][string]$PublishedContract,
    [Parameter(Mandatory = $true)][string]$PreviousGmBundle
)

$ErrorActionPreference = "Stop"
$root = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
Push-Location $root
try {
    if (-not $env:GM_TOKEN) { throw "GM_TOKEN missing" }
    $reportRoot = Join-Path $root "reports\premarket_command"
    $rawRoot = Join-Path $root "data\raw\gm_opening\$ExecutionTradeDate"
    New-Item -ItemType Directory -Force -Path $reportRoot, $rawRoot | Out-Null
    $snapshot = Join-Path $reportRoot "gm_opening.$ExecutionTradeDate.json"
    $rawTicks = Join-Path $rawRoot "ticks.csv.gz"
    $candidate = Join-Path $reportRoot "opening_candidate.$ExecutionTradeDate.json"
    $revised = Join-Path $reportRoot "premarket_command.$ExecutionTradeDate.opening_reviewed.json"

    & python adapters\gm_opening_auction_adapter.py --execution-date $ExecutionTradeDate --previous-bundle $PreviousGmBundle --output $snapshot --raw-output $rawTicks
    if (-not (Test-Path -LiteralPath $snapshot)) { throw "GM opening adapter produced no auditable snapshot" }
    & python tools\build_premarket_opening_command.py --published $PublishedContract --gm-opening $snapshot --output $candidate
    if ($LASTEXITCODE -notin 0, 2) { throw "Opening candidate build failed with exit code $LASTEXITCODE" }
    & python tools\run_premarket_opening_review.py --published $PublishedContract --opening-command $candidate --output $revised
    if ($LASTEXITCODE -ne 0) { throw "Opening tighten-only review failed with exit code $LASTEXITCODE" }
    Write-Host "PREMARKET_OPENING_REVIEW_COMPLETED output=$revised"
}
finally {
    Pop-Location
}
