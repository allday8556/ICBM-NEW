param(
    # PR repair mode: 현재 PR의 exact-head audit BLOCKER 수정
    [int]$PrNumber = 0,

    [ValidateSet("GPT","CLAUDE")]
    [string]$AuditSource = "GPT",

    # Remediation mode: post-merge full-main audit BLOCKER → remediation PR 생성
    [switch]$RemediateMain,

    # Auto-next mode: canonical ROADMAP에서 선택·교차확인된 다음 slice → 구현 PR 생성
    [switch]$ImplementNext
)

$ErrorActionPreference = "Stop"

$nativeUtf8 = New-Object System.Text.UTF8Encoding($false)
$script:OutputEncoding = $nativeUtf8
[Console]::OutputEncoding = $nativeUtf8

$hostRoot  = $PSScriptRoot

# V2 §3 marker grammar + authority write guard: every Host GitHub write goes through Invoke-GhWrite
. (Join-Path $PSScriptRoot "agent-host-authority-v2.ps1")

$configPath = Join-Path $hostRoot "state\orchestrator-config.json"
$auditScript = Join-Path $hostRoot "run-audit-v1.1.ps1"
$stateDir = Join-Path $hostRoot "state"
$logDir = Join-Path $hostRoot "logs"
$repairRoot = Join-Path $hostRoot "worktrees"

$config = Get-Content $configPath -Raw -Encoding utf8 | ConvertFrom-Json

$repoSlug = [string]$config.repository
# 사용자 repo는 object DB / origin remote 로만 사용한다.
# 사용자 repo의 branch / HEAD / working files는 절대 바꾸지 않는다.
$repoPath = [string]$config.repo_path
$maxCycles = [int]$config.repair_loop.max_cycles

New-Item -ItemType Directory -Force -Path $logDir | Out-Null
New-Item -ItemType Directory -Force -Path $repairRoot | Out-Null

$ts = Get-Date -Format "yyyyMMdd-HHmmss"
$modeTag = if ($RemediateMain) { "remediate-main" } elseif ($ImplementNext) { "implement-next" } else { "repair-pr-$PrNumber" }
$script:nativeErrPath = Join-Path $logDir "$modeTag-native-$ts.stderr.txt"
$script:activeWorktree = $null

# -------------------------------------------------
# Native helpers
#
# PS5.1 + ErrorActionPreference=Stop 에서 git의 정상 stderr
# (worktree 진행 메시지, CRLF 경고, push 진행 출력)가 terminating error로
# 오인되지 않도록 lookahead와 같은 방식(EAP=Continue + stderr 파일 분리)을 사용한다.
# -------------------------------------------------

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

function Remove-HostWorktree {
    param([string]$Path)

    if (-not $Path -or -not (Test-Path $Path)) {
        return
    }

    Invoke-Git @("-C", $repoPath, "worktree", "remove", "--force", $Path) | Out-Null

    if (Test-Path $Path) {
        Remove-Item $Path -Recurse -Force
    }
}

# Machine-readable 결과는 Write-Host가 아니라 캡처 가능한 output으로 반환한다.
# The class is decided by the reason's category (agent-host-authority-v2.ps1 Get-HoldClass, protocol §5.1):
# HUMAN_DECISION_REQUIRED only for the closed product / real-external-action list; anything else is a TECHNICAL_HOLD the
# Host retries by itself.
function Stop-RepairHold {
    param([string]$Reason)

    Remove-HostWorktree -Path $script:activeWorktree
    Write-Output "HOLD_CLASS=$(Get-HoldClass $Reason)"
    Write-Output "REPAIR_HOLD=$Reason"
    exit 0
}

# What a fixer or implementer is told about stopping (protocol §0.2, ADR-0022 §3-§4). One text for every mode.
$humanDecisionRule = @"
WHO DECIDES WHAT:
- The user decides product features, product behaviour and real external actions.
- You decide implementation for work the canonical documents already define: internal design, schema and migrations
  for an already-approved feature, endpoint shape, tests, repository rules, migration numbering, document pins.
  An implementation choice is never a reason to stop. Make it, state it in FIXER_SUMMARY, and the auditors verify it.

Stop WITHOUT changing anything only when the work itself needs a decision that is the user's:
- NEW_PRODUCT_FEATURE: a product feature the canonical requirements do not contain;
- PRODUCT_DIRECTION_UNDECIDED: a user-visible behaviour, UX or policy with several real product directions that no canonical text decides;
- BEYOND_USER_REQUIREMENT: a change beyond what the user asked for;
- LIVE: a side-effecting LIVE provider mutation or canary;
- SUPPLIER_ORDER: any real supplier order (발주);
- RESIDUAL_RISK_APPROVAL: accepting a material residual risk that cannot be removed inside scope;
- COST: a new or material unbudgeted payment or cost;
- EXTERNAL_DATA_TRANSFER: sending credentials, customer/order data or other non-public sensitive data externally;
- DESTRUCTIVE: an irreversible destructive operation, force-push or branch deletion.
A routine read-only provider call, read-back, health check or already-approved lookup is not a human stop by name alone.
Then finish with exactly:

FIXER_RESULT=HUMAN_DECISION_REQUIRED
FIXER_CATEGORY=<$(Get-HumanDecisionCategoryList)>
FIXER_REASON=<one line: the decision the user has to make>
"@

# A fixer's own stop. Only the closed category list is a human decision; any other refusal is a TECHNICAL_HOLD and the
# next attempt re-analyses the problem.
function Stop-FixerDecision {
    param([string]$Out, [string]$Who)

    if ($Out -match '(?m)^FIXER_RESULT=HUMAN_DECISION_REQUIRED\s*$') {
        $cat = [regex]::Match($Out, '(?m)^FIXER_CATEGORY=(\S+)\s*$').Groups[1].Value
        Write-Host "FIXER_CATEGORY=$(if ($cat) { $cat } else { 'UNSPECIFIED' })"

        if ($cat -and (Get-HoldClass $cat) -eq "HUMAN_DECISION_REQUIRED") {
            Stop-RepairHold "${Who}_HUMAN_DECISION_REQUIRED_$cat"
        }

        Stop-RepairHold "${Who}_DECLINED_WITHOUT_A_HUMAN_CATEGORY"
    }

    # the pre-ADR-0022 word: never a human decision by itself
    if ($Out -match '(?m)^FIXER_RESULT=HUMAN_HOLD\s*$') {
        Stop-RepairHold "${Who}_DECLINED_LEGACY_HUMAN_HOLD"
    }
}

# -------------------------------------------------
# I2: duplicate active slice PR prevention (V2 §0: the open-PR duplicate guard must conform).
#
# 새 slice PR 을 만들기 전에 열린 PR 전부(모든 페이지)를 읽고 slice identity 를 비교한다. branch 이름만으로 판단하지 않는다.
#   slice identity 출처: branch (feat/agent-host-<slice>-<main12> | fix/agent-host-remediation-main-<main12>),
#                        PR body (**Slice:** <slice> | "Post-merge full-audit remediation for exact main"),
#                        host registry (state\next-main-*.json / remediation-main-*.json 의 pr + slice_id).
#   target/base: base 가 main 이면 DUPLICATE_ACTIVE_SLICE_PR:#N, 다른 base 면 SAME_SLICE_PR_OTHER_BASE:#N (둘 다 HOLD).
#   active (B3): merged/closed PR 은 비활성 (open PR 만 목록에 나온다). 열린 같은-slice PR 은 기본적으로 ACTIVE.
#     V2 가 채택한 supersession authority 가 아직 없으므로 label 이나 다른 metadata 로는 절대 비활성화되지 않는다.
#     "superseded" label 은 advisory 로 기록만 한다.
#   active owner: author/assignees 는 기록만 한다 (GitHub 계정이 작성자를 증명하지 않으므로 판정에 쓰지 않는다).
# 목록을 끝까지 읽지 못하면 HOLD (OPEN_PR_LIST_UNREADABLE).
# -------------------------------------------------

