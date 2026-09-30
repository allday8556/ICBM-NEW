param(
    [int]$IssueNumber = 89,

    # 방금 병합된 PR (로그/프롬프트 표시용, remediation baseline fallback).
    [int]$MergedPr = 0,

    # repo 전체 전수 감사를 강제한다.
    [switch]$ForceFull,

    # DELTA에서 자동 승격된 FULL이면 그 사유 (내부 호출용)
    [string]$EscalationReason = ""
)

$ErrorActionPreference = "Stop"

$utf8 = New-Object System.Text.UTF8Encoding($false)
$script:OutputEncoding = $utf8
[Console]::OutputEncoding = $utf8

$hostRoot = $PSScriptRoot
$configPath = Join-Path $hostRoot "state\orchestrator-config.json"
$stateDir = Join-Path $hostRoot "state"
$logDir = Join-Path $hostRoot "logs"

New-Item -ItemType Directory -Force -Path $stateDir | Out-Null
New-Item -ItemType Directory -Force -Path $logDir | Out-Null

$config = Get-Content $configPath -Raw -Encoding utf8 | ConvertFrom-Json

$repoSlug = [string]$config.repository
# 사용자 repo는 object DB / origin remote 로만 사용한다.
# 사용자 repo의 branch / HEAD / working files는 절대 바꾸지 않는다.
$repoPath = [string]$config.repo_path

$fullPolicy = "full-audit-v1-sol-high"
$policyVersion = $fullPolicy
$auditMode = "FULL"
$baselinePath = Join-Path $stateDir "full-audit-baseline.json"
$escalationPath = Join-Path $stateDir "full-audit-escalation.json"
$deferredPath = Join-Path $stateDir "full-audit-deferred.json"
$selfPath = $PSCommandPath

# -------------------------------------------------
# Audit tool identity: 이 실행의 host 도구 해시와 AI CLI 버전.
# 로그에 출력하고, 이 실행이 새로 만든 감사 결과 파일에 도장으로 남긴다.
# (캐시 재사용 시에는 원래 판정을 낸 도구의 도장이 그대로 유지된다)
# -------------------------------------------------

function Get-CliVersion {
    param([string]$Name)

    $oldEap = $ErrorActionPreference

    try {
        $ErrorActionPreference = "Continue"
        $v = & $Name --version 2>$null | Select-Object -First 1
        $v = "$v".Trim()

        if ($v) {
            return $v
        }

        return "UNKNOWN"
    }
    catch {
        return "UNKNOWN"
    }
    finally {
        $ErrorActionPreference = $oldEap
    }
}

$script:auditToolName = Split-Path $PSCommandPath -Leaf
$script:auditToolSha = (Get-FileHash -Algorithm SHA256 -LiteralPath $PSCommandPath).Hash.ToLower()
$script:codexCli = Get-CliVersion "codex"
$script:claudeCli = Get-CliVersion "claude"

Write-Host "AUDIT_TOOL=$script:auditToolName"
Write-Host "AUDIT_TOOL_SHA256=$script:auditToolSha"
Write-Host "CODEX_CLI=$script:codexCli"
Write-Host "CLAUDE_CLI=$script:claudeCli"

function Add-ToolStamp {
    param(
        [string]$Path,
        [string]$Auditor,
        [string]$Policy = ""
    )

    if (-not (Test-Path $Path)) {
        return
    }

    $enc = New-Object System.Text.UTF8Encoding($false)
    $text = [System.IO.File]::ReadAllText($Path)

    if ($text -match '(?m)^AUDIT_TOOL_SHA256=') {
        return
    }

    if ($text.Length -gt 0 -and -not $text.EndsWith("`n")) {
        $text += "`r`n"
    }

    $cli = if ($Auditor -eq "GPT") { $script:codexCli } else { $script:claudeCli }

    $text += (@(
        ""
        "AUDIT_TOOL=$script:auditToolName"
        "AUDIT_TOOL_SHA256=$script:auditToolSha"
        "AUDIT_POLICY=$Policy"
        "AUDITOR=$Auditor"
        "AUDITOR_CLI=$cli"
        "AUDIT_STAMPED_AT=$((Get-Date).ToString('o'))"
    ) -join "`r`n") + "`r`n"

    [System.IO.File]::WriteAllText($Path, $text, $enc)
}

# 전수 감사 주기/milestone 경계 정책 (config.full_audit_policy)
$fap = $config.full_audit_policy
$fullIntervalDays = if ($fap -and $fap.full_interval_days) { [double]$fap.full_interval_days } else { 7 }
$maxConsecutiveDelta = if ($fap -and $fap.max_consecutive_delta) { [int]$fap.max_consecutive_delta } else { 10 }
$milestoneFile = if ($fap -and $fap.milestone_marker_file) { [string]$fap.milestone_marker_file } else { "app/__init__.py" }
$milestonePattern = if ($fap -and $fap.milestone_marker_pattern) { [string]$fap.milestone_marker_pattern } else { 'MILESTONE\s*=\s*"([^"]+)"' }

# FULL 강제 트리거 (baseline..main 변경 경로). config가 없으면 아래 기본값.
# 위험 경계를 건드린 변경은 DELTA로 끝내지 않는다.
$defaultTriggers = [ordered]@{
    ARCHITECTURE_OR_ADR = @('^docs/adr/', '^docs/ARCHITECTURE\.md$', '^CLAUDE\.md$', '^documents/decisions/adr/', '^documents/architecture/ARCHITECTURE\.md$', '^documents/rules/')
    SCHEMA_OR_MIGRATION = @('^app/db/', '(^|/)models\.py$', '(^|/)alembic', '(^|/)migrations/')
    PROVIDER_OR_LIVE_BOUNDARY = @('^integrations/', '^app/live/', '^app/core/execution\.py$', '^app/system/execution_mode\.py$', '^app/register/(execution|caller|canary)\.py$', '^app/container\.py$', '^docs/platforms/', '^app/capabilities/live_safety/', '^app/platform/core/(execution|egress)', '^app/stages/register/(execution|caller|canary)\.py$', '^documents/contracts/platforms/')
    MILESTONE_OR_GATE_CLOSEOUT = @('^docs/acceptance/', '^app/__init__\.py$', '^documents/acceptance/', '^documents/roadmap/CURRENT-MILESTONE\.md$')
}

$fullTriggers = [ordered]@{}

