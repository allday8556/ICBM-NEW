param(
    [Parameter(Mandatory=$true)]
    [int]$CurrentPr,

    [int]$PollSeconds = 60,

    [int]$MaxWaitMinutes = 360,

    # first wait before a TECHNICAL_HOLD is retried; doubles per repeat of the same state (default: PollSeconds)
    [int]$TechnicalBackoffSeconds = 0
)

$ErrorActionPreference = "Stop"

$nativeUtf8 = New-Object System.Text.UTF8Encoding($false)
$script:OutputEncoding = $nativeUtf8
[Console]::OutputEncoding = $nativeUtf8

$hostRoot = $PSScriptRoot

# §3 marker grammar + authority write guard (every Host GitHub write goes through Invoke-GhWrite) and the §5.1 hold taxonomy
. (Join-Path $PSScriptRoot "agent-host-authority-v2.ps1")

$configPath = Join-Path $hostRoot "state\orchestrator-config.json"
$stateScript = Join-Path $hostRoot "orchestrator-v1.2.ps1"
$auditScript = Join-Path $hostRoot "run-audit-v1.1.ps1"
$repairScript = Join-Path $hostRoot "run-repair-v1.1.ps1"
$fullAuditScript = Join-Path $hostRoot "run-full-audit-v1.ps1"
$nextSelectScript = Join-Path $hostRoot "run-lookahead-main-v1.ps1"
$lookaheadScript = Join-Path $hostRoot "run-lookahead-v1.ps1"
$runtimeStatePath = Join-Path $hostRoot "state\orchestrator-runtime.json"

$config = Get-Content $configPath -Raw -Encoding utf8 |
    ConvertFrom-Json

$repoSlug = [string]$config.repository

# MERGE_GUARD를 통과한 DUAL PASS PR에 한해 자동 병합 (protocol §0.2: the default operating mode).
# config.auto_merge=false 는 host 설정에 의한 정지 (WAITING_FOR_MERGE_BY_CONFIG) 이며 hold 가 아니다.
$autoMerge = ($config.auto_merge -eq $true)
$autoMergeMethod = if ($config.auto_merge_method) { [string]$config.auto_merge_method } else { "merge" }

# full-main DUAL PASS 이후 ROADMAP 재조회 → 다음 slice 자동 진행
$autoNext = ($config.auto_next.enabled -eq $true)

# TECHNICAL_HOLD self-recovery (protocol §5.1): the same (PR, HEAD, main, reason) is retried with a doubling wait.
# The ceiling is a cost circuit breaker, never a hand-off for a decision: it ends the run as TECHNICAL_HOLD_EXHAUSTED.
$technicalMaxRetries = if ($config.technical_hold.max_same_state_retries) { [int]$config.technical_hold.max_same_state_retries } else { 5 }
$technicalBackoff = if ($TechnicalBackoffSeconds -gt 0) { $TechnicalBackoffSeconds } else { $PollSeconds }
$technicalBackoffMax = if ($config.technical_hold.max_backoff_seconds) { [int]$config.technical_hold.max_backoff_seconds } else { 900 }
$script:lastHold = $null
$logDir = Join-Path $hostRoot "logs"

# 마지막 자동 병합 SHA (post-merge full audit의 exact main 확인용)
$lastMergeSha = ""

$deadline = (Get-Date).AddMinutes($MaxWaitMinutes)

# 이 실행의 audit toolset: host 스크립트별 SHA-256 + 전체 세트 해시
$toolsetFiles = @(
    "orchestrator-v1.3.ps1", "orchestrator-v1.2.ps1", "run-audit-v1.1.ps1",
    "run-repair-v1.1.ps1", "run-full-audit-v1.ps1", "run-lookahead-main-v1.ps1",
    "resume-orchestrator-v1.3.ps1", "run-lookahead-v1.ps1", "agent-host-authority-v2.ps1"
)

$toolsetLines = @(
    foreach ($tf in $toolsetFiles) {
        $tp = Join-Path $hostRoot $tf

        if (Test-Path $tp) {
            "$tf=$((Get-FileHash -Algorithm SHA256 -LiteralPath $tp).Hash.ToLower())"
        }
        else {
            "$tf=MISSING"
        }
    }
)

$sha = [System.Security.Cryptography.SHA256]::Create()
$toolsetSha = (
    [System.BitConverter]::ToString(
        $sha.ComputeHash([System.Text.Encoding]::UTF8.GetBytes(($toolsetLines -join "`n")))
    ) -replace "-", ""
).ToLower()

# 같은 PR HEAD + main 조합에서 Lookahead AI 호출은 한 번만.
# HEAD/main이 바뀌면 새 key가 되어 다시 준비한다.
$lookaheadAttemptedKey = $null

function Save-RuntimeState {
    param(
        [string]$Status,
        [string]$Action,
        [string]$PrHead = "",
        [string]$MainHead = "",
        [string]$Detail = ""
    )

    $runtime = [ordered]@{
        version = "1.3"
        current_pr = $CurrentPr
        status = $Status
        action = $Action
        pr_head = $PrHead
        main_head = $MainHead
        detail = $Detail
        auto_merge = $autoMerge
        auto_next = $autoNext
        # exactly two classes (protocol §5.1). An exhausted technical hold is still a TECHNICAL_HOLD: the status says
        # the circuit breaker ended the run, the class does not change.
        hold_class = $(if ($Status -eq "HUMAN_DECISION_REQUIRED") { "HUMAN_DECISION_REQUIRED" } elseif ($Status -in @("TECHNICAL_HOLD", "TECHNICAL_HOLD_EXHAUSTED")) { "TECHNICAL_HOLD" } else { "NONE" })
        toolset_sha256 = $toolsetSha
        updated_at = (Get-Date).ToString("o")
    }

    $tmp = "$runtimeStatePath.tmp"

    $runtime |
        ConvertTo-Json -Depth 10 |
        Set-Content -Encoding utf8 $tmp

    Move-Item $tmp $runtimeStatePath -Force
}

# A pass stops in exactly one of two classes (protocol §5.1; agent-host-authority-v2.ps1 Get-HoldClass):
#   HUMAN_DECISION_REQUIRED : the closed product / real-external-action list. The run ends and waits for the user.
#   TECHNICAL_HOLD          : everything else. The supervisor below retries it; nobody is asked anything.
# The class comes from the reason's category, never from how often something failed.
function Stop-Hold {
    param(
        [string]$Reason,
        [string]$PrHead = "",
        [string]$MainHead = "",
        [string]$Detail = ""
    )

    $class = Get-HoldClass $Reason

    Save-RuntimeState `
        -Status $class `
        -Action $Reason `
        -PrHead $PrHead `
        -MainHead $MainHead `
        -Detail $Detail

    $script:lastHold = [pscustomobject]@{ Class = $class; Reason = $Reason; PrHead = $PrHead; MainHead = $MainHead; Detail = $Detail }

    Write-Host ""
    Write-Host "HOLD_CLASS=$class"
    Write-Host "$class=$Reason"

    # 구축 기간 동안 보류된 전수 감사 — 실사용 전 필요
    $deferredPath = Join-Path $hostRoot "state\full-audit-deferred.json"

    if (Test-Path $deferredPath) {
        try {
            $n = @((Get-Content $deferredPath -Raw -Encoding utf8 | ConvertFrom-Json).items).Count

            if ($n -gt 0) {
                Write-Host "PRE_REAL_USE_FULL_AUDIT_PENDING=$n (run-full-audit-v1.ps1 -ForceFull before any LIVE/canary)"
            }
        }
        catch {
        }
    }
}

# A configured or finished stop: not a hold, nothing to retry, nothing to decide.
function Stop-Idle {
    param(
        [string]$Status,
        [string]$Action,
        [string]$PrHead = "",
        [string]$MainHead = "",
        [string]$Detail = ""
    )

    Save-RuntimeState -Status $Status -Action $Action -PrHead $PrHead -MainHead $MainHead -Detail $Detail
    $script:lastHold = $null
    Write-Host ""
    Write-Host "STATE=$Status ($Action)"
}

# 하위 스크립트의 모든 stream(output/error/warning/information)을 캡처하면서 화면에도 표시.
# PS5.1 + EAP=Stop 에서 하위 native stderr가 caller를 중단시키지 않도록 EAP=Continue.
function Invoke-HostScript {
    param(
        [string]$Path,
        [hashtable]$Params = @{}
    )

    $lines = New-Object System.Collections.Generic.List[string]
    $oldEap = $ErrorActionPreference

    try {
        $ErrorActionPreference = "Continue"

        & $Path @Params *>&1 |
            ForEach-Object {
                $s = "$_"

                # Write-Host(InformationRecord)는 이미 화면에 표시되므로 캡처만 한다.
                if (-not ($_ -is [System.Management.Automation.InformationRecord])) {
                    Write-Host $s
                }

                $lines.Add($s)
            }
    }
    catch {
        $msg = "HOST_SCRIPT_EXCEPTION=$($_.Exception.Message)"
        Write-Host $msg
        $lines.Add($msg)
    }
    finally {
        $ErrorActionPreference = $oldEap
    }

    return ($lines.ToArray() -join "`n")
}

function Get-Pr {
    gh pr view $CurrentPr `
        --repo $repoSlug `
        --json number,title,state,isDraft,mergedAt,headRefOid,headRefName,baseRefName |
        ConvertFrom-Json
}