function Get-AllOpenPrs {
    $all = New-Object System.Collections.Generic.List[object]

    for ($page = 1; $page -le 50; $page++) {
        $oldEap = $ErrorActionPreference

        try {
            $ErrorActionPreference = "Continue"
            $raw = gh api "repos/$repoSlug/pulls?state=open&per_page=100&page=$page" 2>> $script:nativeErrPath
            $code = $LASTEXITCODE
        }
        finally {
            $ErrorActionPreference = $oldEap
        }

        if ($code -ne 0 -or $null -eq $raw) {
            return $null
        }

        try {
            $arr = @(((@($raw) -join "`n") | ConvertFrom-Json) | ForEach-Object { $_ })
        }
        catch {
            return $null
        }

        foreach ($x in $arr) {
            if ($null -eq $x -or "$($x.number)" -notmatch '^\d+$') {
                return $null
            }

            $all.Add($x)
        }

        if ($arr.Count -lt 100) {
            return ,$all.ToArray()
        }
    }

    return $null
}

function Get-PrSliceIdentities {
    param($Pr)

    $ids = New-Object System.Collections.Generic.List[string]
    $ref = [string]$Pr.head.ref
    $body = ([string]$Pr.body).Replace("`r`n", "`n")

    $m = [regex]::Match($ref, '^feat/agent-host-(.+)-[0-9a-f]{12}$')
    if ($m.Success) { $ids.Add("slice:$($m.Groups[1].Value)|branch") }
    if ($ref -match '^fix/agent-host-remediation-main-[0-9a-f]{12}$') { $ids.Add("remediation|branch") }

    $m = [regex]::Match($body, '(?m)^\*\*Slice:\*\*\s+(\S+)')
    if ($m.Success) { $ids.Add("slice:$($m.Groups[1].Value -replace '[^A-Za-z0-9._-]', '-')|body") }
    if ($body -match '(?m)^Post-merge full-audit remediation for exact main') { $ids.Add("remediation|body") }

    foreach ($rf in @(Get-ChildItem $stateDir -Filter "next-main-*.json" -ErrorAction SilentlyContinue)) {
        try { $reg = Get-Content $rf.FullName -Raw -Encoding utf8 | ConvertFrom-Json } catch { $reg = $null }
        if ($reg -and "$($reg.pr)" -eq "$($Pr.number)" -and $reg.slice_id) { $ids.Add("slice:$($reg.slice_id)|registry") }
    }

    foreach ($rf in @(Get-ChildItem $stateDir -Filter "remediation-main-*.json" -ErrorAction SilentlyContinue)) {
        try { $reg = Get-Content $rf.FullName -Raw -Encoding utf8 | ConvertFrom-Json } catch { $reg = $null }
        if ($reg -and "$($reg.pr)" -eq "$($Pr.number)") { $ids.Add("remediation|registry") }
    }

    return ,$ids.ToArray()
}

function Assert-NoDuplicateSlicePr {
    param([string]$SliceKey)

    $open = Get-AllOpenPrs

    if ($null -eq $open) {
        Stop-RepairHold "OPEN_PR_LIST_UNREADABLE"
    }

    Write-Host "OPEN_PRS_SCANNED=$(@($open).Count) slice_identity=$SliceKey"

    foreach ($p in @($open | Sort-Object { [int]$_.number })) {
        $hits = @(Get-PrSliceIdentities $p | Where-Object { ($_ -split '\|')[0] -ceq $SliceKey })

        if ($hits.Count -eq 0) {
            continue
        }

        $via = @($hits | ForEach-Object { ($_ -split '\|')[1] }) -join ','
        $supersededLabel = @($p.labels | Where-Object { "$($_.name)" -ieq "superseded" }).Count -gt 0
        Write-Host "SAME_SLICE_OPEN_PR=#$($p.number) head=$($p.head.ref) base=$($p.base.ref) draft=$($p.draft) author=$($p.user.login) assignees=$(@($p.assignees | ForEach-Object { $_.login }) -join ',') via=$via active=True"

        if ($supersededLabel) {
            # advisory only: a label is not a content-bound user/architect supersession authority (B3)
            Write-Host "SAME_SLICE_OPEN_PR_ADVISORY=#$($p.number) label=superseded (ignored; open PR stays ACTIVE)"
        }

        if ("$($p.base.ref)" -eq "main") {
            Stop-RepairHold "DUPLICATE_ACTIVE_SLICE_PR:#$($p.number)"
        }

        Stop-RepairHold "SAME_SLICE_PR_OTHER_BASE:#$($p.number)"
    }
}

function New-HostWorktree {
    param(
        [string]$Path,
        [string]$Commit
    )

    Remove-HostWorktree -Path $Path

    Invoke-Git @("-C", $repoPath, "worktree", "add", "-f", "--detach", $Path, $Commit) | Out-Null

    if ($LASTEXITCODE -ne 0) {
        return $false
    }

    $script:activeWorktree = $Path

    $wtHead = "$(Invoke-Git @('-C', $Path, 'rev-parse', 'HEAD'))".Trim()
    return ($wtHead -eq $Commit)
}

function Sync-HostRef {
    param(
        [string]$RemoteRef,
        [string]$LocalRef,
        [string]$ExpectedSha
    )

    Invoke-Git @(
        "-C", $repoPath, "fetch", "--no-tags", "--quiet", "--refmap=", "origin",
        "+${RemoteRef}:$LocalRef"
    ) | Out-Null

    Invoke-Git @("-C", $repoPath, "cat-file", "-e", "$ExpectedSha^{commit}") | Out-Null
    return ($LASTEXITCODE -eq 0)
}

# Claude FIXER는 disposable worktree 안에서만 실행. push 불가.
function Invoke-IsolatedFixer {
    param(
        [string]$Prompt,
        [string]$Worktree
    )

    $hookDir = Join-Path $stateDir "repair-no-push-hooks"
    New-Item -ItemType Directory -Force -Path $hookDir | Out-Null

    # Repair sandbox에서는 push 차단.
    $prePush = Join-Path $hookDir "pre-push"

    @(
        '#!/bin/sh'
        'echo "ICBM Agent Host: push disabled inside repair sandbox." >&2'
        'exit 1'
    ) | Set-Content -Encoding ascii $prePush

    $fixerErr = Join-Path $logDir "$modeTag-fixer-$ts.stderr.txt"

    # 기존 환경 보존
    $oldGitCount = $env:GIT_CONFIG_COUNT
    $oldGitKey0  = $env:GIT_CONFIG_KEY_0
    $oldGitVal0  = $env:GIT_CONFIG_VALUE_0
    $oldGitKey1  = $env:GIT_CONFIG_KEY_1
    $oldGitVal1  = $env:GIT_CONFIG_VALUE_1
    $oldEap = $ErrorActionPreference

    try {
        # 1) pre-push hook
        # 2) --no-verify 등을 사용해도 origin push URL은 localhost 폐쇄 포트로 강제
        $env:GIT_CONFIG_COUNT = "2"

        $env:GIT_CONFIG_KEY_0 = "core.hooksPath"
        $env:GIT_CONFIG_VALUE_0 = $hookDir

        $env:GIT_CONFIG_KEY_1 = "remote.origin.pushurl"
        $env:GIT_CONFIG_VALUE_1 = "https://127.0.0.1:1/icbm-agent-host-push-disabled"

        $ErrorActionPreference = "Continue"

        Push-Location $Worktree

        try {
            $out = $Prompt | claude -p `
                --no-session-persistence `
                --permission-mode auto `
                --permission-prompts none `
                2> $fixerErr
        }
        finally {
            Pop-Location
        }
    }
    finally {
        $ErrorActionPreference = $oldEap
        $env:GIT_CONFIG_COUNT = $oldGitCount
        $env:GIT_CONFIG_KEY_0 = $oldGitKey0
        $env:GIT_CONFIG_VALUE_0 = $oldGitVal0
        $env:GIT_CONFIG_KEY_1 = $oldGitKey1
        $env:GIT_CONFIG_VALUE_1 = $oldGitVal1
    }

    return (@($out) -join "`n")
}