foreach ($k in $defaultTriggers.Keys) {
    $fromConfig = if ($fap -and $fap.full_triggers -and $fap.full_triggers.$k) { @($fap.full_triggers.$k) } else { @() }
    # config는 기본값을 좁힐 수 없다: 기본값 + config 추가분
    $fullTriggers[$k] = @($defaultTriggers[$k] + $fromConfig | Select-Object -Unique)
}

# 구축 기간: 경로 트리거와 정기 건강검진의 FULL은 즉시 돌리지 않고 보류 목록에 기록한다.
# (baseline 없음, DELTA 승격/INSUFFICIENT/실패, milestone 경계는 계속 즉시 FULL)
# 실사용(LIVE/canary) 전에 -ForceFull 로 한 번에 전수 감사하고, FULL이 끝나면 목록이 비워진다.
$deferFull = ($fap -and $fap.defer_full_until_pre_live -eq $true)
$deferredNow = New-Object System.Collections.Generic.List[string]

# ROADMAP §12 milestone 상태 줄(ACCEPTED/CURRENT) 변경도 milestone closeout
$roadmapMilestoneLine = '^[+-]\s*(?:→\s*)?M\d+(?:\.\d+)?\s.*\s(ACCEPTED|CURRENT)\b'

$worktreeRoot = Join-Path $hostRoot "worktrees"
$auditWorktree = Join-Path $worktreeRoot "full-audit-main"

New-Item -ItemType Directory -Force -Path $worktreeRoot | Out-Null

$script:nativeErrPath = Join-Path `
    $logDir `
    "full-audit-native-$(Get-Date -Format 'yyyyMMdd-HHmmss').stderr.txt"

# PS5.1 + ErrorActionPreference=Stop 에서 git의 정상 stderr가
# terminating error로 오인되지 않도록 lookahead와 같은 방식을 사용한다.
function Invoke-Git {
    param([string[]]$GitArgs)

    $oldEap = $ErrorActionPreference

    try {
        $ErrorActionPreference = "Continue"
        $out = & git @GitArgs 2>> $script:nativeErrPath
        $code = $LASTEXITCODE
    }
    finally {
        $ErrorActionPreference = $oldEap
    }

    $global:LASTEXITCODE = $code
    return $out
}

# repair/audit와 같은 isolated worktree 원칙. exact main이 다르면 재생성.
function Initialize-HostWorktree {
    param(
        [string]$Path,
        [string]$Commit
    )

    if (Test-Path (Join-Path $Path ".git")) {
        $wtHead = "$(Invoke-Git @('-C', $Path, 'rev-parse', 'HEAD'))".Trim()
        $wtDirty = Invoke-Git @("-C", $Path, "status", "--porcelain")

        if ($wtHead -eq $Commit -and -not $wtDirty) {
            return $true
        }
    }

    if (Test-Path $Path) {
        Invoke-Git @("-C", $repoPath, "worktree", "remove", "--force", $Path) | Out-Null

        if (Test-Path $Path) {
            Remove-Item $Path -Recurse -Force
        }
    }

    Invoke-Git @("-C", $repoPath, "worktree", "add", "-f", "--detach", $Path, $Commit) | Out-Null

    if ($LASTEXITCODE -ne 0) {
        return $false
    }

    $wtHead = "$(Invoke-Git @('-C', $Path, 'rev-parse', 'HEAD'))".Trim()
    return ($wtHead -eq $Commit)
}

function Get-RemoteMain {
    return (
        gh api "repos/$repoSlug/commits/main" --jq .sha
    ).Trim()
}