# audit-before-CI: 감사/수정 중에는 draft (FULL CI 없음), DUAL PASS HEAD 에서만 ready (FULL CI 1회)
function Set-PrDraft {
    param([bool]$Draft)

    $oldEap = $ErrorActionPreference

    try {
        $ErrorActionPreference = "Continue"
        # 모든 Host GitHub write 는 authority write guard 경유 (V2 §3)
        $ghArgs = if ($Draft) { @("pr", "ready", "$CurrentPr", "--repo", $repoSlug, "--undo") } else { @("pr", "ready", "$CurrentPr", "--repo", $repoSlug) }
        $w = Invoke-GhWrite -GhArgs $ghArgs -Site "orchestrator:Set-PrDraft"
        $code = $w.ExitCode
    }
    finally {
        $ErrorActionPreference = $oldEap
    }

    Write-Host "PR_DRAFT_SET=$Draft (exit=$code)"
    return ($code -eq 0)
}

function Stop-DuplicateFullCi {
    param(
        [string]$Head,
        [datetime]$AfterUtc
    )

    $dupPath = Join-Path $hostRoot "state\ci-duplicate-cancelled-$($Head.Substring(0,12)).json"
    $cancelled = New-Object System.Collections.Generic.List[object]

    for ($i = 0; $i -lt 12 -and $cancelled.Count -eq 0; $i++) {
        Start-Sleep -Seconds 15
        $oldEap = $ErrorActionPreference

        try {
            $ErrorActionPreference = "Continue"
            $raw = gh api "repos/$repoSlug/actions/runs?head_sha=$Head&event=pull_request&per_page=50" 2>$null
        }
        finally {
            $ErrorActionPreference = $oldEap
        }

        $runs = @()
        try { $runs = @(($raw | ConvertFrom-Json).workflow_runs) } catch { $runs = @() }

        foreach ($r in $runs) {
            $created = [datetime]::Parse([string]$r.created_at).ToUniversalTime()
            if ($created -lt $AfterUtc.AddSeconds(-5)) { continue }

            $oldEap = $ErrorActionPreference
            try {
                $ErrorActionPreference = "Continue"
                [void](Invoke-GhWrite -GhArgs @("run", "cancel", "$($r.id)", "--repo", $repoSlug) -Site "orchestrator:Stop-DuplicateFullCi")
            }
            finally {
                $ErrorActionPreference = $oldEap
            }

            $cancelled.Add([pscustomobject]@{ run_id = [string]$r.id; check_suite_id = [string]$r.check_suite_id; created_at = [string]$r.created_at })
            Write-Host "CI_DUPLICATE_CANCELLED=run $($r.id) (suite $($r.check_suite_id))"
        }
    }

    if ($cancelled.Count -gt 0) {
        [ordered]@{
            head = $Head
            reason = "exact HEAD already had a GREEN FULL CI; ready_for_review started a duplicate FULL CI, cancelled per owner instruction (no FULL CI re-run on the same HEAD)"
            runs = $cancelled.ToArray()
            check_suite_ids = @($cancelled.ToArray() | ForEach-Object { $_.check_suite_id })
            cancelled_at = (Get-Date).ToString("o")
        } | ConvertTo-Json -Depth 5 | Set-Content -Encoding utf8 $dupPath
    }
    else {
        Write-Host "CI_DUPLICATE_NOT_SEEN (no ready_for_review run appeared within 3 minutes)"
    }
}

function Get-MainHead {
    $sha = gh api "repos/$repoSlug/commits/main" --jq .sha
    return "$sha".Trim()
}

# §7 condition 8: how many commits of the current base the PR HEAD does not contain. -1 = unreadable.
function Get-BehindBy {
    param([string]$Base, [string]$Head)

    $oldEap = $ErrorActionPreference

    try {
        $ErrorActionPreference = "Continue"
        $raw = gh api "repos/$repoSlug/compare/$Base...$Head" --jq .behind_by 2>$null
        $code = $LASTEXITCODE
    }
    finally {
        $ErrorActionPreference = $oldEap
    }

    if ($code -ne 0 -or "$raw".Trim() -notmatch '^\d+$') {
        return -1
    }

    return [int]"$raw".Trim()
}

# §8: the tree a commit points at. Empty = unreadable.
function Get-CommitTree {
    param([string]$Sha)

    $oldEap = $ErrorActionPreference

    try {
        $ErrorActionPreference = "Continue"
        $raw = gh api "repos/$repoSlug/git/commits/$Sha" --jq .tree.sha 2>$null
        $code = $LASTEXITCODE
    }
    finally {
        $ErrorActionPreference = $oldEap
    }

    if ($code -ne 0 -or "$raw".Trim() -notmatch '^[0-9a-f]{40}$') {
        return ""
    }

    return "$raw".Trim()
}

# §8 POST_MERGE_VERIFY: the merge commit's tree equals the merged PR HEAD's tree, whatever the merge commit's
# metadata. Read from GitHub's record of the merge on every pass that finds the PR merged, so nothing local can
# make it look verified. A mismatch is never accepted silently and never lets the next slice start.
function Test-PostMergeTree {
    param([string]$MergedHead)

    $info = Get-PrMergeInfo
    $mergeSha = [string]$info.mergeCommit.oid

    if (-not $mergeSha) {
        Write-Host "POST_MERGE_VERIFY=FAIL (no merge commit recorded)"
        return [pscustomobject]@{ Ok = $false; Detail = "merge_commit=NONE" }
    }

    $mergedTree = Get-CommitTree -Sha $mergeSha
    $auditedTree = Get-CommitTree -Sha $MergedHead
    Write-Host "POST_MERGE_MERGE_COMMIT=$mergeSha"
    Write-Host "POST_MERGE_MERGED_TREE=$mergedTree"
    Write-Host "POST_MERGE_AUDITED_TREE=$auditedTree"

    if (-not $mergedTree -or $mergedTree -ne $auditedTree) {
        Write-Host "POST_MERGE_VERIFY=FAIL"
        return [pscustomobject]@{ Ok = $false; Detail = "merge_sha=$mergeSha;merged_tree=$mergedTree;audited_tree=$auditedTree" }
    }

    Write-Host "POST_MERGE_VERIFY=PASS"
    return [pscustomobject]@{ Ok = $true; Detail = "merge_sha=$mergeSha;tree=$mergedTree" }
}

function Get-AuditPolicyVersion {
    $auditText = Get-Content $auditScript -Raw -Encoding utf8

    $m = [regex]::Match(
        $auditText,
        '\$auditPolicyVersion\s*=\s*"([^"]+)"'
    )

    if (-not $m.Success) {
        throw "AUDIT_POLICY_VERSION_NOT_FOUND"
    }

    return $m.Groups[1].Value
}