# Host-owned commit + push. 사용자 checkout이 아니라 host worktree에서 수행.
# 성공 시 새 commit SHA, 실패 시 "HOLD:<reason>".
function Publish-WorktreeCommit {
    param(
        [string]$Worktree,
        [string]$BaseHead,
        [string[]]$Files,
        [string]$Branch,
        [string]$Message
    )

    $wtHead = "$(Invoke-Git @('-C', $Worktree, 'rev-parse', 'HEAD'))".Trim()

    if ($wtHead -ne $BaseHead) {
        # FIXER가 sandbox 안에서 commit 했다면 host commit 1개로 합친다.
        Invoke-Git @("-C", $Worktree, "reset", "--soft", $BaseHead) | Out-Null

        if ($LASTEXITCODE -ne 0) {
            return "HOLD:WORKTREE_RESET_FAILED"
        }
    }

    Invoke-Git (@("-C", $Worktree, "add", "-A", "--") + $Files) | Out-Null

    if ($LASTEXITCODE -ne 0) {
        return "HOLD:GIT_ADD_FAILED"
    }

    $staged = @(
        Invoke-Git @("-C", $Worktree, "-c", "core.quotepath=false", "diff", "--cached", "--no-renames", "--name-only")
    ) | Where-Object { $_ }

    $unexpected = @($staged | Where-Object { $_ -notin $Files })

    if ($staged.Count -eq 0 -or $unexpected.Count -gt 0) {
        return "HOLD:STAGED_SET_MISMATCH"
    }

    Invoke-Git @("-C", $Worktree, "diff", "--cached", "--check") | Out-Null

    if ($LASTEXITCODE -ne 0) {
        return "HOLD:DIFF_CHECK_FAILED"
    }

    Invoke-Git @("-C", $Worktree, "commit", "--quiet", "-m", $Message) | Out-Null

    if ($LASTEXITCODE -ne 0) {
        return "HOLD:GIT_COMMIT_FAILED"
    }

    $newHead = "$(Invoke-Git @('-C', $Worktree, 'rev-parse', 'HEAD'))".Trim()

    # fast-forward push만 허용 (remote가 움직였으면 거부된다).
    # remote 이름 대신 URL로 push → 사용자 repo의 refs/remotes/origin/* 도 갱신하지 않는다.
    $pushUrl = "$(Invoke-Git @('-C', $repoPath, 'remote', 'get-url', '--push', 'origin'))".Trim()

    if (-not $pushUrl) {
        return "HOLD:PUSH_URL_NOT_FOUND"
    }

    Invoke-Git @("-C", $Worktree, "push", "--quiet", $pushUrl, "HEAD:refs/heads/$Branch") | Out-Null

    if ($LASTEXITCODE -ne 0) {
        return "HOLD:GIT_PUSH_FAILED"
    }

    return $newHead
}

Write-Host ""
Write-Host "========================================"
Write-Host " ICBM AUTO REPAIR V1.1 - ISOLATED"
Write-Host $(if ($RemediateMain) { " MODE: FULL-AUDIT REMEDIATION" } elseif ($ImplementNext) { " MODE: AUTO-NEXT IMPLEMENTATION" } else { " PR #$PrNumber" })
Write-Host "========================================"
Write-Host ""

if (-not $config.repair_loop.enabled) {
    Stop-RepairHold "REPAIR_LOOP_DISABLED"
}

$mainHead = "$(gh api "repos/$repoSlug/commits/main" --jq .sha)".Trim()

# =================================================
# REMEDIATION MODE
# =================================================