function Get-CiState {
    param([string]$Head)

    $raw = gh api `
        "repos/$repoSlug/commits/$Head/check-runs?per_page=100"

    $data = $raw | ConvertFrom-Json
    $checks = @($data.check_runs)

    if ($checks.Count -eq 0) {
        return [pscustomobject]@{
            State = "PENDING"
            Total = 0
            Success = 0
            Pending = 0
            Failed = 0
        }
    }

    $pending = @(
        $checks |
        Where-Object {
            $_.status -ne "completed"
        }
    )

    # Issue #143: "CI gate" job 이 있으면 scope 가 의도적으로 건너뛴(skipped) job 만 중립이고,
    # gate 자체는 이 exact HEAD 에서 success 여야 한다. gate 가 없는 기존 workflow 는 종전대로 전부 success.
    $gateRuns = @($checks | Where-Object { $_.name -eq "CI gate" })
    $scopedCi = ($gateRuns.Count -gt 0 -or @($checks | Where-Object { $_.name -eq "CI scope" }).Count -gt 0)
    $neutralConclusions = if ($scopedCi) { @("success", "skipped") } else { @("success") }
    # draft(wip) run 의 skipped gate 는 중립. 병합 준비는 success gate 가 하나 이상 있어야 한다.
    $gateSucceeded = @($gateRuns | Where-Object { $_.status -eq "completed" -and $_.conclusion -eq "success" })
    $gateOpen = @($gateRuns | Where-Object { $_.status -ne "completed" })
    if ($scopedCi -and $gateSucceeded.Count -eq 0 -and $gateOpen.Count -eq 0) {
        # gate job 은 모든 job 이 끝난 뒤 생성된다 → 아직 판정 전
        $pending = @($pending) + @([pscustomobject]@{ name = "CI gate"; status = "queued"; conclusion = $null })
    }

    # Full audit는 fail-closed.
    # completed인데 success가 아니면 전부 실패/확인필요로 본다 (CI gate 가 있을 때 scope skip 만 예외).
    $failed = @(
        $checks |
        Where-Object {
            $_.status -eq "completed" -and
            ($_.conclusion -notin $neutralConclusions -or
             ($_.name -eq "CI gate" -and $_.conclusion -notin @("success", "skipped")))
        }
    )

    $success = @(
        $checks |
        Where-Object {
            $_.status -eq "completed" -and
            $_.conclusion -eq "success"
        }
    )

    $state = if ($failed.Count -gt 0) {
        "FAILED"
    }
    elseif ($pending.Count -gt 0) {
        "PENDING"
    }
    else {
        "GREEN"
    }

    return [pscustomobject]@{
        State = $state
        Total = $checks.Count
        Success = $success.Count
        Pending = $pending.Count
        Failed = $failed.Count
    }
}

function Read-AuditVerdict {
    param(
        [string]$Path,
        [string]$ExpectedMain
    )

    if (-not (Test-Path $Path)) {
        return $null
    }

    $text = Get-Content $Path -Raw -Encoding utf8

    $mainMatch = [regex]::Match(
        $text,
        '(?m)^AUDIT_MAIN=(.+?)\s*$'
    )

    $verdictMatch = [regex]::Match(
        $text,
        '(?m)^VERDICT=(PASS|BLOCKER|INSUFFICIENT)\s*$'
    )

    $summaryMatch = [regex]::Match(
        $text,
        '(?m)^SUMMARY=(.+?)\s*$'
    )

    if (
        -not $mainMatch.Success -or
        -not $verdictMatch.Success
    ) {
        return [pscustomobject]@{
            Valid = $false
            Verdict = $null
            Summary = ""
        }
    }

    return [pscustomobject]@{
        Valid = (
            $mainMatch.Groups[1].Value.Trim() -eq $ExpectedMain
        )
        Verdict = $verdictMatch.Groups[1].Value.Trim()
        Summary = if ($summaryMatch.Success) {
            $summaryMatch.Groups[1].Value.Trim()
        }
        else {
            ""
        }
    }
}

function Save-FullAuditState {
    param(
        [string]$Status,
        [string]$Main,
        [string]$GptVerdict = "",
        [string]$ClaudeVerdict = "",
        [string]$Detail = ""
    )

    $obj = [ordered]@{
        version = "1"
        policy = $policyVersion
        main = $Main
        status = $Status
        gpt_verdict = $GptVerdict
        claude_verdict = $ClaudeVerdict
        detail = $Detail
        mode = $auditMode
        auto_merge = $false
        implementation_authorized = $false
        updated_at = (Get-Date).ToString("o")
    }

    $obj |
        ConvertTo-Json -Depth 10 |
        Set-Content `
            -Encoding utf8 `
            (Join-Path $stateDir "full-audit-state.json")
}

# 감사가 끝난 main(BLOCKED/DUAL_PASS)을 다음 DELTA 감사의 baseline으로 기록한다.
function Save-AuditBaseline {
    param([string]$Status)

    if ($reusedBaseline) {
        return
    }

    if ($auditMode -eq "FULL") {
        Remove-Item $escalationPath -Force -ErrorAction SilentlyContinue
        Remove-Item $deferredPath -Force -ErrorAction SilentlyContinue
    }

    $isFull = ($auditMode -eq "FULL")

    $obj = [ordered]@{
        main = $mainHead
        policy = $policyVersion
        mode = $auditMode
        status = $Status
        last_full_main = if ($isFull) { $mainHead } else { $lastFullMain }
        last_full_at = if ($isFull) { (Get-Date).ToString("o") } else { $lastFullAt }
        delta_count_since_full = if ($isFull) { 0 } else { $deltaCount + 1 }
        audit_tool = $script:auditToolName
        audit_tool_sha256 = $script:auditToolSha
        codex_cli = $script:codexCli
        claude_cli = $script:claudeCli
        updated_at = (Get-Date).ToString("o")
    }

    $obj |
        ConvertTo-Json -Depth 5 |
        Set-Content -Encoding utf8 $baselinePath
}

Write-Host ""
Write-Host "========================================"
Write-Host " ICBM POST-MERGE FULL AUDIT V1"
Write-Host "========================================"
Write-Host ""

$mainHead = Get-RemoteMain

Write-Host "REMOTE_MAIN=$mainHead"
Write-Host ""

# -------------------------------------------------
# CI gate
# -------------------------------------------------

$ci = Get-CiState -Head $mainHead

Write-Host "CI_STATE=$($ci.State)"
Write-Host "CI_SUCCESS=$($ci.Success)/$($ci.Total)"
Write-Host "CI_PENDING=$($ci.Pending)"
Write-Host "CI_FAILED=$($ci.Failed)"
Write-Host ""

if ($ci.State -eq "PENDING") {
    Save-FullAuditState `
        -Status "CI_WAIT" `
        -Main $mainHead `
        -Detail "CI pending"

    Write-Output "FULL_AUDIT_WAIT=CI_PENDING"
    return
}

if ($ci.State -eq "FAILED") {
    Save-FullAuditState `
        -Status "TECHNICAL_HOLD" `
        -Main $mainHead `
        -Detail "CI failed"

    Write-Output "FULL_AUDIT_HOLD=CI_FAILED"
    return
}

# -------------------------------------------------
# Dedicated exact-main audit worktree
# 사용자 checkout이 main이 아니어도, dirty여도 감사 가능 (사용자 repo 불변).
# -------------------------------------------------

Invoke-Git @(
    "-C", $repoPath, "fetch", "--no-tags", "--quiet", "--refmap=", "origin",
    "+refs/heads/main:refs/icbm-agent-host/main"
) | Out-Null

Invoke-Git @("-C", $repoPath, "cat-file", "-e", "$mainHead^{commit}") | Out-Null

if ($LASTEXITCODE -ne 0) {
    Write-Output "FULL_AUDIT_HOLD=MAIN_NOT_FETCHED"
    return
}

if (-not (Initialize-HostWorktree -Path $auditWorktree -Commit $mainHead)) {
    Write-Output "FULL_AUDIT_HOLD=AUDIT_WORKTREE_FAILED"
    return
}

Write-Host "AUDIT_WORKTREE=$auditWorktree"
Write-Host ""

# -------------------------------------------------
# Audit mode
#
# 기본은 DELTA: 마지막으로 감사가 끝난 main(baseline)의 BLOCKER 해소 + baseline..main 변경분만 감사.
# FULL(repo 전수)은 다음 경우에만:
#   - baseline 없음 / baseline 보고서 없음 / baseline이 현재 main의 조상이 아님
#   - milestone 경계 (milestone marker 값 변경)
#   - 주기: 마지막 FULL 이후 full_interval_days 경과 또는 DELTA가 max_consecutive_delta 회 연속
#   - -ForceFull
# -------------------------------------------------

$baselineMain = ""
$baselineReportText = ""
$fullReason = ""
$reusedBaseline = $false
$lastFullMain = ""
$lastFullAt = ""
$deltaCount = 0

function Get-BaselineReports {
    param(
        [string]$BMain,
        [string]$BPolicy
    )

    $bShort = $BMain.Substring(0,12)

    return @(
        foreach ($sfx in @("gpt", "claude")) {
            $rp = Join-Path $stateDir "$BPolicy-main-$bShort-$sfx.txt"

            if (Test-Path $rp) {
                "===== BASELINE $($sfx.ToUpper()) AUDIT ($BPolicy, main $BMain) =====`n" +
                    (Get-Content $rp -Raw -Encoding utf8)
            }
        }
    )
}

