param(
    [int]$CurrentPr = 0,
    [int]$PollSeconds = 60,
    [int]$MaxWaitMinutes = 360
)

$ErrorActionPreference = "Stop"

$hostRoot = $PSScriptRoot
$runtimePath = Join-Path $hostRoot "state\orchestrator-runtime.json"
$orchestrator = Join-Path $hostRoot "orchestrator-v1.3.ps1"

if ($CurrentPr -le 0) {

    if (-not (Test-Path $runtimePath)) {
        throw "CurrentPr가 없고 저장된 runtime state도 없습니다."
    }

    $runtime =
        Get-Content $runtimePath -Raw -Encoding utf8 |
        ConvertFrom-Json

    $CurrentPr = [int]$runtime.current_pr

    Write-Host "RESUME_PR=$CurrentPr"
    Write-Host "RESUME_FROM=$($runtime.status)"
    Write-Host "LAST_ACTION=$($runtime.action)"
}

& $orchestrator `
    -CurrentPr $CurrentPr `
    -PollSeconds $PollSeconds `
    -MaxWaitMinutes $MaxWaitMinutes