if ($RemediateMain) {

    if (-not $config.repair_loop.auto_remediate_full_audit_blocker) {
        Stop-RepairHold "AUTO_REMEDIATION_DISABLED"
    }

    $fullStatePath = Join-Path $stateDir "full-audit-state.json"

    if (-not (Test-Path $fullStatePath)) {
        Stop-RepairHold "NO_FULL_AUDIT_STATE"
    }

    $fullState = Get-Content $fullStatePath -Raw -Encoding utf8 | ConvertFrom-Json

    if ([string]$fullState.status -ne "BLOCKED") {
        Stop-RepairHold "FULL_AUDIT_NOT_BLOCKED"
    }

    if ([string]$fullState.main -ne $mainHead) {
        Stop-RepairHold "FULL_AUDIT_STALE_MAIN"
    }

    # --- repo owner architect ruling (exact main 기준 remediation 범위) ---
    # state\remediation-authorization.json { main, comment_id, items, allowed_files }
    # ruling이 있으면 FIXER에 전달하고, 수정 가능 파일을 allowed_files로 결정적으로 제한한다.
    $authPath = Join-Path $stateDir "remediation-authorization.json"
    $authBody = ""
    $authUrl = ""
    $authCommentId = ""
    $authFiles = @()

    if (Test-Path $authPath) {
        $auth = Get-Content $authPath -Raw -Encoding utf8 | ConvertFrom-Json

        if ([string]$auth.main -eq $mainHead) {
            $authCommentId = [string]$auth.comment_id
            $comment = gh api "repos/$repoSlug/issues/comments/$authCommentId" | ConvertFrom-Json
            $owner = ($repoSlug -split "/")[0]

            if (
                -not $comment -or
                [string]$comment.user.login -ne $owner -or
                -not ([string]$comment.body).Contains($mainHead)
            ) {
                Stop-RepairHold "REMEDIATION_AUTHORIZATION_INVALID"
            }

            $authBody = [string]$comment.body
            $authUrl = [string]$comment.html_url
            $authFiles = @($auth.allowed_files | Where-Object { $_ })

            if ($authFiles.Count -eq 0) {
                Stop-RepairHold "REMEDIATION_AUTHORIZATION_NO_FILES"
            }

            Write-Host "REMEDIATION_AUTHORIZATION=$authUrl"
            Write-Host "AUTHORIZED_FILES=$($authFiles -join ',')"
        }
    }

    $mainShort = $mainHead.Substring(0,12)
    $branch = "fix/agent-host-remediation-main-$mainShort"
    $registryPath = Join-Path $stateDir "remediation-main-$mainShort.json"

    # --- 멱등: 이미 이 main에 대해 만든 remediation PR이 있으면 재사용 ---
    if (Test-Path $registryPath) {
        $registry = Get-Content $registryPath -Raw -Encoding utf8 | ConvertFrom-Json

        if ($registry.pr) {
            Write-Output "REMEDIATION_PR=$($registry.pr)"
            Write-Output "REPAIR_RESULT=REMEDIATION_PR_EXISTS"
            exit 0
        }
    }

    # I2: 다른 main 에서 열린 remediation PR 이 아직 열려 있으면 새로 만들지 않는다
    Assert-NoDuplicateSlicePr -SliceKey "remediation"

    if (-not (Sync-HostRef -RemoteRef "refs/heads/main" -LocalRef "refs/icbm-agent-host/main" -ExpectedSha $mainHead)) {
        Stop-RepairHold "MAIN_NOT_FETCHED"
    }

    # --- 이 main 이후에 열린 PR이 있으면 (사람의 remediation 가능성) 중복 remediation 금지 ---
    # 이 main보다 먼저 열린 무관한 오래된 PR은 제외한다.
    $mainDate = [DateTimeOffset]::Parse(
        "$(Invoke-Git @('-C', $repoPath, 'show', '-s', '--format=%cI', $mainHead))".Trim()
    )

    $openPrsRaw = gh pr list --repo $repoSlug --base main --state open --json number,headRefName,createdAt |
        ConvertFrom-Json

    $openPrs = @(
        $openPrsRaw | Where-Object {
            $_ -and
            $_.headRefName -ne $branch -and
            [DateTimeOffset]::Parse("$($_.createdAt)") -ge $mainDate
        }
    )

    if ($openPrs.Count -gt 0) {
        foreach ($o in $openPrs) {
            Write-Host "OPEN_PR=#$($o.number) $($o.headRefName)"
        }

        Stop-RepairHold "OPEN_PR_ALREADY_EXISTS"
    }

    $remoteBranch = Invoke-Git @("-C", $repoPath, "ls-remote", "--heads", "origin", "refs/heads/$branch")

    if ($remoteBranch) {
        Stop-RepairHold "REMEDIATION_BRANCH_EXISTS"
    }

    $gptReport = Join-Path $stateDir "$($fullState.policy)-main-$mainShort-gpt.txt"
    $claudeReport = Join-Path $stateDir "$($fullState.policy)-main-$mainShort-claude.txt"

    $reportText = @(
        foreach ($rp in @($gptReport, $claudeReport)) {
            if (Test-Path $rp) {
                "===== $(Split-Path $rp -Leaf) ====="
                Get-Content $rp -Raw -Encoding utf8
            }
        }
    ) -join "`n"

    if (-not $reportText) {
        Stop-RepairHold "FULL_AUDIT_REPORTS_MISSING"
    }

    $remWorktree = Join-Path $repairRoot "remediate-main-$mainShort"

    if (-not (New-HostWorktree -Path $remWorktree -Commit $mainHead)) {
        Stop-RepairHold "WORKTREE_CREATE_FAILED"
    }

    $authSection = ""

    if ($authBody) {
        $authFileList = ($authFiles | ForEach-Object { "- $_" }) -join "`n"
        $authSection = @"
ARCHITECT RULING (repository owner, $authUrl) - AUTHORITATIVE FOR THIS REMEDIATION:
$authBody

Implement exactly the authorized items and nothing the ruling lists as not included.
You may modify ONLY these existing files:
$authFileList

"@
    }

    $remPrompt = @"
[ICBM-NEW] POST-MERGE FULL-AUDIT BLOCKER REMEDIATION

ROLE:
You are the implementation FIXER, not the auditor.

CANONICAL MAIN (exact, you are on a disposable detached worktree of it):
$mainHead

FULL-AUDIT REPORTS (GPT and Claude, both on this exact main):
$reportText

$authSection
SCOPE - fix every blocker in the reports:
- inconsistency between the canonical contract and the current implementation
- stale docs/tests
- runtime fail-close restoration where current behavior violates an existing invariant
- changes so an already existing owner/guard behaves as intended
- directly affected tests, and whatever implementation detail the fix needs

WORKING ENVIRONMENT:
- Shell commands, file search/read/edit and local tests/lint/type checks are allowed.
- Do NOT make provider/API/network calls. No LIVE. No canary. DRY_RUN only.
- Do NOT push. Do NOT merge. Do NOT create branches or PRs.

RULES:
- Make the smallest semantically complete correction.
- Preserve unrelated accepted behavior and safety invariants. Never weaken a safety gate to make a blocker go away.

$humanDecisionRule

Otherwise finish with:

FIXER_RESULT=READY
FIXER_SUMMARY=<one concise line>
"@

    Write-Host "[FIXER] remediation worktree"
    Write-Host "WORKTREE=$remWorktree"
    Write-Host ""

    $fixerOut = Invoke-IsolatedFixer -Prompt $remPrompt -Worktree $remWorktree

    Write-Host ""
    Write-Host "CLAUDE FIXER:"
    Write-Host $fixerOut
    Write-Host ""

    Stop-FixerDecision -Out $fixerOut -Who "FIXER"

    if ($fixerOut -notmatch '(?m)^FIXER_RESULT=READY\s*$') {
        Stop-RepairHold "CLAUDE_FIXER_RESULT_UNREADABLE"
    }

    $fixerSummary = [regex]::Match($fixerOut, '(?m)^FIXER_SUMMARY=(.+?)\s*$').Groups[1].Value

    # --- deterministic scope guards ---
    $deleted = @(
        Invoke-Git @("-C", $remWorktree, "-c", "core.quotepath=false", "diff", "--no-renames", "--name-only", "--diff-filter=D", $mainHead, "--")
    ) | Where-Object { $_ }

    $modified = @(
        Invoke-Git @("-C", $remWorktree, "-c", "core.quotepath=false", "diff", "--no-renames", "--name-only", $mainHead, "--")
    ) | Where-Object { $_ }

    $untracked = @(
        Invoke-Git @("-C", $remWorktree, "-c", "core.quotepath=false", "ls-files", "--others", "--exclude-standard")
    ) | Where-Object { $_ }

    $allChanged = @(@($modified) + @($untracked)) | Where-Object { $_ } | Sort-Object -Unique

    if ($allChanged.Count -eq 0) {
        Stop-RepairHold "FIXER_NO_CHANGES"
    }

    $migrationPaths = @(
        $allChanged | Where-Object {
            $_ -match '(^|/)migrations/' -or
            $_ -match '(^|/)alembic' -or
            $_ -match '(^|/)versions/.*\.py$'
        }
    )

    # Implementation detail is the fixer's to decide (ADR-0022 §3): a migration, a new file or a removed file is
    # reported for the auditors and never a hold. An explicit ruling's file list is still enforced, as a TECHNICAL_HOLD.
    foreach ($f in $migrationPaths) { Write-Host "SCHEMA_OR_MIGRATION=$f" }
    foreach ($f in $deleted) { Write-Host "REMOVED_FILE=$f" }

    if ($authFiles.Count -gt 0) {
        $outOfAuth = @($allChanged | Where-Object { $_ -cnotin $authFiles })

        if ($outOfAuth.Count -gt 0) {
            foreach ($f in $outOfAuth) { Write-Host "OUT_OF_AUTHORIZATION=$f" }
            Stop-RepairHold "SCOPE_OUTSIDE_THE_RULING"
        }
    }

    $currentMain = "$(gh api "repos/$repoSlug/commits/main" --jq .sha)".Trim()

    if ($currentMain -ne $mainHead) {
        Stop-RepairHold "MAIN_MOVED_DURING_FIX"
    }

    $newHead = Publish-WorktreeCommit `
        -Worktree $remWorktree `
        -BaseHead $mainHead `
        -Files $allChanged `
        -Branch $branch `
        -Message "fix: post-merge full-audit remediation (main $mainShort)"

    if ("$newHead" -like "HOLD:*") {
        Stop-RepairHold ("$newHead".Substring(5))
    }

    # 재시작 시 중복 방지: push 직후 registry 기록
    $registry = [ordered]@{
        main = $mainHead
        branch = $branch
        pushed_head = $newHead
        pr = $null
        authorization_comment_id = $authCommentId
        authorization_url = $authUrl
        audit_policy = [string]$fullState.policy
        updated_at = (Get-Date).ToString("o")
    }

    $registry | ConvertTo-Json -Depth 5 | Set-Content -Encoding utf8 $registryPath

    $gptSummary = if (Test-Path $gptReport) { [regex]::Match((Get-Content $gptReport -Raw -Encoding utf8), '(?m)^SUMMARY=(.+?)\s*$').Groups[1].Value } else { "" }
    $claudeSummary = if (Test-Path $claudeReport) { [regex]::Match((Get-Content $claudeReport -Raw -Encoding utf8), '(?m)^SUMMARY=(.+?)\s*$').Groups[1].Value } else { "" }

    $bodyPath = Join-Path $logDir "remediation-main-$mainShort-pr-body-$ts.md"

    $authLine = ""

    if ($authUrl) {
        $authLine = "**Authorization:** architect ruling $authUrl (only the items it authorizes; files: " + ($authFiles -join ", ") + ")`n"
    }

    $body = @"
Post-merge full-audit remediation for exact main ``$mainHead``.

Created automatically by ICBM Agent Host under the standing remediation authorization
(contract/implementation inconsistency, stale docs/tests, fail-close restoration, existing owner/guard, directly affected tests only).

$authLine
**Full-audit blockers**
- GPT: $gptSummary
- Claude: $claudeSummary

**Fixer summary**
- $fixerSummary

**Changed files**
$(($allChanged | ForEach-Object { "- ``$_``" }) -join "`n")