function Get-MilestoneMarker {
    param([string]$Commit)

    $text = (@(Invoke-Git @("-C", $auditWorktree, "show", "${Commit}:$milestoneFile")) -join "`n")
    $m = [regex]::Match($text, $milestonePattern)

    if ($m.Success) {
        return $m.Groups[1].Value
    }

    return ""
}

$candidate = $null

# 1) 마지막 감사 완료 baseline
if (Test-Path $baselinePath) {
    try {
        $b = Get-Content $baselinePath -Raw -Encoding utf8 | ConvertFrom-Json

        if ($b.main) {
            $candidate = [pscustomobject]@{
                Main = [string]$b.main
                Policy = [string]$b.policy
                LastFullMain = [string]$b.last_full_main
                LastFullAt = [string]$b.last_full_at
                DeltaCount = [int]$b.delta_count_since_full
                Source = "BASELINE_FILE"
            }
        }
    }
    catch {
        $candidate = $null
    }
}

# 2) fallback: 방금 병합된 host remediation PR의 기준 main (baseline 파일 도입 전 호환)
if (-not $candidate -and $MergedPr -gt 0) {
    foreach ($rf in @(Get-ChildItem $stateDir -Filter "remediation-main-*.json" -ErrorAction SilentlyContinue)) {
        $reg = $null

        try {
            $reg = Get-Content $rf.FullName -Raw -Encoding utf8 | ConvertFrom-Json
        }
        catch {
            $reg = $null
        }

        if (-not $reg -or "$($reg.pr)" -ne "$MergedPr" -or -not $reg.main) {
            continue
        }

        $rPolicy = if ($reg.audit_policy) { [string]$reg.audit_policy } else { $fullPolicy }
        $rShort = ([string]$reg.main).Substring(0,12)
        $rReport = Join-Path $stateDir "$rPolicy-main-$rShort-gpt.txt"
        $rAt = if (Test-Path $rReport) { (Get-Item $rReport).LastWriteTime.ToString("o") } else { "" }

        $candidate = [pscustomobject]@{
            Main = [string]$reg.main
            Policy = $rPolicy
            LastFullMain = if ($rPolicy -eq $fullPolicy) { [string]$reg.main } else { "" }
            LastFullAt = if ($rPolicy -eq $fullPolicy) { $rAt } else { "" }
            DeltaCount = 0
            Source = "REMEDIATION_REGISTRY"
        }
    }
}

$pendingEscalation = $null

if (Test-Path $escalationPath) {
    try {
        $pendingEscalation = Get-Content $escalationPath -Raw -Encoding utf8 | ConvertFrom-Json
    }
    catch {
        $pendingEscalation = $null
    }
}

