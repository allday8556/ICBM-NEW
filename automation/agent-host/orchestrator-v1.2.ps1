param(
    [Parameter(Mandatory=$true)]
    [int]$CurrentPr
)

$ErrorActionPreference = "Stop"

$hostRoot  = $PSScriptRoot
$configPath = Join-Path $hostRoot "state\orchestrator-config.json"
$statePath  = Join-Path $hostRoot "state\orchestrator-state.json"
$stateDir   = Join-Path $hostRoot "state"

if (-not (Test-Path $configPath)) {
    Write-Host "ORCHESTRATOR_BLOCKED=CONFIG_NOT_FOUND"
    return
}

$config = Get-Content $configPath -Raw | ConvertFrom-Json
$repoSlug = $config.repository
$repoPath = $config.repo_path

# run-audit-v1.1.ps1의 실제 policy version을 canonical source로 사용
$auditScriptPath = Join-Path $PSScriptRoot "run-audit-v1.1.ps1"

if (-not (Test-Path $auditScriptPath)) {
    throw "AUDIT_SCRIPT_NOT_FOUND=$auditScriptPath"
}

$auditScriptText = Get-Content $auditScriptPath -Raw -Encoding utf8

$policyMatch = [regex]::Match(
    $auditScriptText,
    '\$auditPolicyVersion\s*=\s*"([^"]+)"'
)

if (-not $policyMatch.Success) {
    throw "AUDIT_POLICY_VERSION_NOT_FOUND"
}

$auditPolicyVersion = $policyMatch.Groups[1].Value

function Get-PrInfo {
    param([int]$Number)

    gh pr view $Number `
        --repo $repoSlug `
        --json number,title,state,mergedAt,headRefOid,headRefName,baseRefName,url |
        ConvertFrom-Json
}

function Test-AuditPass {
    param(
        [string]$Path,
        [string]$Head
    )

    if (-not (Test-Path $Path)) {
        return $false
    }

    $text = Get-Content $Path -Raw

    return (
        $text -match "AUDIT_HEAD=$Head" -and
        $text -match "(?m)^VERDICT=PASS\s*$"
    )
}

$mainHead = gh api "repos/$repoSlug/commits/main" --jq .sha
$pr = Get-PrInfo -Number $CurrentPr

$mainShort = $mainHead.Substring(0,12)
$headShort = $pr.headRefOid.Substring(0,12)

$cacheKey = "$auditPolicyVersion-pr-$CurrentPr-main-$mainShort-head-$headShort"

$gptCache = Join-Path $stateDir "$cacheKey-gpt.txt"
$claudeCache = Join-Path $stateDir "$cacheKey-claude.txt"

$gptPass = Test-AuditPass -Path $gptCache -Head $pr.headRefOid
$claudePass = Test-AuditPass -Path $claudeCache -Head $pr.headRefOid

$currentState = "AUDIT_REQUIRED"

if ($pr.state -eq "MERGED" -or $pr.mergedAt) {
    $currentState = "MERGED"
}
elseif ($gptPass -and $claudePass) {
    $currentState = "WAITING_FOR_HUMAN_MERGE"
}
elseif ($gptPass -and -not $claudePass) {
    $currentState = "CLAUDE_AUDIT_REQUIRED"
}

$oldState = $null

if (Test-Path $statePath) {
    try {
        $oldState = Get-Content $statePath -Raw | ConvertFrom-Json
    }
    catch {
        $oldState = $null
    }
}

# 기존 NEXT 슬롯은 보존
$next1 = $null
$next2 = $null
$next3 = $null

if ($oldState) {
    $next1 = $oldState.next_1
    $next2 = $oldState.next_2
    $next3 = $oldState.next_3
}
# LOOKAHEAD_STALE_DETECTOR_V1
$headMoved = $false
$mainMoved = $false

if ($oldState) {
    $previousHead = [string]$oldState.current.head
    $previousMain = [string]$oldState.canonical_main

    if ($previousHead -and $previousHead -ne $pr.headRefOid) {
        $headMoved = $true
    }

    if ($previousMain -and $previousMain -ne $mainHead) {
        $mainMoved = $true
    }
}