This PR follows the audit-before-CI loop: draft PR → GPT exact-head audit + independent Claude cross-audit → on BLOCKER fix (still draft, local checks) and re-audit until it passes → when GPT PASS and Claude PASS meet on the same exact HEAD, ready for review → FULL CI once → GREEN + MERGE_GUARD → merge.
$(if ($config.auto_merge -eq $true) { "AUTO_MERGE=TRUE only after MERGE_GUARD (CI GREEN + GPT PASS + Claude PASS on this exact HEAD/main, merged with expected_head_sha)." } else { "AUTO_MERGE=FALSE (host configuration)." })
"@

    [System.IO.File]::WriteAllText($bodyPath, $body, $nativeUtf8)

    $oldEap = $ErrorActionPreference

    try {
        $ErrorActionPreference = "Continue"
        $w = Invoke-GhWrite `
            -GhArgs @("pr", "create", "--repo", $repoSlug, "--base", "main", "--head", $branch, "--draft", "--title", "fix: post-merge full-audit remediation (main $mainShort)", "--body-file", $bodyPath) `
            -Site "run-repair:RemediateMain:pr-create" `
            -ErrPath $script:nativeErrPath

        if ($w.Refused) {
            Stop-RepairHold $w.Reason
        }

        $prUrl = $w.Output
        $prCreateExit = $w.ExitCode
    }
    finally {
        $ErrorActionPreference = $oldEap
    }

    $prMatch = [regex]::Match("$prUrl", '/pull/(\d+)')

    if ($prCreateExit -ne 0 -or -not $prMatch.Success) {
        Stop-RepairHold "PR_CREATE_FAILED"
    }

    $newPr = [int]$prMatch.Groups[1].Value

    $registry.pr = $newPr
    $registry.updated_at = (Get-Date).ToString("o")
    $registry | ConvertTo-Json -Depth 5 | Set-Content -Encoding utf8 $registryPath

    Remove-HostWorktree -Path $remWorktree

    Write-Host ""
    Write-Output "REMEDIATION_PR=$newPr"
    Write-Output "NEW_HEAD=$newHead"
    Write-Output "REPAIR_RESULT=REMEDIATION_PR_CREATED"
    Write-Output "NEXT=PR_LOOP"
    Write-Output "AUTO_MERGE=$(if ($config.auto_merge -eq $true) { 'TRUE_AFTER_MERGE_GUARD' } else { 'FALSE' })"
    exit 0
}

# =================================================
# AUTO-NEXT IMPLEMENTATION MODE
#
# run-lookahead-main-v1.ps1 이 exact main의 canonical 문서에서 고르고
# Claude가 교차확인한 slice만 구현한다. ALLOWED_PATHS 는 selector 의 추정이다: 밖의 파일은 보고만 하고 (SCOPE_WIDENED),
# 범위 판단은 감사자가 한다 (ADR-0022 §3). 사람에게 넘기는 것은 closed human-decision 목록뿐이다.
# =================================================

if ($ImplementNext) {

    if (-not ($config.auto_next.enabled -eq $true)) {
        Stop-RepairHold "AUTO_NEXT_DISABLED"
    }

    $mainShort = $mainHead.Substring(0,12)
    $specPath = Join-Path $stateDir "next-slice-main-$mainShort.json"

    if (-not (Test-Path $specPath)) {
        Stop-RepairHold "NO_NEXT_SLICE_FOR_MAIN"
    }

    $slice = Get-Content $specPath -Raw -Encoding utf8 | ConvertFrom-Json

    if ([string]$slice.main -ne $mainHead) {
        Stop-RepairHold "NEXT_SLICE_STALE_MAIN"
    }

    $sliceId = ([string]$slice.slice_id) -replace '[^A-Za-z0-9._-]', '-'
    $allowedPrefixes = @($slice.allowed_paths | ForEach-Object { ([string]$_).Trim().TrimStart("/").Replace("\", "/") } | Where-Object { $_ })

    if (-not $sliceId -or $allowedPrefixes.Count -eq 0) {
        Stop-RepairHold "NEXT_SLICE_INCOMPLETE"
    }

    $schemaAuthorized = ([string]$slice.schema_change) -like "AUTHORIZED_BY:*"

    $branch = "feat/agent-host-$sliceId-$mainShort"
    if ($branch.Length -gt 90) { $branch = $branch.Substring(0, 90) }
    $registryPath = Join-Path $stateDir "next-main-$mainShort.json"

    # --- 멱등: 이 main에서 이미 만든 next PR이 있으면 재사용 ---
    if (Test-Path $registryPath) {
        $registry = Get-Content $registryPath -Raw -Encoding utf8 | ConvertFrom-Json

        if ($registry.pr) {
            Write-Output "NEXT_PR=$($registry.pr)"
            Write-Output "REPAIR_RESULT=NEXT_PR_EXISTS"
            exit 0
        }
    }

    # I2: 같은 slice 의 active PR 이 (더 오래된 main 에서라도) 열려 있으면 새 PR 을 만들지 않는다 (#142 → #146 재현 방지)
    Assert-NoDuplicateSlicePr -SliceKey "slice:$sliceId"

    if (-not (Sync-HostRef -RemoteRef "refs/heads/main" -LocalRef "refs/icbm-agent-host/main" -ExpectedSha $mainHead)) {
        Stop-RepairHold "MAIN_NOT_FETCHED"
    }

    # --- 이 main 이후 열린 PR이 있으면 병행 작업과 충돌 가능 → HOLD ---
    $mainDate = [DateTimeOffset]::Parse(
        "$(Invoke-Git @('-C', $repoPath, 'show', '-s', '--format=%cI', $mainHead))".Trim()
    )

    $openPrsRaw = gh pr list --repo $repoSlug --base main --state open --json number,headRefName,createdAt |
        ConvertFrom-Json

    $openPrs = @(
        $openPrsRaw | Where-Object {
            $_ -and
            $_.headRefName -ne $branch -and
            [DateTimeOffset]::Parse("$($_.createdAt)") -ge $mainDate
        }
    )

    if ($openPrs.Count -gt 0) {
        foreach ($o in $openPrs) { Write-Host "OPEN_PR=#$($o.number) $($o.headRefName)" }
        Stop-RepairHold "OPEN_PR_ALREADY_EXISTS"
    }

    if (Invoke-Git @("-C", $repoPath, "ls-remote", "--heads", "origin", "refs/heads/$branch")) {
        Stop-RepairHold "NEXT_BRANCH_EXISTS"
    }

    $implWorktree = Join-Path $repairRoot "next-main-$mainShort"

    if (-not (New-HostWorktree -Path $implWorktree -Commit $mainHead)) {
        Stop-RepairHold "WORKTREE_CREATE_FAILED"
    }

    # --- 이전 main에서 main 이동으로 중단된 같은 slice의 draft가 있으면 적용 후 재검증 ---
    $draftPatchPath = Join-Path $stateDir "next-draft-$sliceId.patch"
    $draftMetaPath = Join-Path $stateDir "next-draft-$sliceId.json"
    $draftNote = ""

    if ((Test-Path $draftMetaPath) -and (Test-Path $draftPatchPath)) {
        $draft = Get-Content $draftMetaPath -Raw -Encoding utf8 | ConvertFrom-Json

        if ([string]$draft.slice_id -eq $sliceId -and [string]$draft.reason -eq "MAIN_MOVED_DURING_FIX" -and [string]$draft.base_main -ne $mainHead) {
            $applyOk = $false
            $oldEap = $ErrorActionPreference

            try {
                $ErrorActionPreference = "Continue"
                & git -C $implWorktree apply --3way --whitespace=nowarn $draftPatchPath 2>> $script:nativeErrPath | Out-Null
                $applyOk = ($LASTEXITCODE -eq 0)
                $unmerged = @(& git -C $implWorktree diff --name-only --diff-filter=U 2>> $script:nativeErrPath) | Where-Object { $_ }
                if ($unmerged.Count -gt 0) { $applyOk = $false }
            }
            finally {
                $ErrorActionPreference = $oldEap
            }

            if ($applyOk) {
                # 새 파일도 untracked 로 보이도록 index 해제 (scope guard 는 worktree 기준)
                Invoke-Git @("-C", $implWorktree, "reset", "-q") | Out-Null
                $movedLog = (@(Invoke-Git @("-C", $repoPath, "log", "--oneline", "--no-decorate", "$($draft.base_main)..$mainHead")) -join "`n")

                Write-Host "IMPL_DRAFT_APPLIED=$draftPatchPath (base $($draft.base_main))"

                $draftNote = @"

DRAFT ALREADY APPLIED (uncommitted) IN THIS WORKTREE:
An earlier run implemented this same slice on older main $($draft.base_main); it was not published only because main moved.
Its diff is applied here unchanged. Main commits since that draft:
$movedLog
Treat the draft as unreviewed work: re-verify it against the canonical documents at THIS main (the new commits may change
evidence, scope or wording), fix whatever conflicts, complete it, and re-run the tests, lint and type checks before finishing.
Previous implementer summary: $($draft.summary)
"@
            }
            else {
                Write-Host "IMPL_DRAFT_APPLY=FAILED (starting clean)"
                Invoke-Git @("-C", $implWorktree, "reset", "-q", "--hard", $mainHead) | Out-Null
                Invoke-Git @("-C", $implWorktree, "clean", "-fdq") | Out-Null
            }
        }
    }

    $implPrompt = @"