function Get-CiState {
    param(
        [string]$Head
    )

    $raw = gh api `
        "repos/$repoSlug/commits/$Head/check-runs?per_page=100"

    $data = $raw | ConvertFrom-Json
    $checks = @($data.check_runs)

    # 같은 exact HEAD 에 이미 FULL CI GREEN 이 있어 host 가 취소한 중복 실행(ready_for_review 재실행)은 판정에서 제외
    $dupPath = Join-Path $hostRoot "state\ci-duplicate-cancelled-$($Head.Substring(0,12)).json"
    if (Test-Path $dupPath) {
        $dupSuites = @((Get-Content $dupPath -Raw -Encoding utf8 | ConvertFrom-Json).check_suite_ids | ForEach-Object { [string]$_ })
        $checks = @($checks | Where-Object { $dupSuites -notcontains [string]$_.check_suite.id })
    }

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

# CI 실패 시 실패한 job만 1회 재실행 (flaky 대응). 같은 HEAD에서 두 번째 실패는 TECHNICAL_HOLD (CI_FAILED).
# 반환: WAIT (재실행 요청 또는 run 진행 중) | HOLD
function Invoke-CiRerunOnce {
    param([string]$Head)

    $markerPath = Join-Path $hostRoot "state\ci-rerun-$($Head.Substring(0,12)).json"

    if (Test-Path $markerPath) {
        return "HOLD"
    }

    $oldEap = $ErrorActionPreference

    try {
        $ErrorActionPreference = "Continue"
        $raw = gh api "repos/$repoSlug/actions/runs?head_sha=$Head&per_page=50" 2>$null
    }
    finally {
        $ErrorActionPreference = $oldEap
    }

    $runs = @()

    try {
        $runs = @(($raw | ConvertFrom-Json).workflow_runs)
    }
    catch {
        $runs = @()
    }

    if (@($runs | Where-Object { $_.status -ne "completed" }).Count -gt 0) {
        # 다른 job이 끝나야 --failed 재실행이 가능하다
        return "WAIT"
    }

    $failedRuns = @($runs | Where-Object { $_.conclusion -in @("failure", "timed_out", "cancelled") })

    if ($failedRuns.Count -eq 0) {
        return "HOLD"
    }

    $rerun = @()

    foreach ($r in $failedRuns) {
        $oldEap = $ErrorActionPreference

        try {
            $ErrorActionPreference = "Continue"
            $w = Invoke-GhWrite -GhArgs @("run", "rerun", "$($r.id)", "--repo", $repoSlug, "--failed") -Site "orchestrator:Invoke-CiRerunOnce"
            $code = $w.ExitCode
        }
        finally {
            $ErrorActionPreference = $oldEap
        }

        if ($code -eq 0) {
            $rerun += [string]$r.id
        }
    }

    if ($rerun.Count -eq 0) {
        return "HOLD"
    }

    [ordered]@{
        head = $Head
        rerun_run_ids = $rerun
        at = (Get-Date).ToString("o")
    } | ConvertTo-Json | Set-Content -Encoding utf8 $markerPath

    Write-Host "CI_RERUN_FAILED_JOBS=$($rerun -join ',')"
    return "WAIT"
}

# AGENT_HOST_PROTOCOL_V2 audit identity = (exact HEAD, packet_digest).
# run-audit 가 이 HEAD 에 대해 마지막으로 발급한 packet 포인터 (state\packets\pr-<N>-head-<12>.current).
# HEAD/main/policy 가 다르거나 source manifest 가 그 뒤에 바뀌었으면 포인터는 무효 ($null).
function Get-PacketPointer {
    param(
        [string]$MainHead,
        [string]$PrHead
    )

    $policy = Get-AuditPolicyVersion
    $ptrPath = Join-Path $hostRoot "state\packets\pr-$CurrentPr-head-$($PrHead.Substring(0,12)).current"

    if (-not (Test-Path $ptrPath)) {
        return $null
    }

    $kv = @{}

    foreach ($line in @(Get-Content $ptrPath -Encoding utf8)) {
        $m = [regex]::Match("$line", '^([A-Z0-9_]+)=(.*)$')

        if ($m.Success) {
            $kv[$m.Groups[1].Value] = $m.Groups[2].Value.Trim()
        }
    }

    if (
        $kv["HEAD"] -ne $PrHead -or
        $kv["MAIN"] -ne $MainHead -or
        $kv["POLICY_VERSION"] -ne $policy -or
        "$($kv['PACKET_DIGEST'])" -notmatch '^[0-9a-f]{64}$'
    ) {
        return $null
    }

    $manifestPath = Join-Path $hostRoot "state\audit-sources-pr-$CurrentPr.json"
    $manifestSha = if (Test-Path $manifestPath) { (Get-FileHash -Algorithm SHA256 -LiteralPath $manifestPath).Hash.ToLower() } else { "NONE" }

    if ($kv["SOURCE_MANIFEST_SHA256"] -ne $manifestSha) {
        return $null
    }

    # required 목록은 digest 에 묶인 불변 packet manifest 에서 읽는다
    $digest = $kv["PACKET_DIGEST"]
    $pmPath = Join-Path $hostRoot "state\packets\pr-$CurrentPr-head-$($PrHead.Substring(0,12))-$($digest.Substring(0,12)).manifest.json"

    if (-not (Test-Path $pmPath)) {
        return $null
    }

    $pm = Get-Content $pmPath -Raw -Encoding utf8 | ConvertFrom-Json

    if ([string]$pm.packet_digest -ne $digest -or [string]$pm.exact_pr_head -ne $PrHead) {
        return $null
    }

    return [pscustomobject]@{
        Digest = $digest
        Required = @($pm.required | ForEach-Object { [string]$_ } | Where-Object { $_ })
    }
}

# -CacheOnly: PASS 캐시 파일만 (DUAL PASS 캐시 판정 / MERGE_GUARD). 기본: 이번 run-audit 결과(.result.txt) 우선.
# 캐시 파일(<key>-gpt.txt)은 run-audit 가 PASS + evidence coverage 일 때만 쓴다. BLOCKER/INSUFFICIENT/HOLD 는 .result.txt 에만 있다.
function Get-AuditResult {
    param(
        [ValidateSet("GPT","CLAUDE")]
        [string]$Source,

        [string]$MainHead,

        [string]$PrHead,

        [switch]$CacheOnly
    )

    $policy = Get-AuditPolicyVersion

    $ptr = Get-PacketPointer -MainHead $MainHead -PrHead $PrHead

    if (-not $ptr) {
        return $null
    }

    $mainShort = $MainHead.Substring(0,12)
    $headShort = $PrHead.Substring(0,12)

    $cacheKey = "$policy-pr-$CurrentPr-main-$mainShort-head-$headShort-pkt-$($ptr.Digest.Substring(0,12))"

    $suffix = if ($Source -eq "GPT") {
        "gpt"
    }
    else {
        "claude"
    }

    $cachePath = Join-Path $hostRoot "state\$cacheKey-$suffix.txt"
    $resultPath = Join-Path $hostRoot "state\$cacheKey-$suffix.result.txt"

    $path = if ($CacheOnly) {
        $cachePath
    }
    elseif (Test-Path $resultPath) {
        $resultPath
    }
    else {
        $cachePath
    }

    if (-not (Test-Path $path)) {
        return $null
    }

    $text = Get-Content $path -Raw -Encoding utf8

    $headMatch = [regex]::Match(
        $text,
        '(?m)^AUDIT_HEAD=(.+?)\s*$'
    )

    $verdictMatch = [regex]::Match(
        $text,
        '(?m)^VERDICT=(PASS|BLOCKER|INSUFFICIENT|HOLD|HUMAN_DECISION_REQUIRED)\s*$'
    )

    $summaryMatch = [regex]::Match(
        $text,
        '(?m)^SUMMARY=(.+?)\s*$'
    )

    $digestMatch = [regex]::Match(
        $text,
        '(?m)^PACKET_DIGEST=([0-9a-f]{64})\s*$'
    )

    $seenMatch = [regex]::Match(
        $text,
        '(?m)^EVIDENCE_SEEN=(.*?)\s*$'
    )

    $seen = if ($seenMatch.Success) {
        @($seenMatch.Groups[1].Value -split ',' | ForEach-Object { $_.Trim() } | Where-Object { $_ -and $_ -ne 'NONE' })
    }
    else {
        @()
    }

    $evidenceSeenOk = (@($ptr.Required | Where-Object { $seen -cnotcontains $_ }).Count -eq 0)

    if (
        -not $headMatch.Success -or
        -not $verdictMatch.Success
    ) {
        return [pscustomobject]@{
            Source = $Source
            Valid = $false
            Path = $path
            Head = $null
            Verdict = $null
            Summary = $null
            Digest = $null
            EvidenceSeenOk = $false
        }
    }

    return [pscustomobject]@{
        Source = $Source
        Valid = (
            $headMatch.Groups[1].Value.Trim() -eq $PrHead -and
            $digestMatch.Success -and
            $digestMatch.Groups[1].Value -eq $ptr.Digest
        )
        Path = $path
        Head = $headMatch.Groups[1].Value.Trim()
        Verdict = $verdictMatch.Groups[1].Value.Trim()
        Summary = if ($summaryMatch.Success) {
            $summaryMatch.Groups[1].Value.Trim()
        }
        else {
            ""
        }
        Digest = if ($digestMatch.Success) { $digestMatch.Groups[1].Value } else { $null }
        EvidenceSeenOk = $evidenceSeenOk
    }
}

# I4: orchestrator-v1.2.ps1 은 display/state helper 전용 (state\orchestrator-state.json 의 NEXT 슬롯 표시, 현재 상태 출력).
# V2 authority 없음: 그 출력과 orchestrator-state.json 은 packet/cache/verdict/merge 판정에 쓰이지 않는다.
# identity 를 고정해 두고, 다르면 실행하지 않는다 (non-blocking; 판정에 영향 없음). 출력은 화면으로만 보낸다.
$stateScriptPinnedSha = "1138fd4d21a49595b5bb862098ce04c96195a3af23fd0b506e715583d2ca299c"

function Invoke-StateRefresh {
    try {
        $actual = (Get-FileHash -Algorithm SHA256 -LiteralPath $stateScript).Hash.ToLower()

        if ($actual -ne $stateScriptPinnedSha) {
            Write-Host "STATE_REFRESH_SKIPPED=STATE_HELPER_IDENTITY_MISMATCH ($actual)"
            return
        }

        & $stateScript -CurrentPr $CurrentPr | Out-Host
    }
    catch {
        Write-Host "STATE_REFRESH_WARNING=$($_.Exception.Message)"
    }
}

function Invoke-LookaheadOnce {
    param(
        [string]$MainHead,
        [string]$PrHead
    )

    $key = "$MainHead`:$PrHead"

    if ($script:lookaheadAttemptedKey -eq $key) {
        Write-Host "LOOKAHEAD=CACHED_FOR_CURRENT_HEAD"
        return
    }

    # 토큰 폭주 방지: 같은 exact HEAD/main에서는 1회만 시도.
    $script:lookaheadAttemptedKey = $key

    if (-not (Test-Path $lookaheadScript)) {
        Write-Host "LOOKAHEAD_WARNING=WORKER_NOT_FOUND"
        return
    }

    Write-Host ""
    Write-Host "========================================"
    Write-Host " LOOKAHEAD NEXT-1..NEXT-3"
    Write-Host "========================================"
    Write-Host ""

    try {
        $lookaheadLines = @(
            & $lookaheadScript `
                -CurrentPr $CurrentPr *>&1
        )

        foreach ($line in $lookaheadLines) {
            Write-Host "$line"
        }

        $lookaheadText = (
            $lookaheadLines |
            ForEach-Object { "$_" }
        ) -join "`n"

        if (
            $lookaheadText -match
            '(?m)^LOOKAHEAD_RESULT=(PREPARED|ALREADY_FRESH)\s*$'
        ) {
            Write-Host "LOOKAHEAD_STATUS=READY"
            return
        }

        if ($lookaheadText -match 'LOOKAHEAD_HOLD=') {
            # 다음 작업 준비 실패/모호성은 CURRENT PR을 막지 않는다.
            # 현재 PR 감사/수정/CI는 계속 진행한다.
            Write-Host "LOOKAHEAD_STATUS=HOLD_NONBLOCKING"
            return
        }

        Write-Host "LOOKAHEAD_STATUS=UNKNOWN_NONBLOCKING"
    }
    catch {
        # Lookahead는 보조 작업.
        # 실패해도 CURRENT PR 파이프라인은 중단하지 않는다.
        Write-Host "LOOKAHEAD_WARNING=$($_.Exception.Message)"
    }
}

function Invoke-Repair {
    param(
        [ValidateSet("GPT","CLAUDE")]
        [string]$Source,

        [string]$OldHead,

        [string]$MainHead
    )

    Save-RuntimeState `
        -Status "REPAIRING" `
        -Action "$Source BLOCKER FIX" `
        -PrHead $OldHead `
        -MainHead $MainHead

    Write-Host ""
    Write-Host "========================================"
    Write-Host " AUTO REPAIR FROM $Source"
    Write-Host "========================================"
    Write-Host ""

    # 수정 HEAD 는 감사 전이므로 FULL CI 를 돌리지 않는다 → push 전에 draft 로
    $draftPr = Get-Pr
    if ($draftPr -and -not $draftPr.isDraft) {
        if (-not (Set-PrDraft -Draft $true)) {
            Stop-Hold -Reason "PR_DRAFT_CONVERT_FAILED" -PrHead $OldHead -MainHead $MainHead
            return $false
        }
    }

    $repairText = Invoke-HostScript `
        -Path $repairScript `
        -Params @{ PrNumber = $CurrentPr; AuditSource = $Source }

    if ($repairText -match '(?m)^REPAIR_RESULT=PUSHED\s*$') {

        $newPr = Get-Pr
        $newHead = [string]$newPr.headRefOid

        if ($newHead -eq $OldHead) {
            Stop-Hold `
                -Reason "REPAIR_PUSHED_BUT_HEAD_UNCHANGED" `
                -PrHead $OldHead `
                -MainHead $MainHead
            return $false
        }

        Write-Host ""
        Write-Host "REPAIR_HEAD_MOVED=TRUE"
        Write-Host "OLD_HEAD=$OldHead"
        Write-Host "NEW_HEAD=$newHead"
        Write-Host "NEXT=CI_WAIT"
        Write-Host ""

        # PUSHED → 새 remote PR HEAD → CI_WAIT
        Save-RuntimeState `
            -Status "CI_WAIT" `
            -Action "REPAIR_PUSHED_WAITING_FOR_CI" `
            -PrHead $newHead `
            -MainHead $MainHead `
            -Detail "source=$Source;old_head=$OldHead"

        Invoke-StateRefresh
        return $true
    }

    $holdMatch = [regex]::Match($repairText, '(?m)^REPAIR_HOLD=(\S+)\s*$')

    if ($holdMatch.Success) {
        Stop-Hold `
            -Reason "REPAIR_$($holdMatch.Groups[1].Value)" `
            -PrHead $OldHead `
            -MainHead $MainHead `
            -Detail "source=$Source"
        return $false
    }

    if ($repairText -match 'HOST_SCRIPT_EXCEPTION=') {
        Stop-Hold `
            -Reason "REPAIR_SCRIPT_ERROR" `
            -PrHead $OldHead `
            -MainHead $MainHead
        return $false
    }

    Stop-Hold `
        -Reason "REPAIR_RESULT_UNKNOWN" `
        -PrHead $OldHead `
        -MainHead $MainHead
    return $false
}

function Get-PrMergeInfo {
    gh pr view $CurrentPr `
        --repo $repoSlug `
        --json number,state,isDraft,mergeable,headRefOid,mergedAt,mergeCommit |
        ConvertFrom-Json
}

# DUAL PASS 이후 병합 직전 재검증.
# 반환 Decision: MERGE | RELOOP(새 HEAD/main 재감사) | CI_WAIT | HOLD
function Invoke-MergeGuard {
    param(
        [string]$AuditedHead,
        [string]$AuditedMain
    )

    Save-RuntimeState `
        -Status "MERGE_GUARD" `
        -Action "PRE_MERGE_FRESHNESS_CHECK" `
        -PrHead $AuditedHead `
        -MainHead $AuditedMain

    Write-Host ""
    Write-Host "========================================"
    Write-Host " MERGE_GUARD"
    Write-Host "========================================"

    # --- owner 보류: 어떤 판정이 나와도 병합 금지 ---
    if (Test-Path (Join-Path $hostRoot "state\merge-hold-pr-$CurrentPr.json")) {
        return [pscustomobject]@{ Decision = "HOLD"; Reason = "GUARD_PR_ON_OWNER_HOLD" }
    }

    # --- 감사 증거 (V2 §7): exact HEAD/main 의 GPT PASS + Claude PASS (PASS 캐시), 같은 HEAD, 같은 packet digest,
    #     GPT/Claude evidence_seen ⊇ packet.manifest.required ---
    $gptR = Get-AuditResult -Source GPT -MainHead $AuditedMain -PrHead $AuditedHead -CacheOnly
    $claudeR = Get-AuditResult -Source CLAUDE -MainHead $AuditedMain -PrHead $AuditedHead -CacheOnly

    if (
        -not $gptR -or -not $gptR.Valid -or $gptR.Verdict -ne "PASS" -or -not $gptR.EvidenceSeenOk -or
        -not $claudeR -or -not $claudeR.Valid -or $claudeR.Verdict -ne "PASS" -or -not $claudeR.EvidenceSeenOk
    ) {
        return [pscustomobject]@{ Decision = "HOLD"; Reason = "GUARD_NOT_DUAL_PASS" }
    }

    if ($gptR.Head -ne $claudeR.Head -or $gptR.Head -ne $AuditedHead) {
        return [pscustomobject]@{ Decision = "HOLD"; Reason = "GUARD_AUDITED_HEAD_MISMATCH" }
    }

    if (-not $gptR.Digest -or $gptR.Digest -ne $claudeR.Digest) {
        return [pscustomobject]@{ Decision = "HOLD"; Reason = "GUARD_PACKET_DIGEST_MISMATCH" }
    }

    Write-Host "GUARD_PACKET_DIGEST=$($gptR.Digest)"

    # --- 병합 직전 remote 재조회 (mergeable UNKNOWN은 GitHub 계산 대기) ---
    $info = $null

    for ($i = 0; $i -lt 6; $i++) {
        $info = Get-PrMergeInfo

        if ("$($info.mergeable)" -ne "UNKNOWN") {
            break
        }

        Start-Sleep -Seconds 5
    }

    $remoteMain = Get-MainHead

    Write-Host "GUARD_PR_STATE=$($info.state)"
    Write-Host "GUARD_DRAFT=$($info.isDraft)"
    Write-Host "GUARD_MERGEABLE=$($info.mergeable)"
    Write-Host "GUARD_REMOTE_HEAD=$($info.headRefOid)"
    Write-Host "GUARD_AUDITED_HEAD=$AuditedHead"
    Write-Host "GUARD_REMOTE_MAIN=$remoteMain"
    Write-Host "GUARD_AUDITED_MAIN=$AuditedMain"

    if ($info.state -ne "OPEN") {
        # 이미 병합/종료됨 → loop가 실제 상태에서 이어간다
        return [pscustomobject]@{ Decision = "RELOOP"; Reason = "PR_STATE_$($info.state)" }
    }

    if ([string]$info.headRefOid -ne $AuditedHead) {
        return [pscustomobject]@{ Decision = "RELOOP"; Reason = "HEAD_MOVED_AFTER_AUDIT" }
    }

    if ($remoteMain -ne $AuditedMain) {
        return [pscustomobject]@{ Decision = "RELOOP"; Reason = "MAIN_MOVED_AFTER_AUDIT" }
    }

    # --- §7 condition 8: the PR HEAD contains the current base (behind_by == 0), checked before the merge ---
    $behindBy = Get-BehindBy -Base $remoteMain -Head $AuditedHead
    Write-Host "GUARD_BEHIND_BY=$behindBy"

    if ($behindBy -lt 0) {
        return [pscustomobject]@{ Decision = "HOLD"; Reason = "GUARD_BASE_COMPARE_UNREADABLE" }
    }

    if ($behindBy -gt 0) {
        # the loop brings the branch up to date and audits the new HEAD
        return [pscustomobject]@{ Decision = "RELOOP"; Reason = "HEAD_BEHIND_BASE" }
    }

    # --- current packet digest (V2 §7.2-§7.3): 같은 HEAD/base 에서 packet 을 다시 생성 (AI 호출 없음) →
    #     hard completeness PASS + 감사된 digest 와 byte-identical 이어야 한다.
    #     full re-scan of every stream 포함 (§7.1). 불완전 stream / 읽을 수 없는 source → TECHNICAL_HOLD ---
    $pkText = Invoke-HostScript `
        -Path $auditScript `
        -Params @{ PrNumber = $CurrentPr; PacketOnly = $true }

    $pkBlocked = [regex]::Match($pkText, '(?m)^AUDIT_BLOCKED=(\S+)\s*$')

    if ($pkBlocked.Success) {
        if ($pkBlocked.Groups[1].Value -in @("PR_NOT_OPEN", "HEAD_MOVED")) {
            return [pscustomobject]@{ Decision = "RELOOP"; Reason = "PACKET_REBUILD_$($pkBlocked.Groups[1].Value)" }
        }

        return [pscustomobject]@{ Decision = "HOLD"; Reason = "GUARD_PACKET_$($pkBlocked.Groups[1].Value)" }
    }

    $pkDigest = [regex]::Match($pkText, '(?m)^PACKET_DIGEST=([0-9a-f]{64})\s*$')

    if ($pkText -notmatch '(?m)^PACKET_COMPLETE=True\s*$' -or -not $pkDigest.Success) {
        return [pscustomobject]@{ Decision = "HOLD"; Reason = "GUARD_PACKET_INCOMPLETE" }
    }

    Write-Host "GUARD_CURRENT_PACKET_DIGEST=$($pkDigest.Groups[1].Value)"

    # §7.1: 어떤 차이든 (cited source 의 body digest, 새로 인용된 source, manifest/bytes/digest 변경) DUAL PASS 무효
    # → 새 audit identity 로 재감사. 재생성된 packet 이 이 HEAD 의 current pointer 가 되므로 다음 loop 의 캐시 판정은
    # 새 digest 로만 맞는다. 같은 HEAD 의 CI 는 유효. (읽을 수 없는 stream·source 는 위에서 TECHNICAL_HOLD)
    if ($pkDigest.Groups[1].Value -ne $gptR.Digest) {
        return [pscustomobject]@{ Decision = "RELOOP"; Reason = "PACKET_DIGEST_CHANGED_AFTER_AUDIT" }
    }

    if ($info.isDraft -eq $true) {
        return [pscustomobject]@{ Decision = "HOLD"; Reason = "GUARD_PR_IS_DRAFT" }
    }

    $ci = Get-CiState -Head $AuditedHead

    Write-Host "GUARD_CI=$($ci.State) $($ci.Success)/$($ci.Total)"

    if ($ci.State -eq "FAILED") {
        return [pscustomobject]@{ Decision = "HOLD"; Reason = "GUARD_CI_FAILED" }
    }

    if ($ci.State -ne "GREEN") {
        return [pscustomobject]@{ Decision = "CI_WAIT"; Reason = "GUARD_CI_$($ci.State)" }
    }

    if ("$($info.mergeable)" -ne "MERGEABLE") {
        return [pscustomobject]@{ Decision = "HOLD"; Reason = "GUARD_NOT_MERGEABLE_$($info.mergeable)" }
    }

    return [pscustomobject]@{ Decision = "MERGE"; Reason = "ALL_CHECKS_PASSED" }
}

# GitHub merge API. sha=<exact audited HEAD> → HEAD가 움직였으면 GitHub가 거부(409).
# force 없음. 성공 시 merge commit SHA, 실패 시 $null (hold 기록됨).
function Invoke-AutoMerge {
    param(
        [string]$AuditedHead,
        [string]$AuditedMain
    )

    Save-RuntimeState `
        -Status "AUTO_MERGING" `
        -Action "GITHUB_MERGE_API" `
        -PrHead $AuditedHead `
        -MainHead $AuditedMain `
        -Detail "method=$autoMergeMethod;expected_head_sha=$AuditedHead"

    Write-Host ""
    Write-Host "AUTO_MERGING PR #$CurrentPr expected_head_sha=$AuditedHead method=$autoMergeMethod"

    $errPath = Join-Path $logDir "pr-$CurrentPr-automerge-$(Get-Date -Format 'yyyyMMdd-HHmmss').stderr.txt"
    $oldEap = $ErrorActionPreference

    try {
        $ErrorActionPreference = "Continue"

        $w = Invoke-GhWrite `
            -GhArgs @("api", "-X", "PUT", "repos/$repoSlug/pulls/$CurrentPr/merge", "-f", "sha=$AuditedHead", "-f", "merge_method=$autoMergeMethod") `
            -Site "orchestrator:Invoke-AutoMerge" `
            -ErrPath $errPath

        $resp = $w.Output
        $code = $w.ExitCode
    }
    finally {
        $ErrorActionPreference = $oldEap
    }

    $merged = $null

    if ($code -eq 0 -and $resp) {
        try {
            $merged = (@($resp) -join "`n") | ConvertFrom-Json
        }
        catch {
            $merged = $null
        }
    }

    if (-not $merged -or $merged.merged -ne $true -or -not $merged.sha) {
        $err = if (Test-Path $errPath) { ("$(Get-Content $errPath -Raw)" -replace '\s+', ' ').Trim() } else { "" }
        $nowHead = [string](Get-PrMergeInfo).headRefOid

        Stop-Hold `
            -Reason "AUTO_MERGE_FAILED" `
            -PrHead $AuditedHead `
            -MainHead $AuditedMain `
            -Detail "exit=$code;remote_head_now=$nowHead;error=$err"
        return $null
    }

    $mergeSha = [string]$merged.sha
    $mainNow = ""

    # merge 결과 확인: PR MERGED + mergeCommit == merge SHA + remote main == merge SHA
    $verified = $false

    for ($i = 0; $i -lt 10; $i++) {
        $after = Get-PrMergeInfo
        $mainNow = Get-MainHead

        if (
            $after.state -eq "MERGED" -and
            [string]$after.mergeCommit.oid -eq $mergeSha -and
            $mainNow -eq $mergeSha
        ) {
            $verified = $true
            break
        }

        Start-Sleep -Seconds 3
    }

    if (-not $verified) {
        Stop-Hold `
            -Reason "AUTO_MERGE_UNVERIFIED" `
            -PrHead $AuditedHead `
            -MainHead $mainNow `
            -Detail "merge_sha=$mergeSha"
        return $null
    }

    # §8 POST_MERGE_VERIFY (tree equality) runs at the top of the next pass, from GitHub's own record of the merge:
    # it is stateless, so a restart or a retry can never skip it.
    Save-RuntimeState `
        -Status "AUTO_MERGED" `
        -Action "MERGE_VERIFIED" `
        -PrHead $AuditedHead `
        -MainHead $mergeSha `
        -Detail "merge_sha=$mergeSha;audited_main=$AuditedMain"

    Write-Host "AUTO_MERGED=TRUE"
    Write-Host "MERGE_SHA=$mergeSha"
    Write-Host "NEW_MAIN=$mergeSha"
    Write-Host "NEXT=POST_MERGE_FULL_AUDIT"

    return $mergeSha
}

# full-main DUAL PASS → exact main의 ROADMAP을 새로 읽어 다음 slice 선택 → 자동 구현 PR
# 반환: RETRY | HOLD | NEW_PR
function Invoke-AutoNext {
    param(
        [string]$MergedHead,
        [string]$MainHead
    )

    Save-RuntimeState `
        -Status "NEXT_SELECTING" `
        -Action "ROADMAP_RESELECT" `
        -PrHead $MergedHead `
        -MainHead $MainHead

    Write-Host ""
    Write-Host "========================================"
    Write-Host " AUTO-NEXT: ROADMAP RESELECT @ $MainHead"
    Write-Host "========================================"

    $selText = Invoke-HostScript `
        -Path $nextSelectScript `
        -Params @{ ExpectedMain = $MainHead }

    if ($selText -match '(?m)^NEXT_HOLD=MAIN_MOVED\s*$') {
        return [pscustomobject]@{ Action = "RETRY"; Pr = 0 }
    }

    if ($selText -match '(?m)^NEXT_DECISION=DONE\s*$') {
        Stop-Idle -Status "COMPLETE" -Action "ROADMAP_COMPLETE" -PrHead $MergedHead -MainHead $MainHead
        return [pscustomobject]@{ Action = "HOLD"; Pr = 0 }
    }

    if ($selText -match '(?m)^NEXT_DECISION=HOLD\s*$') {
        $cat = [regex]::Match($selText, '(?m)^NEXT_HOLD_CATEGORY=(\S+)\s*$').Groups[1].Value
        $why = [regex]::Match($selText, '(?m)^NEXT_HOLD_REASON=(.*?)\s*$').Groups[1].Value

        Stop-Hold `
            -Reason "NEXT_HOLD_$cat" `
            -PrHead $MergedHead `
            -MainHead $MainHead `
            -Detail $why
        return [pscustomobject]@{ Action = "HOLD"; Pr = 0 }
    }

    if ($selText -notmatch '(?m)^NEXT_DECISION=PROCEED\s*$') {
        $h = [regex]::Match($selText, '(?m)^NEXT_HOLD=(\S+)\s*$')
        $reason = if ($h.Success) { "NEXT_$($h.Groups[1].Value)" } elseif ($selText -match 'HOST_SCRIPT_EXCEPTION=') { "NEXT_SELECT_SCRIPT_ERROR" } else { "NEXT_SELECTION_FAILED" }

        Stop-Hold -Reason $reason -PrHead $MergedHead -MainHead $MainHead
        return [pscustomobject]@{ Action = "HOLD"; Pr = 0 }
    }

    $sliceId = [regex]::Match($selText, '(?m)^NEXT_SLICE_ID=(\S+)\s*$').Groups[1].Value

    Save-RuntimeState `
        -Status "IMPLEMENTING" `
        -Action "AUTO_NEXT_SLICE" `
        -PrHead $MergedHead `
        -MainHead $MainHead `
        -Detail "slice=$sliceId"

    $implText = Invoke-HostScript `
        -Path $repairScript `
        -Params @{ ImplementNext = $true }

    $prMatch = [regex]::Match($implText, '(?m)^NEXT_PR=(\d+)\s*$')

    if ($prMatch.Success) {
        return [pscustomobject]@{
            Action = "NEW_PR"
            Pr = [int]$prMatch.Groups[1].Value
        }
    }

    $holdMatch = [regex]::Match($implText, '(?m)^REPAIR_HOLD=(\S+)\s*$')

    # 구현 중 main 이동: HOLD 대상 아님 → 새 main 에서 post-merge 감사·재선택 (draft 는 보존됨)
    if ($holdMatch.Success -and $holdMatch.Groups[1].Value -eq "MAIN_MOVED_DURING_FIX") {
        Write-Host "NEXT_IMPL_MAIN_MOVED=RESELECT_ON_NEW_MAIN"
        return [pscustomobject]@{ Action = "RETRY"; Pr = 0 }
    }

    $reason = if ($holdMatch.Success) {
        "NEXT_IMPL_$($holdMatch.Groups[1].Value)"
    }
    elseif ($implText -match 'HOST_SCRIPT_EXCEPTION=') {
        "NEXT_IMPL_SCRIPT_ERROR"
    }
    else {
        "NEXT_IMPL_RESULT_UNKNOWN"
    }

    Stop-Hold -Reason $reason -PrHead $MergedHead -MainHead $MainHead -Detail "slice=$sliceId"
    return [pscustomobject]@{ Action = "HOLD"; Pr = 0 }
}

# MERGED → exact new main → full-main audit → (허용 BLOCKER) remediation PR
# 반환: RETRY | HOLD | NEW_PR
function Invoke-PostMerge {
    param(
        [string]$MergedHead
    )

    $mainHead = Get-MainHead

    if ($script:lastMergeSha) {
        Write-Host "EXPECTED_NEW_MAIN=$script:lastMergeSha"
        Write-Host "EXACT_NEW_MAIN_MATCH=$($mainHead -eq $script:lastMergeSha)"
    }

    Save-RuntimeState `
        -Status "FULL_AUDITING" `
        -Action "POST_MERGE_FULL_AUDIT" `
        -PrHead $MergedHead `
        -MainHead $mainHead

    Write-Host ""
    Write-Host "========================================"
    Write-Host " POST-MERGE FULL-MAIN AUDIT"
    Write-Host " MAIN=$mainHead"
    Write-Host "========================================"
    Write-Host ""

    $fullText = Invoke-HostScript `
        -Path $fullAuditScript `
        -Params @{ MergedPr = $CurrentPr }

    if ($fullText -match '(?m)^FULL_AUDIT_WAIT=CI_PENDING\s*$') {
        Save-RuntimeState `
            -Status "CI_WAIT" `
            -Action "WAITING_FOR_MAIN_CI" `
            -PrHead $MergedHead `
            -MainHead $mainHead `
            -Detail "poll=$PollSeconds"

        return [pscustomobject]@{ Action = "RETRY"; Pr = 0 }
    }

    if ($fullText -match '(?m)^FULL_AUDIT_RESULT=STALE_MAIN_MOVED\s*$') {
        return [pscustomobject]@{ Action = "RETRY"; Pr = 0 }
    }

    if ($fullText -match '(?m)^FULL_AUDIT_RESULT=DUAL_PASS\s*$') {
        if (-not $autoNext) {
            Stop-Idle `
                -Status "IDLE" `
                -Action "AUTO_NEXT_DISABLED_BY_CONFIG" `
                -PrHead $MergedHead `
                -MainHead $mainHead `
                -Detail "GPT=PASS;CLAUDE=PASS"

            return [pscustomobject]@{ Action = "HOLD"; Pr = 0 }
        }

        return (Invoke-AutoNext -MergedHead $MergedHead -MainHead $mainHead)
    }

    if ($fullText -match '(?m)^FULL_AUDIT_RESULT=BLOCKED\s*$') {

        Save-RuntimeState `
            -Status "REMEDIATING" `
            -Action "FULL_AUDIT_BLOCKER_REMEDIATION" `
            -PrHead $MergedHead `
            -MainHead $mainHead

        Write-Host ""
        Write-Host "========================================"
        Write-Host " FULL-AUDIT BLOCKER → REMEDIATION PR"
        Write-Host "========================================"
        Write-Host ""

        $remText = Invoke-HostScript `
            -Path $repairScript `
            -Params @{ RemediateMain = $true }

        $prMatch = [regex]::Match($remText, '(?m)^REMEDIATION_PR=(\d+)\s*$')

        if ($prMatch.Success) {
            return [pscustomobject]@{
                Action = "NEW_PR"
                Pr = [int]$prMatch.Groups[1].Value
            }
        }

        $holdMatch = [regex]::Match($remText, '(?m)^REPAIR_HOLD=(\S+)\s*$')

        $reason = if ($holdMatch.Success) {
            "REMEDIATION_$($holdMatch.Groups[1].Value)"
        }
        elseif ($remText -match 'HOST_SCRIPT_EXCEPTION=') {
            "REMEDIATION_SCRIPT_ERROR"
        }
        else {
            "REMEDIATION_RESULT_UNKNOWN"
        }

        Stop-Hold -Reason $reason -PrHead $MergedHead -MainHead $mainHead
        return [pscustomobject]@{ Action = "HOLD"; Pr = 0 }
    }

    $fullHold = [regex]::Match($fullText, '(?m)^FULL_AUDIT_HOLD=(\S+)\s*$')

    $reason = if ($fullHold.Success) {
        "FULL_AUDIT_$($fullHold.Groups[1].Value)"
    }
    elseif ($fullText -match '(?m)^FULL_AUDIT_RESULT=TECHNICAL_HOLD_INSUFFICIENT\s*$') {
        "FULL_AUDIT_INSUFFICIENT"
    }
    elseif ($fullText -match 'HOST_SCRIPT_EXCEPTION=') {
        "FULL_AUDIT_SCRIPT_ERROR"
    }
    else {
        "FULL_AUDIT_RESULT_UNKNOWN"
    }

    Stop-Hold -Reason $reason -PrHead $MergedHead -MainHead $mainHead
    return [pscustomobject]@{ Action = "HOLD"; Pr = 0 }
}

Write-Host ""
Write-Host "========================================"
Write-Host " ICBM ORCHESTRATOR V1.3"
Write-Host " CURRENT PR #$CurrentPr"
Write-Host "========================================"
Write-Host ""
Write-Host "AUTO_MERGE=$($autoMerge.ToString().ToUpper()) (method=$autoMergeMethod, MERGE_GUARD required)"
Write-Host "AUDIT_TOOLSET_SHA256=$toolsetSha"

foreach ($tl in $toolsetLines) {
    Write-Host "AUDIT_TOOLSET_FILE $tl"
}

Write-Host "MAX_WAIT_MINUTES=$MaxWaitMinutes"
Write-Host "POLL_SECONDS=$PollSeconds"

if (Test-Path $runtimeStatePath) {
    try {
        $previousRuntime =
            Get-Content $runtimeStatePath -Raw -Encoding utf8 |
            ConvertFrom-Json

        Write-Host "RESUME_PREVIOUS_PR=$($previousRuntime.current_pr)"
        Write-Host "RESUME_PREVIOUS_STATUS=$($previousRuntime.status)"
        Write-Host "RESUME_PREVIOUS_ACTION=$($previousRuntime.action)"
        Write-Host "RESUME_PREVIOUS_HEAD=$($previousRuntime.pr_head)"
    }
    catch {
        Write-Host "RESUME_PREVIOUS_STATUS=UNREADABLE"
    }
}

Write-Host ""

# One pass of the control loop. It returns on a hold, on a configured stop or when the work is done; the supervisor
# below decides what happens next. Dot-sourced, so its variables (the current PR among them) stay in script scope.
$controlPass = {
while ($true) {

    if ((Get-Date) -ge $deadline) {
        Stop-Hold -Reason "MAX_WAIT_TIME_REACHED"
        return
    }

    # owner 가 보류한 PR: FIXER / 추가 CI / 병합 없이 즉시 HUMAN_DECISION_REQUIRED (state/merge-hold-pr-N.json 삭제 전까지)
    $mergeHoldPath = Join-Path $hostRoot "state\merge-hold-pr-$CurrentPr.json"

    if (Test-Path $mergeHoldPath) {
        $mh = Get-Content $mergeHoldPath -Raw -Encoding utf8 | ConvertFrom-Json
        Write-Host "PR_ON_OWNER_HOLD=$mergeHoldPath"
        Stop-Hold -Reason "PR_ON_OWNER_HOLD" -Detail "$($mh.reason)"
        return
    }

    $pr = Get-Pr

    if (-not $pr) {
        Stop-Hold -Reason "PR_NOT_FOUND"
        return
    }

    $mainHead = Get-MainHead
    $prHead = [string]$pr.headRefOid

    Save-RuntimeState `
        -Status "ACTIVE" `
        -Action "CHECK_CURRENT_STATE" `
        -PrHead $prHead `
        -MainHead $mainHead

    if ($pr.mergedAt -or $pr.state -eq "MERGED") {
        Save-RuntimeState `
            -Status "MERGED" `
            -Action "POST_MERGE_FULL_AUDIT" `
            -PrHead $prHead `
            -MainHead $mainHead

        Write-Host ""
        Write-Host "CURRENT_STATE=MERGED"

        $treeCheck = Test-PostMergeTree -MergedHead $prHead

        if (-not $treeCheck.Ok) {
            Stop-Hold `
                -Reason "POST_MERGE_TREE_MISMATCH" `
                -PrHead $prHead `
                -MainHead $mainHead `
                -Detail $treeCheck.Detail
            return
        }

        Write-Host "NEXT=POST_MERGE_FULL_AUDIT"

        $post = Invoke-PostMerge -MergedHead $prHead

        if ($post.Action -eq "RETRY") {
            Write-Host "WAITING_FOR_MAIN=$PollSeconds seconds"
            Start-Sleep -Seconds $PollSeconds
            continue
        }

        if ($post.Action -eq "NEW_PR") {
            Write-Host ""
            Write-Host "PROMOTED_PR=$($post.Pr)"
            Write-Host "NEXT=PR_LOOP"

            # 이후 CURRENT PR = remediation 또는 auto-next PR. 기존 PR loop 그대로 진행.
            $CurrentPr = $post.Pr
            $lookaheadAttemptedKey = $null

            Save-RuntimeState `
                -Status "ACTIVE" `
                -Action "PR_PROMOTED" `
                -MainHead $mainHead `
                -Detail "merged_head=$prHead"

            continue
        }

        return
    }

    if ($pr.state -ne "OPEN") {
        Stop-Hold -Reason "PR_NOT_OPEN" -PrHead $prHead -MainHead $mainHead
        return
    }

    # State + NEXT-1~3 freshness update
    Invoke-StateRefresh

    # -------------------------------------------------
    # Base freshness (§7 condition 8). A PR HEAD that does not contain the current main is brought up to date first:
    # auditing it would be wasted, and merging it would put an unaudited tree on main. This is mechanical work, never a
    # question for the user. A conflict GitHub cannot merge is a TECHNICAL_HOLD for the repair path.
    # -------------------------------------------------

    $behindBy = Get-BehindBy -Base $mainHead -Head $prHead

    if ($behindBy -lt 0) {
        Stop-Hold -Reason "BASE_COMPARE_UNREADABLE" -PrHead $prHead -MainHead $mainHead
        return
    }

    if ($behindBy -gt 0) {
        Write-Host ""
        Write-Host "BASE_SYNC=PR_HEAD_BEHIND_MAIN_BY_$behindBy"

        # the synced HEAD is unaudited: no FULL CI on it
        if (-not $pr.isDraft -and -not (Set-PrDraft -Draft $true)) {
            Stop-Hold -Reason "PR_DRAFT_CONVERT_FAILED" -PrHead $prHead -MainHead $mainHead
            return
        }

        $sync = Invoke-GhWrite `
            -GhArgs @("api", "-X", "PUT", "repos/$repoSlug/pulls/$CurrentPr/update-branch", "-f", "expected_head_sha=$prHead") `
            -Site "orchestrator:base-sync"

        if ($sync.Refused -or $sync.ExitCode -ne 0) {
            Stop-Hold `
                -Reason "BASE_SYNC_FAILED" `
                -PrHead $prHead `
                -MainHead $mainHead `
                -Detail "behind_by=$behindBy;exit=$($sync.ExitCode)"
            return
        }

        Save-RuntimeState `
            -Status "BASE_SYNC" `
            -Action "UPDATE_BRANCH_REQUESTED" `
            -PrHead $prHead `
            -MainHead $mainHead `
            -Detail "behind_by=$behindBy"

        Write-Host "BASE_SYNC=UPDATE_BRANCH_REQUESTED (new HEAD is audited from scratch)"
        Start-Sleep -Seconds $PollSeconds
        continue
    }

    # -------------------------------------------------
    # audit-before-CI (owner decision 2026-09-28): exact candidate HEAD → GPT + Claude 감사 먼저.
    # 감사에서 반려된 HEAD 에는 FULL CI 를 돌리지 않는다. FULL CI 는 DUAL PASS HEAD 에서 1회.
    # -------------------------------------------------

    # -------------------------------------------------
    # Exact-head audit
    # 사용자 checkout 대신 host audit worktree에서 수행 (run-audit).
    # Existing valid cache is reused by run-audit.
    # -------------------------------------------------

    Save-RuntimeState `
        -Status "AUDITING" `
        -Action "EXACT_HEAD_AUDIT" `
        -PrHead $prHead `
        -MainHead $mainHead

    Write-Host ""
    Write-Host "========================================"
    Write-Host " EXACT-HEAD AUDIT"
    Write-Host "========================================"
    Write-Host ""

    # 같은 audit identity (exact HEAD + packet digest, 같은 main) 에 GPT PASS + Claude PASS (PASS 캐시) 가 있고
    # 둘 다 evidence_seen ⊇ required 이면 재감사하지 않는다 (CI 완료만으로 재감사 불필요). BLOCKER/INSUFFICIENT/HOLD 는 캐시가 아니다.
    $cachedGpt = Get-AuditResult -Source GPT -MainHead $mainHead -PrHead $prHead -CacheOnly
    $cachedClaude = Get-AuditResult -Source CLAUDE -MainHead $mainHead -PrHead $prHead -CacheOnly
    $dualPassCached = (
        $cachedGpt -and $cachedGpt.Valid -and $cachedGpt.Verdict -eq "PASS" -and $cachedGpt.EvidenceSeenOk -and
        $cachedClaude -and $cachedClaude.Valid -and $cachedClaude.Verdict -eq "PASS" -and $cachedClaude.EvidenceSeenOk -and
        $cachedGpt.Digest -and $cachedGpt.Digest -eq $cachedClaude.Digest
    )

    $auditText = if ($dualPassCached) {
        Write-Host "AUDIT_CACHE=DUAL_PASS (same exact HEAD/main + packet digest $($cachedGpt.Digest))"
        "AUDIT_CACHE=DUAL_PASS"
    }
    else {
        Invoke-HostScript `
            -Path $auditScript `
            -Params @{ PrNumber = $CurrentPr; SkipCiGate = $true }
    }

    if ($auditText -match '(?m)^AUDIT_BLOCKED=(CI_RUNNING|HEAD_MOVED)\s*$') {
        # 일시적 상태 → CI_WAIT 후 재시도
        Save-RuntimeState `
            -Status "CI_WAIT" `
            -Action "AUDIT_DEFERRED_$($Matches[1])" `
            -PrHead $prHead `
            -MainHead $mainHead

        Start-Sleep -Seconds $PollSeconds
        continue
    }

    if ($auditText -match 'HOST_SCRIPT_EXCEPTION=') {
        Stop-Hold -Reason "AUDIT_SCRIPT_ERROR" -PrHead $prHead -MainHead $mainHead
        return
    }

    $auditBlocked = [regex]::Match($auditText, '(?m)^AUDIT_BLOCKED=(\S+)\s*$')

    if ($auditBlocked.Success) {
        Stop-Hold `
            -Reason "AUDIT_BLOCKED_$($auditBlocked.Groups[1].Value)" `
            -PrHead $prHead `
            -MainHead $mainHead
        return
    }

    # Audit may take time. Re-read remote state.
    $afterAuditPr = Get-Pr
    $afterAuditMain = Get-MainHead

    if (
        $afterAuditPr.headRefOid -ne $prHead -or
        $afterAuditMain -ne $mainHead -or
        $auditText -match '(?m)^HEAD_STATUS=STALE\s*$'
    ) {
        Write-Host ""
        Write-Host "AUDIT_RESULT=STALE"
        Write-Host "NEXT=RESTART_LOOP"
        Invoke-StateRefresh
        continue
    }

    # -------------------------------------------------
    # GPT result
    # -------------------------------------------------

    $gpt = Get-AuditResult `
        -Source GPT `
        -MainHead $mainHead `
        -PrHead $prHead

    if (-not $gpt -or -not $gpt.Valid) {
        Stop-Hold `
            -Reason "GPT_AUDIT_RESULT_MISSING_OR_INVALID" `
            -PrHead $prHead `
            -MainHead $mainHead
        return
    }

    Write-Host ""
    Write-Host "GPT_VERDICT=$($gpt.Verdict)"
    Write-Host "GPT_SUMMARY=$($gpt.Summary)"

    if ($gpt.Verdict -eq "INSUFFICIENT") {
        Stop-Hold `
            -Reason "GPT_INSUFFICIENT" `
            -PrHead $prHead `
            -MainHead $mainHead `
            -Detail $gpt.Summary
        return
    }

    # the user's decision (closed list, §5.1): the auditor names the category at the start of its summary
    if ($gpt.Verdict -eq "HUMAN_DECISION_REQUIRED") {
        # the category decides the class; a verdict without one of the closed list is technical and is audited again
        $category = Get-HumanDecisionCategory ($gpt.Summary -replace '^(\[CALL\d+\]\s*)+', '')
        Stop-Hold `
            -Reason $(if ($category) { "GPT_HUMAN_DECISION_REQUIRED_$category" } else { "GPT_HUMAN_DECISION_WITHOUT_CATEGORY" }) `
            -PrHead $prHead `
            -MainHead $mainHead `
            -Detail $gpt.Summary
        return
    }

    # HOLD (예: EVIDENCE_NOT_SEEN) 와 INSUFFICIENT 는 TECHNICAL_HOLD: code BLOCKER 가 아니므로 FIXER 로 가지 않고,
    # supervisor 가 같은 identity 를 다시 감사한다
    if ($gpt.Verdict -eq "HOLD") {
        Stop-Hold `
            -Reason "GPT_HOLD" `
            -PrHead $prHead `
            -MainHead $mainHead `
            -Detail $gpt.Summary
        return
    }

    if ($gpt.Verdict -eq "BLOCKER") {

        $repaired = Invoke-Repair `
            -Source GPT `
            -OldHead $prHead `
            -MainHead $mainHead

        if (-not $repaired) {
            return
        }

        # New HEAD → GPT from scratch (draft, no FULL CI until DUAL PASS)
        continue
    }

    # -------------------------------------------------
    # Claude independent audit result
    # run-audit already invokes Claude only after GPT PASS.
    # -------------------------------------------------

    $claude = Get-AuditResult `
        -Source CLAUDE `
        -MainHead $mainHead `
        -PrHead $prHead

    if (-not $claude -or -not $claude.Valid) {
        Stop-Hold `
            -Reason "CLAUDE_AUDIT_RESULT_MISSING_OR_INVALID" `
            -PrHead $prHead `
            -MainHead $mainHead
        return
    }

    Write-Host ""
    Write-Host "CLAUDE_VERDICT=$($claude.Verdict)"
    Write-Host "CLAUDE_SUMMARY=$($claude.Summary)"

    if ($claude.Verdict -eq "INSUFFICIENT") {
        Stop-Hold `
            -Reason "CLAUDE_INSUFFICIENT" `
            -PrHead $prHead `
            -MainHead $mainHead `
            -Detail $claude.Summary
        return
    }

    # the user's decision (closed list, §5.1): the auditor names the category at the start of its summary
    if ($claude.Verdict -eq "HUMAN_DECISION_REQUIRED") {
        # the category decides the class; a verdict without one of the closed list is technical and is audited again
        $category = Get-HumanDecisionCategory ($claude.Summary -replace '^(\[CALL\d+\]\s*)+', '')
        Stop-Hold `
            -Reason $(if ($category) { "CLAUDE_HUMAN_DECISION_REQUIRED_$category" } else { "CLAUDE_HUMAN_DECISION_WITHOUT_CATEGORY" }) `
            -PrHead $prHead `
            -MainHead $mainHead `
            -Detail $claude.Summary
        return
    }

    # HOLD (예: EVIDENCE_NOT_SEEN) 와 INSUFFICIENT 는 TECHNICAL_HOLD: code BLOCKER 가 아니므로 FIXER 로 가지 않고,
    # supervisor 가 같은 identity 를 다시 감사한다
    if ($claude.Verdict -eq "HOLD") {
        Stop-Hold `
            -Reason "CLAUDE_HOLD" `
            -PrHead $prHead `
            -MainHead $mainHead `
            -Detail $claude.Summary
        return
    }

    if ($claude.Verdict -eq "BLOCKER") {

        $repaired = Invoke-Repair `
            -Source CLAUDE `
            -OldHead $prHead `
            -MainHead $mainHead

        if (-not $repaired) {
            return
        }

        # Any Claude repair invalidates GPT PASS.
        # New HEAD always starts again at GPT (draft, no FULL CI until DUAL PASS).
        continue
    }

    # -------------------------------------------------
    # DUAL PASS freshness
    # -------------------------------------------------

    $finalPr = Get-Pr
    $finalMain = Get-MainHead
    $finalCi = Get-CiState -Head $finalPr.headRefOid

    if (
        $finalPr.headRefOid -ne $prHead -or
        $finalMain -ne $mainHead
    ) {
        Write-Host ""
        Write-Host "DUAL_PASS_DISCARDED=HEAD_OR_MAIN_MOVED"
        Invoke-StateRefresh
        continue
    }

    # ---- DUAL PASS HEAD → FULL CI 1회 ----
    if ($finalPr.isDraft) {
        Write-Host ""
        Write-Host "DUAL_PASS_HEAD=$prHead → READY_FOR_FULL_CI"

        # 이 exact HEAD 에 이미 FULL CI GREEN 이 있으면 ready 전환이 띄우는 중복 FULL CI 는 취소하고 기존 GREEN 을 쓴다
        $preReadyCi = Get-CiState -Head $prHead
        $readyAtUtc = (Get-Date).ToUniversalTime()

        if (-not (Set-PrDraft -Draft $false)) {
            Stop-Hold -Reason "PR_READY_CONVERT_FAILED" -PrHead $prHead -MainHead $mainHead
            return
        }

        if ($preReadyCi.State -eq "GREEN") {
            Write-Host "FULL_CI_ALREADY_GREEN_ON_EXACT_HEAD=TRUE"
            Stop-DuplicateFullCi -Head $prHead -AfterUtc $readyAtUtc
        }

        Save-RuntimeState `
            -Status "CI_WAIT" `
            -Action "FULL_CI_ON_DUAL_PASS_HEAD" `
            -PrHead $prHead `
            -MainHead $mainHead `
            -Detail "poll=$PollSeconds"

        Start-Sleep -Seconds $PollSeconds
        continue
    }

    Write-Host ""
    Write-Host "PR_HEAD=$prHead"
    Write-Host "CI_STATE=$($finalCi.State)"
    Write-Host "CI_SUCCESS=$($finalCi.Success)/$($finalCi.Total)"
    Write-Host "CI_PENDING=$($finalCi.Pending)"
    Write-Host "CI_FAILED=$($finalCi.Failed)"

    if ($finalCi.State -eq "FAILED") {
        $rerunDecision = Invoke-CiRerunOnce -Head $prHead

        if ($rerunDecision -eq "WAIT") {
            Save-RuntimeState `
                -Status "CI_WAIT" `
                -Action "CI_FAILED_RERUN_ONCE" `
                -PrHead $prHead `
                -MainHead $mainHead `
                -Detail "poll=$PollSeconds"

            Write-Host "CI_FAILED=RERUN_ONCE_WAIT $PollSeconds seconds"
            Start-Sleep -Seconds $PollSeconds
            continue
        }

        # CI 실패 수정으로 HEAD 가 바뀌면 이 PASS 는 stale → 새 HEAD 재감사 후 FULL CI 재실행
        Stop-Hold -Reason "CI_FAILED" -PrHead $prHead -MainHead $mainHead -Detail "FULL CI failed on the DUAL PASS head after one rerun"
        return
    }

    if ($finalCi.State -ne "GREEN") {
        Save-RuntimeState `
            -Status "CI_WAIT" `
            -Action "FULL_CI_ON_DUAL_PASS_HEAD" `
            -PrHead $prHead `
            -MainHead $mainHead `
            -Detail "poll=$PollSeconds"

        Write-Host "WAITING_FOR_FULL_CI=$PollSeconds seconds"
        Start-Sleep -Seconds $PollSeconds
        continue
    }

    # CI가 너무 빨리 끝나 Lookahead 기회가 없었던 경우에도
    # 병합대기 진입 전에 NEXT-1~3 준비를 한 번 보장한다.
    Invoke-LookaheadOnce `
        -MainHead $mainHead `
        -PrHead $prHead

    Invoke-StateRefresh

    Write-Host ""
    Write-Host "========================================"
    Write-Host " DUAL PASS"
    Write-Host "========================================"
    Write-Host ""
    Write-Host "PR=$CurrentPr"
    Write-Host "HEAD=$prHead"
    Write-Host "MAIN=$mainHead"
    Write-Host "CI=GREEN"
    Write-Host "GPT=PASS"
    Write-Host "CLAUDE=PASS"

    if (-not $autoMerge) {
        Stop-Idle `
            -Status "WAITING_FOR_MERGE_BY_CONFIG" `
            -Action "DUAL_PASS_COMPLETE" `
            -PrHead $prHead `
            -MainHead $mainHead `
            -Detail "CI=GREEN;GPT=PASS;CLAUDE=PASS"

        Write-Host "AUTO_MERGE=FALSE"
        Write-Host ""
        return
    }

    # -------------------------------------------------
    # DUAL_PASS → MERGE_GUARD → AUTO_MERGING → AUTO_MERGED → POST_MERGE_FULL_AUDIT
    # -------------------------------------------------

    $guard = Invoke-MergeGuard `
        -AuditedHead $prHead `
        -AuditedMain $mainHead

    Write-Host "MERGE_GUARD=$($guard.Decision) ($($guard.Reason))"

    if ($guard.Decision -eq "RELOOP") {
        # 기존 PASS 재사용 금지: 새 HEAD/main 기준으로 loop에서 재감사
        Write-Host "DUAL_PASS_DISCARDED=$($guard.Reason)"
        Invoke-StateRefresh
        continue
    }

    if ($guard.Decision -eq "CI_WAIT") {
        Save-RuntimeState `
            -Status "CI_WAIT" `
            -Action $guard.Reason `
            -PrHead $prHead `
            -MainHead $mainHead

        Start-Sleep -Seconds $PollSeconds
        continue
    }

    if ($guard.Decision -ne "MERGE") {
        Stop-Hold `
            -Reason $guard.Reason `
            -PrHead $prHead `
            -MainHead $mainHead
        return
    }

    $mergeSha = Invoke-AutoMerge `
        -AuditedHead $prHead `
        -AuditedMain $mainHead

    if (-not $mergeSha) {
        return
    }

    $lastMergeSha = $mergeSha

    # 다음 loop: PR MERGED → exact 새 main 기준 post-merge full audit
    continue
}
}

# -------------------------------------------------
# Supervisor (protocol §5.1)
#
#   no hold                  → the pass finished or stopped by configuration: end.
#   HUMAN_DECISION_REQUIRED  → end and wait for the user. This is the only class that waits for a person.
#   TECHNICAL_HOLD           → wait, then run the pass again from the live GitHub state. Nobody is asked.
#                              The same (PR, HEAD, main, reason) is retried technical_hold.max_same_state_retries times
#                              with a doubling wait; then the run ends TECHNICAL_HOLD_EXHAUSTED (a cost circuit breaker,
#                              reported, not a request for a decision). A different state starts a new count.
# -------------------------------------------------

$technicalCounts = @{}

while ($true) {
    $script:lastHold = $null

    . $controlPass

    $hold = $script:lastHold

    if (-not $hold) {
        break
    }

    if ($hold.Class -eq "HUMAN_DECISION_REQUIRED") {
        Write-Host "SUPERVISOR=WAITING_FOR_USER_DECISION ($($hold.Reason))"
        break
    }

    if ((Get-Date) -ge $deadline) {
        Write-Host "SUPERVISOR=DEADLINE_REACHED (resume continues from the recorded state)"
        break
    }

    $key = "$CurrentPr|$($hold.PrHead)|$($hold.MainHead)|$($hold.Reason)"
    $technicalCounts[$key] = 1 + [int]$technicalCounts[$key]
    $n = [int]$technicalCounts[$key]

    if ($n -gt $technicalMaxRetries) {
        Save-RuntimeState `
            -Status "TECHNICAL_HOLD_EXHAUSTED" `
            -Action $hold.Reason `
            -PrHead $hold.PrHead `
            -MainHead $hold.MainHead `
            -Detail "retries=$technicalMaxRetries;$($hold.Detail)"

        Write-Host "SUPERVISOR=TECHNICAL_HOLD_EXHAUSTED ($($hold.Reason) after $technicalMaxRetries retries of the same state)"
        break
    }

    $wait = [int][Math]::Min($technicalBackoff * [Math]::Pow(2, $n - 1), $technicalBackoffMax)
    Write-Host "SUPERVISOR=TECHNICAL_RETRY $n/$technicalMaxRetries ($($hold.Reason)) in $wait seconds"
    Start-Sleep -Seconds $wait
}