function Set-LookaheadStale {
    param(
        $Slot,
        [string]$Reason
    )

    if (-not $Slot) {
        return $null
    }

    # 원본 준비내용은 그대로 보존
    $copy = $Slot |
        ConvertTo-Json -Depth 20 |
        ConvertFrom-Json

    if (-not $copy.PSObject.Properties["prepared_state"]) {
        $copy |
            Add-Member `
                -NotePropertyName prepared_state `
                -NotePropertyValue ([string]$copy.state)
    }

    $copy.state = "STALE_PREP"

    if ($copy.PSObject.Properties["stale_reason"]) {
        $copy.stale_reason = $Reason
    }
    else {
        $copy |
            Add-Member `
                -NotePropertyName stale_reason `
                -NotePropertyValue $Reason
    }

    $now = (Get-Date).ToString("o")

    if ($copy.PSObject.Properties["stale_detected_at"]) {
        $copy.stale_detected_at = $now
    }
    else {
        $copy |
            Add-Member `
                -NotePropertyName stale_detected_at `
                -NotePropertyValue $now
    }

    return $copy
}

if ($headMoved -or $mainMoved) {

    $reasons = @()

    if ($headMoved) {
        $reasons += "CURRENT_PR_HEAD_CHANGED"
    }

    if ($mainMoved) {
        $reasons += "CANONICAL_MAIN_CHANGED"
    }

    $staleReason = ($reasons -join "+")

    $next1 = Set-LookaheadStale `
        -Slot $next1 `
        -Reason $staleReason

    $next2 = Set-LookaheadStale `
        -Slot $next2 `
        -Reason $staleReason

    $next3 = Set-LookaheadStale `
        -Slot $next3 `
        -Reason $staleReason
}


$state = [ordered]@{
    version = "1.2"
    updated_at = (Get-Date).ToString("o")

    canonical_main = $mainHead
    head_changed_since_last_run = $headMoved
    main_changed_since_last_run = $mainMoved

    current = [ordered]@{
        kind = "PR"
        number = $CurrentPr
        title = $pr.title
        branch = $pr.headRefName
        head = $pr.headRefOid
        github_state = $pr.state
        state = $currentState
        gpt_pass = $gptPass
        claude_pass = $claudePass
        auto_merge = ($config.auto_merge -eq $true)
        human_merge_required = -not ($config.auto_merge -eq $true)
    }

    next_1 = $next1
    next_2 = $next2
    next_3 = $next3
}

$state |
    ConvertTo-Json -Depth 10 |
    Set-Content -Encoding utf8 $statePath

Write-Host ""
Write-Host "========================================"
Write-Host " ICBM ORCHESTRATOR V1.2"
Write-Host "========================================"
Write-Host ""

Write-Host "MAIN      : $mainHead"
Write-Host "CURRENT   : PR #$CurrentPr"
Write-Host "HEAD      : $($pr.headRefOid)"
Write-Host "GPT       : $gptPass"
Write-Host "CLAUDE    : $claudePass"
Write-Host "STATE     : $currentState"
Write-Host "HEAD MOVED: $headMoved"
Write-Host "MAIN MOVED: $mainMoved"
Write-Host ""

if ($currentState -eq "WAITING_FOR_HUMAN_MERGE") {
    if ($config.auto_merge -eq $true) {
        Write-Host "ACTION    : MERGE_GUARD -> AUTO MERGE"
        Write-Host "AUTO_MERGE: TRUE (guarded)"
    }
    else {
        Write-Host "ACTION    : HUMAN MERGE REQUIRED"
        Write-Host "AUTO_MERGE: FALSE"
    }
}
elseif ($currentState -eq "MERGED") {
    Write-Host "ACTION    : READY_FOR_NEXT_WORK_PROMOTION"
}
elseif ($currentState -eq "CLAUDE_AUDIT_REQUIRED") {
    Write-Host "ACTION    : CLAUDE CROSS-AUDIT REQUIRED"
}
else {
    Write-Host "ACTION    : EXACT-HEAD AUDIT REQUIRED"
}

Write-Host ""
Write-Host "NEXT-1    : $(if ($next1) {$next1.state} else {'EMPTY'})"
Write-Host "NEXT-2    : $(if ($next2) {$next2.state} else {'EMPTY'})"
Write-Host "NEXT-3    : $(if ($next3) {$next3.state} else {'EMPTY'})"
Write-Host ""
Write-Host "STATE_FILE=$statePath"
Write-Host ""