[ICBM-NEW] AUTO-NEXT SLICE IMPLEMENTATION

ROLE:
You are the implementer (Claude Code). Follow CLAUDE.md in this repository.

CANONICAL MAIN (exact; you are on a disposable detached worktree of it):
$mainHead

SELECTED NEXT SLICE (chosen from the canonical documents at this main and independently cross-checked):
SLICE_ID=$sliceId
SLICE_TITLE=$($slice.slice_title)
CANONICAL_SOURCES=$($slice.canonical_sources)
ALLOWED_PATHS=$($allowedPrefixes -join ', ')
SCHEMA_CHANGE=$($slice.schema_change)
ACCEPTANCE=$($slice.acceptance)
FORBIDDEN=$($slice.forbidden)

SPEC:
$($slice.spec)
$draftNote
RULES:
- Read CLAUDE.md (it imports documents/rules/), documents/roadmap/ROADMAP.md, documents/architecture/ARCHITECTURE.md and the CANONICAL_SOURCES first.
- Implement exactly this slice and nothing else. ALLOWED_PATHS is where the slice lives; touch another path only when
  the slice needs it (its tests, a document pin, a repository-map count, a migration) and say so in FIXER_SUMMARY.
- A schema or migration this already-approved slice needs is yours to design; keep it additive where you can.
- Add or update the tests that pin this slice; run the relevant local tests, lint and type checks.
- Do NOT make provider/marketplace/API/network calls. No LIVE. No canary. DRY_RUN only.
- Do NOT push, merge, or create branches or PRs. The host does that.
- Where canonical documents conflict on an implementation matter, follow the most recent ADR and say so.

$humanDecisionRule

Otherwise finish with:

FIXER_RESULT=READY
FIXER_SUMMARY=<one concise line>
"@

    Write-Host "[IMPLEMENTER] next-slice worktree"
    Write-Host "SLICE=$sliceId"
    Write-Host "WORKTREE=$implWorktree"
    Write-Host ""

    $implOut = Invoke-IsolatedFixer -Prompt $implPrompt -Worktree $implWorktree

    Write-Host ""
    Write-Host "CLAUDE IMPLEMENTER:"
    Write-Host $implOut
    Write-Host ""

    Stop-FixerDecision -Out $implOut -Who "IMPLEMENTER"

    if ($implOut -notmatch '(?m)^FIXER_RESULT=READY\s*$') {
        Stop-RepairHold "IMPLEMENTER_RESULT_UNREADABLE"
    }

    $implSummary = [regex]::Match($implOut, '(?m)^FIXER_SUMMARY=(.+?)\s*$').Groups[1].Value

    # --- deterministic scope guards ---
    $deleted = @(
        Invoke-Git @("-C", $implWorktree, "-c", "core.quotepath=false", "diff", "--no-renames", "--name-only", "--diff-filter=D", $mainHead, "--")
    ) | Where-Object { $_ }

    $modified = @(
        Invoke-Git @("-C", $implWorktree, "-c", "core.quotepath=false", "diff", "--no-renames", "--name-only", $mainHead, "--")
    ) | Where-Object { $_ }

    $untracked = @(
        Invoke-Git @("-C", $implWorktree, "-c", "core.quotepath=false", "ls-files", "--others", "--exclude-standard")
    ) | Where-Object { $_ }

    $allChanged = @(@($modified) + @($untracked)) | Where-Object { $_ } | Sort-Object -Unique

    if ($allChanged.Count -eq 0) {
        Stop-RepairHold "IMPLEMENTER_NO_CHANGES"
    }

    # Reported for the auditors, never a hold (ADR-0022 §3): the selector's ALLOWED_PATHS are an estimate.
    foreach ($f in $deleted) { Write-Host "REMOVED_FILE=$f" }

    $outOfSlice = @(
        $allChanged | Where-Object {
            $path = $_
            -not ($allowedPrefixes | Where-Object { $path -eq $_ -or $path.StartsWith($_.TrimEnd("/") + "/") })
        }
    )

    foreach ($f in $outOfSlice) { Write-Host "SCOPE_WIDENED=$f" }

    $migrationPaths = @(
        $allChanged | Where-Object {
            $_ -match '(^|/)migrations/' -or
            $_ -match '(^|/)alembic' -or
            $_ -match '(^|/)versions/.*\.py$'
        }
    )

    foreach ($f in $migrationPaths) { Write-Host "SCHEMA_OR_MIGRATION=$f (selector: $($slice.schema_change))" }

    $currentMain = "$(gh api "repos/$repoSlug/commits/main" --jq .sha)".Trim()

    if ($currentMain -ne $mainHead) {
        # 구현 결과를 버리지 않고 draft patch로 보존 → 새 main 재선택 시 같은 slice면 재사용
        Invoke-Git @("-C", $implWorktree, "add", "-A", "--") | Out-Null
        Invoke-Git @(
            "-C", $implWorktree, "diff", "--cached", "--binary", "--no-color",
            "--output=$draftPatchPath", $mainHead, "--"
        ) | Out-Null

        if ((Test-Path $draftPatchPath) -and (Get-Item $draftPatchPath).Length -gt 0) {
            [ordered]@{
                slice_id = $sliceId
                base_main = $mainHead
                moved_to_main = $currentMain
                patch = $draftPatchPath
                files = @($allChanged)
                summary = $implSummary
                reason = "MAIN_MOVED_DURING_FIX"
                saved_at = (Get-Date).ToString("o")
            } | ConvertTo-Json -Depth 5 | Set-Content -Encoding utf8 $draftMetaPath

            Write-Host "IMPL_DRAFT_SAVED=$draftPatchPath"
        }

        Stop-RepairHold "MAIN_MOVED_DURING_FIX"
    }

    $newHead = Publish-WorktreeCommit `
        -Worktree $implWorktree `
        -BaseHead $mainHead `
        -Files $allChanged `
        -Branch $branch `
        -Message "feat: $sliceId ($($slice.slice_title))"

    if ("$newHead" -like "HOLD:*") {
        Stop-RepairHold ("$newHead".Substring(5))
    }

    $registry = [ordered]@{
        main = $mainHead
        branch = $branch
        slice_id = $sliceId
        slice_spec = $specPath
        pushed_head = $newHead
        pr = $null
        updated_at = (Get-Date).ToString("o")
    }

    $registry | ConvertTo-Json -Depth 5 | Set-Content -Encoding utf8 $registryPath

    $bodyPath = Join-Path $logDir "next-main-$mainShort-pr-body-$ts.md"
    $changedList = ($allChanged | ForEach-Object { "- $_" }) -join "`n"

    $body = @"
Auto-next slice for exact main ``$mainHead``, selected by ICBM Agent Host from the canonical documents at that main
(GPT selection, independent Claude cross-check) — no separate authorization was required by the canonical documents.

**Slice:** $sliceId — $($slice.slice_title)
**Canonical sources:** $($slice.canonical_sources)
**Allowed paths:** $($allowedPrefixes -join ', ')
**Schema change:** $($slice.schema_change)
**Acceptance:** $($slice.acceptance)
**Forbidden:** $($slice.forbidden)

**Implementer summary**
- $implSummary