if ($ForceFull) {
    $fullReason = if ($EscalationReason) { "ESCALATED:$EscalationReason" } else { "FORCED" }
}
elseif ($pendingEscalation -and [string]$pendingEscalation.main -eq $mainHead) {
    # 이 main의 이전 DELTA 감사가 실행 실패 → FULL로 승격
    $fullReason = "PREVIOUS_AUDIT_FAILURE:$($pendingEscalation.reason)"
}
elseif (-not $candidate) {
    $fullReason = "NO_BASELINE"
}
elseif ($candidate.Main -eq $mainHead -and $candidate.Source -eq "BASELINE_FILE") {
    # 이 exact main은 이미 감사가 끝났다 → 같은 policy 캐시를 재사용 (재감사/토큰 소모 없음)
    $reusedBaseline = $true
    $policyVersion = $candidate.Policy
    $auditMode = if ($candidate.Policy -like "*-delta") { "DELTA" } else { "FULL" }
    $lastFullMain = $candidate.LastFullMain
    $lastFullAt = $candidate.LastFullAt
    $deltaCount = $candidate.DeltaCount
    $fullReason = "ALREADY_AUDITED_MAIN"
}
elseif ($candidate.Main -eq $mainHead) {
    $fullReason = "BASELINE_IS_CURRENT_MAIN"
}
else {
    $bReports = Get-BaselineReports -BMain $candidate.Main -BPolicy $candidate.Policy

    Invoke-Git @("-C", $auditWorktree, "merge-base", "--is-ancestor", $candidate.Main, $mainHead) | Out-Null
    $isAncestor = ($LASTEXITCODE -eq 0)

    $lastFullAge = $null

    if ($candidate.LastFullAt) {
        try {
            $lastFullAge = ((Get-Date) - [DateTime]::Parse($candidate.LastFullAt)).TotalDays
        }
        catch {
            $lastFullAge = $null
        }
    }

    $markerBase = Get-MilestoneMarker -Commit $candidate.Main
    $markerNow = Get-MilestoneMarker -Commit $mainHead

    if ($bReports.Count -eq 0) {
        $fullReason = "BASELINE_REPORTS_MISSING"
    }
    elseif (-not $isAncestor) {
        $fullReason = "BASELINE_NOT_ANCESTOR"
    }
    elseif ($markerBase -ne $markerNow) {
        $fullReason = "MILESTONE_BOUNDARY:$markerBase->$markerNow"
    }
    elseif (-not $candidate.LastFullMain -or $null -eq $lastFullAge) {
        $fullReason = "NO_FULL_AUDIT_RECORD"
    }
    elseif (-not $deferFull -and $lastFullAge -ge $fullIntervalDays) {
        $fullReason = "PERIODIC_INTERVAL:$([math]::Round($lastFullAge, 1))d>=$($fullIntervalDays)d"
    }
    elseif (-not $deferFull -and $candidate.DeltaCount -ge $maxConsecutiveDelta) {
        $fullReason = "PERIODIC_MAX_DELTA:$($candidate.DeltaCount)>=$maxConsecutiveDelta"
    }
    else {
        if ($deferFull -and $lastFullAge -ge $fullIntervalDays) {
            $deferredNow.Add("PERIODIC_INTERVAL:$([math]::Round($lastFullAge, 1))d")
        }

        if ($deferFull -and $candidate.DeltaCount -ge $maxConsecutiveDelta) {
            $deferredNow.Add("PERIODIC_MAX_DELTA:$($candidate.DeltaCount)")
        }

        # 위험 경계 경로 트리거
        $changedPaths = @(
            Invoke-Git @("-C", $auditWorktree, "-c", "core.quotepath=false", "diff", "--no-renames", "--name-only", $candidate.Main, $mainHead)
        ) | Where-Object { $_ }

        foreach ($k in $fullTriggers.Keys) {
            if ($fullReason) { break }

            foreach ($cp in $changedPaths) {
                $hit = @($fullTriggers[$k] | Where-Object { "$cp" -match $_ })

                if ($hit.Count -gt 0) {
                    if ($deferFull -and $k -eq "MILESTONE_OR_GATE_CLOSEOUT") {
                        # 구축 기간: acceptance 문서는 Status 줄이 바뀐 closeout일 때만 즉시 FULL
                        $statusDiff = @(
                            Invoke-Git @("-C", $auditWorktree, "diff", "-U0", $candidate.Main, $mainHead, "--", "$cp")
                        ) | Where-Object { "$_" -match '^[+-]\s*Status:' }

                        if ($statusDiff.Count -eq 0 -and "$cp" -ne "app/__init__.py") {
                            $deferredNow.Add("TRIGGER_$($k)_EDIT:$cp")
                            continue
                        }
                    }
                    elseif ($deferFull) {
                        # 구축 기간: 기록만 하고 DELTA로 진행
                        $deferredNow.Add("TRIGGER_$($k):$cp")
                        break
                    }

                    $fullReason = "TRIGGER_$($k):$cp"
                    break
                }
            }
        }

        $roadmapPath = @("documents/roadmap/ROADMAP.md", "ROADMAP.md") | Where-Object { $changedPaths -contains $_ } | Select-Object -First 1

        if (-not $fullReason -and $roadmapPath) {
            $roadmapDiff = @(
                Invoke-Git @("-C", $auditWorktree, "diff", "-U0", $candidate.Main, $mainHead, "--", $roadmapPath)
            )

            if (@($roadmapDiff | Where-Object { "$_" -match $roadmapMilestoneLine }).Count -gt 0) {
                $fullReason = "TRIGGER_MILESTONE_OR_GATE_CLOSEOUT:ROADMAP.md milestone status"
            }
        }
    }

    if ($fullReason) {
        # 트리거 → FULL (아래 DELTA 분기로 가지 않음)
    }
    else {
        $auditMode = "DELTA"
        $baselineMain = $candidate.Main
        $lastFullMain = $candidate.LastFullMain
        $lastFullAt = $candidate.LastFullAt
        $deltaCount = $candidate.DeltaCount

        # baseline 보고서의 판정 헤더가 이번 출력 형식(AUDIT_MAIN=/VERDICT=)과 섞이지 않도록 이름을 바꾼다.
        $baselineReportText = (
            ($bReports -join "`n`n") `
                -replace '(?m)^AUDIT_MAIN=', 'BASELINE_AUDIT_MAIN=' `
                -replace '(?m)^VERDICT=', 'BASELINE_VERDICT=' `
                -replace '(?m)^SUMMARY=', 'BASELINE_SUMMARY='
        )
    }
}

if ($auditMode -eq "DELTA" -and $deferredNow.Count -gt 0) {
    $ledger = @()

    if (Test-Path $deferredPath) {
        try {
            $ledger = @((Get-Content $deferredPath -Raw -Encoding utf8 | ConvertFrom-Json).items)
        }
        catch {
            $ledger = @()
        }
    }

    $ledger = @($ledger | Where-Object { $_ -and [string]$_.main -ne $mainHead })

    foreach ($d in $deferredNow) {
        $ledger += [pscustomobject]@{ main = $mainHead; reason = $d; at = (Get-Date).ToString("o") }
    }

    [ordered]@{
        note = "FULL audits deferred during the build phase; run run-full-audit-v1.ps1 -ForceFull before any real use (LIVE/canary)"
        items = $ledger
    } | ConvertTo-Json -Depth 5 | Set-Content -Encoding utf8 $deferredPath

    foreach ($d in $deferredNow) {
        Write-Host "FULL_AUDIT_DEFERRED=$d"
    }

    Write-Host "FULL_AUDIT_DEFERRED_TOTAL=$($ledger.Count)"
}

if ($reusedBaseline) {
    Write-Host "AUDIT_MODE=$auditMode"
    Write-Host "AUDIT_REUSE=ALREADY_AUDITED_MAIN ($policyVersion)"
}
elseif ($auditMode -eq "DELTA") {
    $policyVersion = "$fullPolicy-delta"

    $deltaNames = (
        @(Invoke-Git @("-C", $auditWorktree, "-c", "core.quotepath=false", "diff", "--name-status", $baselineMain, $mainHead)) -join "`n"
    )

    Write-Host "AUDIT_MODE=DELTA"
    Write-Host "BASELINE_MAIN=$baselineMain ($($candidate.Source))"
    Write-Host "LAST_FULL_MAIN=$lastFullMain"
    Write-Host "LAST_FULL_AT=$lastFullAt"
    Write-Host "DELTA_COUNT_SINCE_FULL=$deltaCount/$maxConsecutiveDelta"
}
else {
    Write-Host "AUDIT_MODE=FULL"
    Write-Host "FULL_AUDIT_REASON=$fullReason"
}

Write-Host ""

# -------------------------------------------------
# Audit cache paths
# -------------------------------------------------

$mainShort = $mainHead.Substring(0,12)

$gptCache = Join-Path `
    $stateDir `
    "$policyVersion-main-$mainShort-gpt.txt"

$claudeCache = Join-Path `
    $stateDir `
    "$policyVersion-main-$mainShort-claude.txt"

$timestamp = Get-Date -Format "yyyyMMdd-HHmmss"

$gptErr = Join-Path `
    $logDir `
    "full-audit-main-$mainShort-gpt-$timestamp.stderr.txt"

$claudeErr = Join-Path `
    $logDir `
    "full-audit-main-$mainShort-claude-$timestamp.stderr.txt"

# -------------------------------------------------
# Shared audit contract
# -------------------------------------------------

$deltaScope = @"
CANONICAL MAIN:
$mainHead

REPOSITORY:
$repoSlug

ISSUE:
#$IssueNumber

HOST-VERIFIED FACTS:
- the working directory is a host-owned audit worktree checked out at exact canonical main
- that worktree is clean
- all GitHub check-runs for this exact main are completed SUCCESS
- baseline main $baselineMain already received a post-merge audit; its reports are below
- $baselineMain is an ancestor of this main; everything changed since then is exactly: git diff $baselineMain $mainHead
- that change consists of the PR(s) merged since the baseline (latest merged PR: #$MergedPr)

THIS IS A POST-MERGE DELTA AUDIT, NOT A FULL REPOSITORY RE-AUDIT.

Do ONLY the following:

1. BLOCKER CLOSURE
   - For every BLOCKER in the baseline reports, verify on this exact main whether it is resolved.
   - Inspect exactly the referenced files, symbols and contracts.

2. CHANGE AUDIT
   - Audit git diff $baselineMain $mainHead for new contradictions, weakened safety rules,
     stale language, scope violations or unauthorized implementation.
   - Check that changed canonical text is consistent with the canonical documents it references.

3. NO RE-AUDIT OF UNRELATED AREAS
   - Do not re-audit parts of the repository the change does not touch.
   - Baseline NONBLOCKING_GAPS stay nonblocking unless this change made them worse.
   - The baseline reports are findings about the previous main, not a verdict on this main.

CHANGED FILES SINCE BASELINE:
$deltaNames

BASELINE REPORTS:
$baselineReportText

RULES:
- READ ONLY. Do not modify repository files.
- Do not commit, push, merge, create branches or PRs.
- Do not call marketplace/provider APIs. Do not enable LIVE. Do not run a canary.
- Do not use commit messages as proof.
- You may use read-only local inspection commands such as rg, git log/show/diff and file reads.
- If evidence is insufficient to make a safe judgment, use INSUFFICIENT.

The first four output lines MUST be exactly:

AUDIT_MAIN=$mainHead
VERDICT=<PASS|BLOCKER|INSUFFICIENT>
SUMMARY=<one concise line>
ESCALATE_FULL=<NO|YES: one concise reason>

Set ESCALATE_FULL=YES when the change has cross-cutting impact that cannot be judged safely from the
changed files and what they directly reference (several owners, contracts or invariants affected, or
a shared runtime path whose other callers are outside this change). The host then runs a full
repository audit of this exact main. Otherwise ESCALATE_FULL=NO.

After those four lines, provide concise sections:

BLOCKERS:
- NONE
or numbered blockers (unresolved baseline blockers or new ones) with exact file/symbol/contract references.

BASELINE_BLOCKER_CLOSURE:
- one line per baseline blocker: RESOLVED or UNRESOLVED with evidence.

NONBLOCKING_GAPS:
- expected/deferred work only.

NEXT_ACTION:
- one concise action.
"@

$auditScope = @"
CANONICAL MAIN:
$mainHead

REPOSITORY:
$repoSlug

ISSUE:
#$IssueNumber

HOST-VERIFIED FACTS:
- the working directory is a host-owned audit worktree checked out at exact canonical main
- that worktree is clean
- all GitHub check-runs for this exact main are completed SUCCESS

THIS IS A POST-MERGE FULL AUDIT.

Audit the CURRENT MERGED REPOSITORY as a whole, not only the last PR.

Mandatory audit areas:

1. CANONICAL CONTRACT CONSISTENCY
   - CLAUDE.md and the rule files it imports under documents/rules/
   - documents/roadmap/ROADMAP.md and documents/roadmap/CURRENT-MILESTONE.md
   - documents/architecture/ARCHITECTURE.md
   - documents/architecture/GLOSSARY.md where relevant
   - relevant documents/decisions/adr/*
   - relevant documents/acceptance/*
   - repository rules/tests that pin those contracts
   - look for stale or contradictory old/new rules

2. MILESTONE / GATE TRUTH
   - M5 remains PENDING unless canonical evidence explicitly proves otherwise
   - M6 must not start prematurely
   - CREATE / SEARCH / provider-write adoption truth
   - DRY_RUN / LIVE boundaries
   - Gate-3 readiness and canary prerequisites
   - residual-risk acceptance requirements

3. REGISTRATION SAFETY
   - UNKNOWN never blindly resends CREATE
   - positive-only reconcile does not infer absence from zero results
   - presence proof is distinct from registration success
   - recovered provider identity and Snapshot verification remain separated
   - definitive rejection versus ambiguous outcome remains fail-closed

4. RUNTIME VS CONTRACT
   - detect runtime/schema/migration/routes that exceed current authorization
   - detect contracts that claim owners/features which runtime does not actually provide
   - check provider-zero/LIVE restrictions remain enforced

5. OWNER / DATA INTEGRITY
   - canonical owner boundaries
   - append-only/protected evidence where required
   - Product / Draft / PricingSnapshot / RegistrationIntent identity consistency
   - no UI-local second truth
   - no stale cross-product/context mixing path visible in current architecture

6. UI ↔ BACKEND CONTRACT
   - server-owned registration state partition
   - ambiguous outcomes must not appear as confirmed failures
   - status/readiness/count authority must come from canonical owners
   - no duplicated conflicting state machines

7. SCHEMA / MIGRATION INTEGRITY
   - migration chain consistency
   - schema-head assumptions
   - durable proof/evidence owners
   - no unauthorized migration implied by docs

8. TEST / PIN QUALITY
   - tests actually pin critical safety contracts
   - no obvious contradiction where tests encode stale semantics
   - deterministic CI success must not be mistaken for semantic correctness

9. EVIDENCE FRESHNESS
   - exact-SHA visual acceptance
   - restore/backup proof freshness
   - retention proof freshness
   - code identity/schema binding
   - a proof from an older main must not silently cover this main

10. CROSS-MERGE REGRESSION
   - interactions among recently merged registration/AI sequencing,
     extension-primary COLLECT transport, and safe-reconcile amendments
   - identify any contradiction introduced only after combining them on main

11. ROADMAP GAPS
   - distinguish documented missing work from a defect
   - do not call an explicitly deferred/not-authorized feature a blocker merely because it is absent
   - do flag false claims, unsafe behavior, contradictions, unauthorized implementation,
     stale acceptance, or broken owner boundaries

RULES:
- READ ONLY.
- Do not modify repository files.
- Do not commit, push, merge, create branches or PRs.
- Do not call marketplace/provider APIs.
- Do not enable LIVE.
- Do not run a canary.
- Do not perform marketplace mutation.
- Do not use commit messages as proof.
- Inspect actual canonical docs, implementation and tests.
- You may use read-only local inspection commands such as rg, git log/show/diff,
  file reads and directory inspection.
- Do not run commands that mutate repository state.
- Do not run the product against a real provider.
- If evidence is insufficient to make a safe judgment, use INSUFFICIENT.
- A documented stale proof is not automatically a code blocker if the canonical
  state correctly marks it stale and refuses to rely on it.
- Separate true BLOCKERS from expected/deferred roadmap gaps.

The first three output lines MUST be exactly:

AUDIT_MAIN=$mainHead
VERDICT=<PASS|BLOCKER|INSUFFICIENT>
SUMMARY=<one concise line>

After those three lines, provide concise sections:

BLOCKERS:
- NONE
or numbered blockers with exact file/symbol/contract references.

NONBLOCKING_GAPS:
- expected/deferred work only.

EVIDENCE_FRESHNESS:
- exact status of visual/restore/retention or other SHA-bound evidence.

NEXT_ACTION:
- one concise action.
"@

if ($auditMode -eq "DELTA") {
    $auditScope = $deltaScope
    $auditTitle = "POST-MERGE DELTA AUDIT"
}
else {
    $auditTitle = "POST-MERGE FULL REPOSITORY AUDIT"
}

# DELTA → FULL 자동 승격: cross-cutting 감지, INSUFFICIENT, 형식 오류
function Get-DeltaEscalation {
    param(
        [string]$Path,
        $Result
    )

    if ($auditMode -ne "DELTA") {
        return ""
    }

    if (-not $Result -or -not $Result.Valid) {
        return "DELTA_AUDIT_FAILURE"
    }

    if ($Result.Verdict -eq "INSUFFICIENT") {
        return "DELTA_INSUFFICIENT"
    }

    $t = if (Test-Path $Path) { Get-Content $Path -Raw -Encoding utf8 } else { "" }

    if ($t -match '(?m)^ESCALATE_FULL=YES') {
        return "DELTA_CROSS_CUTTING"
    }

    return ""
}

function Invoke-FullEscalation {
    param([string]$Reason)

    Write-Host ""
    Write-Host "FULL_AUDIT_ESCALATION=$Reason"
    Write-Host "NEXT=FULL_AUDIT_SAME_MAIN"

    & $selfPath -IssueNumber $IssueNumber -MergedPr $MergedPr -ForceFull -EscalationReason $Reason
}

# DELTA 실행 실패 → 다음 실행에서 같은 main을 FULL로 승격
function Save-PendingEscalation {
    param([string]$Reason)

    if ($auditMode -ne "DELTA") {
        return
    }

    [ordered]@{
        main = $mainHead
        reason = $Reason
        updated_at = (Get-Date).ToString("o")
    } | ConvertTo-Json | Set-Content -Encoding utf8 $escalationPath
}

# -------------------------------------------------
# GPT architect/verifier
# -------------------------------------------------

$gptResult = Read-AuditVerdict `
    -Path $gptCache `
    -ExpectedMain $mainHead

if ($gptResult -and $gptResult.Valid) {
    Write-Host "GPT_FULL_AUDIT_CACHE=HIT"
}
else {
    Write-Host "GPT_FULL_AUDIT_CACHE=MISS"
    Write-Host "GPT_MODEL=gpt-5.6-sol"
    Write-Host "GPT_REASONING=high"
    Write-Host ""

    $gptPrompt = @"
[ICBM-NEW] $auditTitle

ROLE:
You are the primary architect/verifier.

$auditScope
"@

    Remove-Item $gptErr -Force -ErrorAction SilentlyContinue

    $oldEap = $ErrorActionPreference

    try {
        $ErrorActionPreference = "Continue"

        $gptPrompt | codex exec `
            -C $auditWorktree `
            -s read-only `
            --ephemeral `
            --model gpt-5.6-sol `
            --config 'model_reasoning_effort="high"' `
            -o $gptCache `
            - `
            2> $gptErr

        $gptExit = $LASTEXITCODE
    }
    finally {
        $ErrorActionPreference = $oldEap
    }

    if ($gptExit -ne 0 -or -not (Test-Path $gptCache)) {
        Save-FullAuditState `
            -Status "TECHNICAL_HOLD" `
            -Main $mainHead `
            -Detail "GPT audit execution failed"

        Save-PendingEscalation -Reason "DELTA_GPT_EXEC_FAILED"
        Write-Output "FULL_AUDIT_HOLD=GPT_EXEC_FAILED"
        Write-Host "GPT_EXIT=$gptExit"
        return
    }

    Add-ToolStamp -Path $gptCache -Auditor "GPT" -Policy $policyVersion

    $gptResult = Read-AuditVerdict `
        -Path $gptCache `
        -ExpectedMain $mainHead
}

$gptEscalation = Get-DeltaEscalation -Path $gptCache -Result $gptResult

if ($gptEscalation) {
    Invoke-FullEscalation -Reason "GPT_$gptEscalation"
    return
}

if (-not $gptResult -or -not $gptResult.Valid) {
    Write-Output "FULL_AUDIT_HOLD=GPT_RESULT_INVALID"
    return
}

Write-Host ""
Write-Host "========================================"
Write-Host " GPT FULL AUDIT"
Write-Host "========================================"
Get-Content $gptCache -Encoding utf8 | Out-Host
Write-Host ""

# -------------------------------------------------
# Fresh independent Claude audit
# 토큰 절감: GPT가 PASS일 때만 실행한다 (PR 감사와 같은 방식).
# GPT BLOCKER → 어차피 remediation, GPT INSUFFICIENT → 어차피 HOLD.
# It must not consume GPT output.
# -------------------------------------------------

$claudeSkipped = ($gptResult.Verdict -ne "PASS")

if ($claudeSkipped) {
    Write-Host "CLAUDE_FULL_AUDIT=SKIPPED (GPT_$($gptResult.Verdict))"

    $claudeResult = [pscustomobject]@{
        Valid = $true
        Verdict = "SKIPPED"
        Summary = ""
    }
}
else {

$claudeResult = Read-AuditVerdict `
    -Path $claudeCache `
    -ExpectedMain $mainHead

if ($claudeResult -and $claudeResult.Valid) {
    Write-Host "CLAUDE_FULL_AUDIT_CACHE=HIT"
}
else {
    Write-Host "CLAUDE_FULL_AUDIT_CACHE=MISS"
    Write-Host ""

    $claudePrompt = @"
[ICBM-NEW] INDEPENDENT $auditTitle (CROSS-AUDIT)

ROLE:
You are the independent cross-auditor.

IMPORTANT INDEPENDENCE RULE:
- Do NOT read or search Agent Host GPT audit files.
- Do NOT assume the GPT verdict.
- Reach your own conclusion from the repository.
- This is a fresh session.

$auditScope
"@

    Remove-Item $claudeErr -Force -ErrorAction SilentlyContinue

    $oldEap = $ErrorActionPreference

    try {
        $ErrorActionPreference = "Continue"

        Push-Location $auditWorktree

        try {
            $claudeOutput = @(
                $claudePrompt |
                claude -p `
                    --no-session-persistence `
                    --restricted `
                    --permission-mode plan `
                    --permission-prompts none `
                    2> $claudeErr
            )

            $claudeExit = $LASTEXITCODE
        }
        finally {
            Pop-Location
        }
    }
    finally {
        $ErrorActionPreference = $oldEap
    }

    if ($claudeExit -ne 0) {
        Save-FullAuditState `
            -Status "TECHNICAL_HOLD" `
            -Main $mainHead `
            -GptVerdict $gptResult.Verdict `
            -Detail "Claude audit execution failed"

        Save-PendingEscalation -Reason "DELTA_CLAUDE_EXEC_FAILED"
        Write-Output "FULL_AUDIT_HOLD=CLAUDE_EXEC_FAILED"
        Write-Host "CLAUDE_EXIT=$claudeExit"

        if (Test-Path $claudeErr) {
            Get-Content $claudeErr -Tail 20 |
                ForEach-Object {
                    Write-Host "CLAUDE_STDERR=$_"
                }
        }

        return
    }

    $claudeOutput |
        Set-Content -Encoding utf8 $claudeCache

    Add-ToolStamp -Path $claudeCache -Auditor "CLAUDE" -Policy $policyVersion

    $claudeResult = Read-AuditVerdict `
        -Path $claudeCache `
        -ExpectedMain $mainHead
}

if (-not $claudeSkipped) {
    $claudeEscalation = Get-DeltaEscalation -Path $claudeCache -Result $claudeResult

    if ($claudeEscalation) {
        Invoke-FullEscalation -Reason "CLAUDE_$claudeEscalation"
        return
    }
}

if (-not $claudeResult -or -not $claudeResult.Valid) {
    Write-Output "FULL_AUDIT_HOLD=CLAUDE_RESULT_INVALID"
    return
}

Write-Host ""
Write-Host "========================================"
Write-Host " CLAUDE FULL CROSS-AUDIT"
Write-Host "========================================"
Get-Content $claudeCache -Encoding utf8 | Out-Host
Write-Host ""

}

# -------------------------------------------------
# Auditors must not have modified main.
# -------------------------------------------------

$dirtyAfter = Invoke-Git @("-C", $auditWorktree, "status", "--porcelain")
$localAfter = "$(Invoke-Git @('-C', $auditWorktree, 'rev-parse', 'HEAD'))".Trim()
$mainNow = Get-RemoteMain

if ($dirtyAfter) {
    Save-FullAuditState `
        -Status "TECHNICAL_HOLD" `
        -Main $mainHead `
        -GptVerdict $gptResult.Verdict `
        -ClaudeVerdict $claudeResult.Verdict `
        -Detail "Auditor modified worktree"

    Write-Output "FULL_AUDIT_HOLD=AUDITOR_MODIFIED_WORKTREE"
    return
}

if (
    $localAfter -ne $mainHead -or
    $mainNow -ne $mainHead
) {
    Save-FullAuditState `
        -Status "STALE" `
        -Main $mainHead `
        -GptVerdict $gptResult.Verdict `
        -ClaudeVerdict $claudeResult.Verdict `
        -Detail "Main moved during audit"

    Write-Output "FULL_AUDIT_RESULT=STALE_MAIN_MOVED"
    Write-Host "START_MAIN=$mainHead"
    Write-Host "LOCAL_NOW=$localAfter"
    Write-Host "REMOTE_NOW=$mainNow"
    return
}

# -------------------------------------------------
# Final classification
# -------------------------------------------------

Write-Host ""
Write-Host "========================================"
Write-Host " FULL AUDIT RESULT"
Write-Host "========================================"
Write-Host "MAIN=$mainHead"
Write-Host "AUDIT_MODE=$auditMode"
Write-Host "AUDIT_TOOL_SHA256=$script:auditToolSha"
Write-Host "GPT_VERDICT=$($gptResult.Verdict)"
Write-Host "CLAUDE_VERDICT=$($claudeResult.Verdict)"
Write-Host ""

if (
    $gptResult.Verdict -eq "INSUFFICIENT" -or
    $claudeResult.Verdict -eq "INSUFFICIENT"
) {
    Save-FullAuditState `
        -Status "TECHNICAL_HOLD" `
        -Main $mainHead `
        -GptVerdict $gptResult.Verdict `
        -ClaudeVerdict $claudeResult.Verdict `
        -Detail "At least one auditor returned INSUFFICIENT"

    Write-Output "FULL_AUDIT_RESULT=TECHNICAL_HOLD_INSUFFICIENT"
    Write-Host "IMPLEMENTATION_AUTHORIZED=FALSE"
    Write-Host "FULL_AUDIT_MERGES=NONE (merge decisions belong to orchestrator MERGE_GUARD)"
    return
}

if (
    $gptResult.Verdict -eq "BLOCKER" -or
    $claudeResult.Verdict -eq "BLOCKER"
) {
    Save-FullAuditState `
        -Status "BLOCKED" `
        -Main $mainHead `
        -GptVerdict $gptResult.Verdict `
        -ClaudeVerdict $claudeResult.Verdict `
        -Detail "Full-audit blocker(s) require remediation PR"

    Save-AuditBaseline -Status "BLOCKED"

    Write-Output "FULL_AUDIT_RESULT=BLOCKED"
    Write-Output "NEXT=REMEDIATION_PR"
    Write-Host "IMPLEMENTATION_AUTHORIZED=FALSE"
    Write-Host "FULL_AUDIT_MERGES=NONE (merge decisions belong to orchestrator MERGE_GUARD)"
    Write-Host "GPT_REPORT=$gptCache"
    Write-Host "CLAUDE_REPORT=$claudeCache"
    return
}

Save-FullAuditState `
    -Status "DUAL_PASS" `
    -Main $mainHead `
    -GptVerdict "PASS" `
    -ClaudeVerdict "PASS" `
    -Detail "Post-merge full audit dual pass"

Save-AuditBaseline -Status "DUAL_PASS"

Write-Output "FULL_AUDIT_RESULT=DUAL_PASS"
Write-Output "NEXT=NEXT_1_AUTHORIZATION_REVIEW"
Write-Host "IMPLEMENTATION_AUTHORIZED=FALSE"
Write-Host "FULL_AUDIT_MERGES=NONE (merge decisions belong to orchestrator MERGE_GUARD)"
Write-Host "GPT_REPORT=$gptCache"
Write-Host "CLAUDE_REPORT=$claudeCache"
