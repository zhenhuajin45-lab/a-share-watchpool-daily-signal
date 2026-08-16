[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][ValidatePattern('^\d{8}$')][string]$SourceTradeDate,
    [Parameter(Mandatory = $true)][ValidatePattern('^\d{8}$')][string]$ExecutionTradeDate,
    [string]$AuthorLedger = ".\data\normalized\author_ratio.json",
    [string]$TopicContext = "",
    [string]$KaipanlaCrossEvidence = "",
    [switch]$RunDeepSeek,
    [switch]$PublishIfEligible
)

$ErrorActionPreference = "Stop"
$root = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
Push-Location $root
try {
    if (-not $env:GM_TOKEN) { throw "GM_TOKEN missing. Dot-source scripts\Set-PremarketSecrets.ps1 first." }
    if (-not (Test-Path -LiteralPath $AuthorLedger)) { throw "Author ledger not found: $AuthorLedger" }

    $rawRoot = Join-Path $root "data\raw"
    $normalizedRoot = Join-Path $root "data\normalized"
    $reportRoot = Join-Path $root "reports\premarket_command"
    New-Item -ItemType Directory -Force -Path $rawRoot, $normalizedRoot, $reportRoot | Out-Null

    $gmIndex = Join-Path $rawRoot "gm\$SourceTradeDate.json"
    $gmMarket = Join-Path $rawRoot "gm_market_sector\$SourceTradeDate.bundle.json"
    $external = Join-Path $rawRoot "external\$ExecutionTradeDate.json"
    $releaseGate = Join-Path $reportRoot "release_gate.json"
    $normalized = Join-Path $normalizedRoot "premarket_input.$ExecutionTradeDate.json"
    $draft = Join-Path $reportRoot "premarket_command.$ExecutionTradeDate.draft.json"

    & python adapters\gm_market_data_adapter.py --trade-date $SourceTradeDate --output $gmIndex
    if ($LASTEXITCODE -ne 0) { throw "GM index collection failed with exit code $LASTEXITCODE" }
    & python adapters\gm_market_breadth_sector_adapter.py --trade-date $SourceTradeDate --output $gmMarket --evidence-dir (Join-Path $rawRoot "gm_market_sector") --include-concepts
    if ($LASTEXITCODE -ne 0) { throw "GM market/sector collection failed with exit code $LASTEXITCODE" }
    & python adapters\external_market_adapter.py --trade-date $ExecutionTradeDate --out $external --no-latest
    if ($LASTEXITCODE -ne 0) { throw "External two-source collection failed with exit code $LASTEXITCODE" }

    & python tools\evaluate_premarket_release_gate.py --replay-dir data\acceptance\replay --shadow-dir data\acceptance\shadow --simulation-dir data\acceptance\simulation --output $releaseGate
    if ($LASTEXITCODE -notin 0, 2) { throw "Release-gate evaluation failed with exit code $LASTEXITCODE" }

    $assembleArgs = @(
        "tools\assemble_premarket_input.py", "--source-trade-date", $SourceTradeDate,
        "--execution-trade-date", $ExecutionTradeDate, "--gm-market-bundle", $gmMarket,
        "--gm", $gmIndex, "--author-ledger", $AuthorLedger, "--external", $external,
        "--release-gate", $releaseGate, "--output", $normalized
    )
    if ($TopicContext) { $assembleArgs += @("--topic-context", $TopicContext) }
    if ($KaipanlaCrossEvidence) { $assembleArgs += @("--kaipanla-cross-evidence", $KaipanlaCrossEvidence) }
    & python @assembleArgs
    if ($LASTEXITCODE -ne 0) { throw "Input assembly failed with exit code $LASTEXITCODE" }

    & python tools\build_premarket_command.py --input $normalized --output $draft
    if ($LASTEXITCODE -ne 0) { throw "Deterministic command is incomplete; inspect source_health.blockers in $draft" }

    if ($RunDeepSeek) {
        $review = Join-Path $reportRoot "deepseek_review.$ExecutionTradeDate.json"
        $final = Join-Path $reportRoot "premarket_command.$ExecutionTradeDate.reviewed.json"
        & python tools\run_premarket_deepseek_review.py --command $draft --review-output $review --final-output $final
        $reviewExit = $LASTEXITCODE
        if ($reviewExit -notin 0, 2) { throw "DeepSeek review failed with exit code $reviewExit" }
        if ($PublishIfEligible -and $reviewExit -eq 0) {
            & python tools\publish_premarket_command.py --contract $final --expected-execution-date $ExecutionTradeDate
            if ($LASTEXITCODE -ne 0) { throw "Publication failed with exit code $LASTEXITCODE" }
        }
    }

    Write-Host "PREMARKET_PIPELINE_COMPLETED draft=$draft"
}
finally {
    Pop-Location
}