**Changed files**
$changedList
$(if (@($outOfSlice).Count -gt 0) { "`n**Outside the selector's allowed paths (for the auditors to judge)**`n" + ((@($outOfSlice) | ForEach-Object { "- $_" }) -join "`n") + "`n" })$(if (@($migrationPaths).Count -gt 0) { "`n**Schema or migration**`n" + ((@($migrationPaths) | ForEach-Object { "- $_" }) -join "`n") + "`n" })
No provider call, no LIVE, no canary. This PR follows the audit-before-CI loop: draft PR → GPT exact-head audit + independent Claude cross-audit → on BLOCKER fix (still draft, local checks) and re-audit until it passes → when GPT PASS and Claude PASS meet on the same exact HEAD, ready for review → FULL CI once → GREEN + MERGE_GUARD → merge.
$(if ($config.auto_merge -eq $true) { "AUTO_MERGE=TRUE only after MERGE_GUARD (CI GREEN + GPT PASS + Claude PASS on this exact HEAD/main, merged with expected_head_sha)." } else { "AUTO_MERGE=FALSE (host configuration)." })
"@

    [System.IO.File]::WriteAllText($bodyPath, $body, $nativeUtf8)

    $oldEap = $ErrorActionPreference

    try {
        $ErrorActionPreference = "Continue"
        $w = Invoke-GhWrite `
            -GhArgs @("pr", "create", "--repo", $repoSlug, "--base", "main", "--head", $branch, "--draft", "--title", "feat: $sliceId (auto-next, main $mainShort)", "--body-file", $bodyPath) `
            -Site "run-repair:ImplementNext:pr-create" `
            -ErrPath $script:nativeErrPath

        if ($w.Refused) {
            Stop-RepairHold $w.Reason
        }

        $prUrl = $w.Output
        $prCreateExit = $w.ExitCode
    }
    finally {
        $ErrorActionPreference = $oldEap
    }

    $prMatch = [regex]::Match("$prUrl", '/pull/(\d+)')

    if ($prCreateExit -ne 0 -or -not $prMatch.Success) {
        Stop-RepairHold "PR_CREATE_FAILED"
    }

    $newPr = [int]$prMatch.Groups[1].Value

    $registry.pr = $newPr
    $registry.updated_at = (Get-Date).ToString("o")
    $registry | ConvertTo-Json -Depth 5 | Set-Content -Encoding utf8 $registryPath

    Remove-HostWorktree -Path $implWorktree
    Remove-Item -LiteralPath $draftPatchPath, $draftMetaPath -Force -ErrorAction SilentlyContinue

    Write-Host ""
    Write-Output "NEXT_PR=$newPr"
    Write-Output "NEW_HEAD=$newHead"
    Write-Output "REPAIR_RESULT=NEXT_PR_CREATED"
    Write-Output "NEXT=PR_LOOP"
    exit 0
}

# =================================================
# PR REPAIR MODE
# =================================================

if ($PrNumber -le 0) {
    Stop-RepairHold "PR_NUMBER_REQUIRED"
}

$repairStatePath = Join-Path $hostRoot "state\repair-pr-$PrNumber.json"

if (
    $AuditSource -eq "GPT" -and
    -not $config.repair_loop.auto_fix_on_gpt_blocker
) {
    Stop-RepairHold "AUTO_FIX_GPT_BLOCKER_DISABLED"
}

if (
    $AuditSource -eq "CLAUDE" -and
    -not $config.repair_loop.auto_fix_on_claude_blocker
) {
    Stop-RepairHold "AUTO_FIX_CLAUDE_BLOCKER_DISABLED"
}

# -------------------------------------------------
# Current GitHub state
# -------------------------------------------------

$pr = gh pr view $PrNumber `
    --repo $repoSlug `
    --json number,title,state,headRefOid,headRefName,baseRefName |
    ConvertFrom-Json

if ($pr.state -ne "OPEN") {
    Stop-RepairHold "PR_NOT_OPEN"
}

$prHead = [string]$pr.headRefOid

# -------------------------------------------------
# Repair cycles (protocol §5.1; ADR-0022 §4)
#
# A count never hands the PR to the user. After repair_loop.max_cycles attempts the fixer stops repeating itself: every
# later attempt is an INDEPENDENT RE-ANALYSIS, given the blockers the earlier attempts left, and told to take another
# approach. The number of repairs never ends the loop and never hands it over (protocol §1, §5.1).
# -------------------------------------------------

$cycles = 0
$blockerHistory = @()

if (Test-Path $repairStatePath) {
    try {
        $repairState = Get-Content $repairStatePath -Raw -Encoding utf8 |
            ConvertFrom-Json

        $cycles = [int]$repairState.cycles
        $blockerHistory = @($repairState.history | Where-Object { $_ } | ForEach-Object { [string]$_ })
    }
    catch {
        $cycles = 0
        $blockerHistory = @()
    }
}

$reanalysis = ($cycles -ge $maxCycles)

if ($reanalysis) {
    Write-Host "CYCLES=$cycles"
    Write-Output "REPAIR_MODE=INDEPENDENT_REANALYSIS"
}

# -------------------------------------------------
# Discover current audit policy/cache
# -------------------------------------------------

$auditText = Get-Content $auditScript -Raw -Encoding utf8

$policyMatch = [regex]::Match(
    $auditText,
    '\$auditPolicyVersion\s*=\s*"([^"]+)"'
)

if (-not $policyMatch.Success) {
    Stop-RepairHold "AUDIT_POLICY_NOT_FOUND"
}

$auditPolicyVersion = $policyMatch.Groups[1].Value

$mainShort = $mainHead.Substring(0,12)
$headShort = $prHead.Substring(0,12)

$cacheKey = "$auditPolicyVersion-pr-$PrNumber-main-$mainShort-head-$headShort"

# AGENT_HOST_PROTOCOL_V2: audit identity = (HEAD, packet_digest). BLOCKER 는 PASS 캐시가 아니라
# 이번 run-audit 결과 파일(<key>-pkt-<digest12>-<auditor>.result.txt)에만 있다. 포인터: state\packets\pr-<N>-head-<12>.current
$packetPtr = Join-Path $hostRoot "state\packets\pr-$PrNumber-head-$headShort.current"
$packetDigest = ""

if (Test-Path $packetPtr) {
    $ptrText = Get-Content $packetPtr -Raw -Encoding utf8

    if (
        $ptrText -match "(?m)^HEAD=$prHead\s*$" -and
        $ptrText -match "(?m)^MAIN=$mainHead\s*$" -and
        $ptrText -match "(?m)^POLICY_VERSION=$([regex]::Escape($auditPolicyVersion))\s*$"
    ) {
        $packetDigest = [regex]::Match($ptrText, '(?m)^PACKET_DIGEST=([0-9a-f]{64})\s*$').Groups[1].Value
    }
}

if (-not $packetDigest) {
    Stop-RepairHold "NO_EXACT_HEAD_${AuditSource}_AUDIT"
}

$auditCache = if ($AuditSource -eq "GPT") {
    Join-Path $hostRoot "state\$cacheKey-pkt-$($packetDigest.Substring(0,12))-gpt.result.txt"
}
else {
    Join-Path $hostRoot "state\$cacheKey-pkt-$($packetDigest.Substring(0,12))-claude.result.txt"
}

if (-not (Test-Path $auditCache)) {
    Stop-RepairHold "NO_EXACT_HEAD_${AuditSource}_AUDIT"
}

$auditResult = Get-Content $auditCache -Raw -Encoding utf8

if ($auditResult -notmatch "(?m)^PACKET_DIGEST=$packetDigest\s*$") {
    Stop-RepairHold "STALE_AUDIT"
}

$headMatch = [regex]::Match(
    $auditResult,
    '(?m)^AUDIT_HEAD=(.+?)\s*$'
)

$verdictMatch = [regex]::Match(
    $auditResult,
    '(?m)^VERDICT=(PASS|BLOCKER|INSUFFICIENT)\s*$'
)

$summaryMatch = [regex]::Match(
    $auditResult,
    '(?m)^SUMMARY=(.+?)\s*$'
)

if (
    -not $headMatch.Success -or
    $headMatch.Groups[1].Value.Trim() -ne $prHead
) {
    Stop-RepairHold "STALE_AUDIT"
}

if (-not $verdictMatch.Success) {
    Stop-RepairHold "AUDIT_VERDICT_UNREADABLE"
}

$verdict = $verdictMatch.Groups[1].Value
$blocker = $summaryMatch.Groups[1].Value.Trim()

if ($verdict -eq "INSUFFICIENT") {
    Stop-RepairHold "AUDIT_INSUFFICIENT"
}

if ($verdict -ne "BLOCKER") {
    Stop-RepairHold "NO_AUDIT_BLOCKER"
}

# -------------------------------------------------
# Freeze current PR scope
# -------------------------------------------------

$allowedFiles = @(
    gh api --paginate `
        "repos/$repoSlug/pulls/$PrNumber/files?per_page=100" `
        --jq '.[].filename'
) | Where-Object { $_ }

if ($allowedFiles.Count -eq 0) {
    Stop-RepairHold "NO_ALLOWED_FILES"
}

Write-Host "HEAD       : $prHead"
Write-Host "MAIN       : $mainHead"
Write-Host "BLOCKER    : $blocker"
Write-Host "CYCLE      : $($cycles + 1) $(if ($reanalysis) { '(independent re-analysis)' } else { "of the first $maxCycles" })"
Write-Host ""

# -------------------------------------------------
# Claude FIXER — isolated disposable worktree
# -------------------------------------------------

$fileList = ($allowedFiles | ForEach-Object { "- $_" }) -join "`n"

$repairWorktree = Join-Path `
    $repairRoot `
    "repair-pr-$PrNumber-$headShort"

$patchPath = Join-Path `
    $hostRoot `
    "state\repair-pr-$PrNumber-$headShort.patch"

Remove-Item $patchPath -Force -ErrorAction SilentlyContinue

if (-not (Sync-HostRef -RemoteRef "refs/heads/$($pr.headRefName)" -LocalRef "refs/icbm-agent-host/pr-$PrNumber" -ExpectedSha $prHead)) {
    if (-not (Sync-HostRef -RemoteRef "refs/pull/$PrNumber/head" -LocalRef "refs/icbm-agent-host/pr-$PrNumber" -ExpectedSha $prHead)) {
        Stop-RepairHold "PR_HEAD_NOT_FETCHED"
    }
}

if (-not (New-HostWorktree -Path $repairWorktree -Commit $prHead)) {
    Stop-RepairHold "WORKTREE_CREATE_FAILED"
}

$reanalysisNote = ""

if ($reanalysis) {
    $reanalysisNote = @"

INDEPENDENT RE-ANALYSIS (attempt $($cycles + 1)):
$cycles earlier repair attempts did not end this PR's blockers. Do not repeat them. The blockers they were given, oldest first:
$(($blockerHistory | ForEach-Object { "- $_" }) -join "`n")
Re-derive the problem from the canonical documents and the code as they are now. Decide what the earlier attempts
misunderstood, take a different approach, and fix the root cause so that this class of blocker cannot return.
"@
}

$fixPrompt = @"
[ICBM-NEW] CURRENT PR exact-head BLOCKER repair

ROLE:
You are the implementation FIXER, not the auditor.

PR:
#$PrNumber

EXACT PR HEAD:
$prHead

CANONICAL MAIN:
$mainHead

$AuditSource AUDIT BLOCKER:
$blocker

SCOPE:
The slice this PR implements. These are the files it already changes:

$fileList

Prefer them. Add or change another file only when the fix needs it (a test, a fixture, a document pin, a repository-map
count, a migration renumber) and say so in FIXER_SUMMARY.
$reanalysisNote

WORKING ENVIRONMENT:
- You are inside a disposable repair worktree.
- Shell commands are allowed.
- File search/read/edit is allowed.
- Relevant local tests/lint/type checks are allowed.
- Do NOT make provider/API/network calls.
- Do NOT push.
- Do NOT merge.
- Do NOT enable LIVE.
- Do NOT open a canary.

RULES:
- Fix the blocker above, and every other place in this PR with the same weakness.
- Read the canonical context the blocker touches.
- Make the smallest semantically complete correction.
- Preserve unrelated accepted behavior and safety invariants. Never weaken a safety gate to make a blocker go away.
- Do not adopt unrelated endpoints, schema, migrations or runtime behavior.

$humanDecisionRule

Otherwise finish with:

FIXER_RESULT=READY
FIXER_SUMMARY=<one concise line>
"@

Write-Host "[FIXER] isolated worktree"
Write-Host "WORKTREE=$repairWorktree"
Write-Host ""

$claudeOut = Invoke-IsolatedFixer -Prompt $fixPrompt -Worktree $repairWorktree

Write-Host ""
Write-Host "CLAUDE FIXER:"
Write-Host $claudeOut
Write-Host ""

Stop-FixerDecision -Out $claudeOut -Who "FIXER"

if ($claudeOut -notmatch '(?m)^FIXER_RESULT=READY\s*$') {
    Stop-RepairHold "CLAUDE_FIXER_RESULT_UNREADABLE"
}

# Exact HEAD와 비교한 전체 repair delta (새 파일 포함).
# Claude가 sandbox 안에서 로컬 commit을 했더라도 포함된다.
$untracked = @(
    Invoke-Git @("-C", $repairWorktree, "-c", "core.quotepath=false", "ls-files", "--others", "--exclude-standard")
) | Where-Object { $_ }

$modified = @(
    @(Invoke-Git @("-C", $repairWorktree, "-c", "core.quotepath=false", "diff", "--no-renames", "--name-only", $prHead, "--")) + $untracked
) | Where-Object { $_ } | Sort-Object -Unique

if ($modified.Count -eq 0) {
    Stop-RepairHold "FIXER_NO_CHANGES"
}

# Reported for the auditors, never a hold (ADR-0022 §3): a fix may need a file the PR did not touch yet.
foreach ($file in @($modified | Where-Object { $_ -notin $allowedFiles })) {
    Write-Host "SCOPE_WIDENED=$file"
}

# Claude 작업 중 remote PR/main 이동 여부 확인
$remotePrHead = gh pr view $PrNumber `
    --repo $repoSlug `
    --json headRefOid `
    --jq .headRefOid

$remotePrHead = "$remotePrHead".Trim()

$currentMain = "$(gh api "repos/$repoSlug/commits/main" --jq .sha)".Trim()

if ($remotePrHead -ne $prHead) {
    Stop-RepairHold "REMOTE_PR_HEAD_MOVED_DURING_FIX"
}

if ($currentMain -ne $mainHead) {
    Stop-RepairHold "MAIN_MOVED_DURING_FIX"
}

# 감사 추적용 patch 기록. 중요: --output은 반드시 "--" pathspec 앞에 둔다.
Invoke-Git @(
    "-C", $repairWorktree, "diff", "--binary", "--no-color",
    "--output=$patchPath", $prHead, "--"
) | Out-Null

Write-Host "ISOLATION_CHECK=PASS"
Write-Host "SCOPE_CHECK=PASS"
Write-Host "MODIFIED_FILES=$($modified.Count)"

# -------------------------------------------------
# Host-owned commit + push (host worktree에서, 사용자 checkout 불변)
# -------------------------------------------------

$newHead = Publish-WorktreeCommit `
    -Worktree $repairWorktree `
    -BaseHead $prHead `
    -Files $modified `
    -Branch $pr.headRefName `
    -Message "fix: address exact-head audit blocker (#$PrNumber)"

if ("$newHead" -like "HOLD:*") {
    Stop-RepairHold ("$newHead".Substring(5))
}

# GitHub PR head 반영 대기 (짧은 지연 허용)
$observedHead = ""

for ($i = 0; $i -lt 10; $i++) {
    $observedHead = gh pr view $PrNumber `
        --repo $repoSlug `
        --json headRefOid `
        --jq .headRefOid

    $observedHead = "$observedHead".Trim()

    if ($observedHead -eq $newHead) {
        break
    }

    Start-Sleep -Seconds 3
}

if ($observedHead -ne $newHead) {
    Stop-RepairHold "NEW_HEAD_NOT_OBSERVED"
}

$cycles++

$repairState = [ordered]@{
    pr = $PrNumber
    cycles = $cycles
    previous_head = $prHead
    new_head = $newHead
    main = $mainHead
    blocker_source = $AuditSource
    blocker = $blocker
    history = @(@($blockerHistory) + @("[$AuditSource] $blocker") | Select-Object -Last 12)
    updated_at = (Get-Date).ToString("o")
}

$repairState |
    ConvertTo-Json -Depth 10 |
    Set-Content -Encoding utf8 $repairStatePath

Remove-HostWorktree -Path $repairWorktree

Write-Host ""
Write-Output "REPAIR_RESULT=PUSHED"
Write-Output "OLD_HEAD=$prHead"
Write-Output "NEW_HEAD=$newHead"
Write-Output "NEXT=WAIT_FOR_CI"
Write-Output "AUTO_MERGE=$(if ($config.auto_merge -eq $true) { 'TRUE_AFTER_MERGE_GUARD' } else { 'FALSE' })"
Write-Host ""
