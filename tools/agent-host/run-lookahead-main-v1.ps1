param(
    # orchestrator가 넘기는 post-merge 감사 main. 다르면 재선택하지 않는다.
    [string]$ExpectedMain = ""
)

# AUTO-NEXT ROADMAP SELECTION
#
# full-main DUAL PASS 이후, exact 최신 main의 canonical 문서(ROADMAP/ADR/acceptance/CLAUDE.md)를
# 매번 새로 읽어 다음 미완료 단계를 고른다. 과거 NEXT 슬롯/계획은 사용하지 않는다.
#
# PROCEED: canonical 문서가 scope와 순서를 이미 정했고, 별도 승인/정책 결정/LIVE/provider/canary/
#          residual-risk 승인이 필요 없는 단계 → run-repair -ImplementNext 로 자동 구현.
# HOLD   : 그 외 → 사유 분류와 함께 사람에게.
# DONE   : ROADMAP의 모든 단계 완료.
#
# GPT가 선택하고, PROCEED일 때만 Claude가 canonical 문서로 독립 교차확인한다.

$ErrorActionPreference = "Stop"

$utf8 = New-Object System.Text.UTF8Encoding($false)
$script:OutputEncoding = $utf8
[Console]::OutputEncoding = $utf8

$hostRoot = $PSScriptRoot
$configPath = Join-Path $hostRoot "state\orchestrator-config.json"
$stateDir = Join-Path $hostRoot "state"
$logDir = Join-Path $hostRoot "logs"
$worktreeRoot = Join-Path $hostRoot "worktrees"
$selectWorktree = Join-Path $worktreeRoot "next-select-main"

New-Item -ItemType Directory -Force -Path $logDir, $worktreeRoot | Out-Null

$config = Get-Content $configPath -Raw -Encoding utf8 | ConvertFrom-Json

$repoSlug = [string]$config.repository
# 사용자 repo는 object DB / origin remote 로만 사용한다.
$repoPath = [string]$config.repo_path

$ts = Get-Date -Format "yyyyMMdd-HHmmss"
$script:nativeErrPath = Join-Path $logDir "next-select-native-$ts.stderr.txt"

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

function Get-Field {
    param(
        [string]$Text,
        [string]$Name
    )

    $m = [regex]::Match($Text, "(?m)^$Name=(.*?)\s*$")

    if ($m.Success) {
        return $m.Groups[1].Value.Trim()
    }

    return ""
}

Write-Host ""
Write-Host "========================================"
Write-Host " AUTO-NEXT ROADMAP SELECTION"
Write-Host "========================================"

$mainHead = "$(gh api "repos/$repoSlug/commits/main" --jq .sha)".Trim()

Write-Host "MAIN=$mainHead"

if ($ExpectedMain -and $ExpectedMain -ne $mainHead) {
    Write-Output "NEXT_HOLD=MAIN_MOVED"
    return
}

# --- 선택은 post-merge 감사 DUAL PASS가 끝난 exact main에서만 ---
$baselinePath = Join-Path $stateDir "full-audit-baseline.json"
$baseline = $null

if (Test-Path $baselinePath) {
    $baseline = Get-Content $baselinePath -Raw -Encoding utf8 | ConvertFrom-Json
}

if (-not $baseline -or [string]$baseline.main -ne $mainHead -or [string]$baseline.status -ne "DUAL_PASS") {
    Write-Output "NEXT_HOLD=MAIN_NOT_DUAL_PASS_AUDITED"
    return
}

Invoke-Git @(
    "-C", $repoPath, "fetch", "--no-tags", "--quiet", "--refmap=", "origin",
    "+refs/heads/main:refs/icbm-agent-host/main"
) | Out-Null

if (-not (Initialize-HostWorktree -Path $selectWorktree -Commit $mainHead)) {
    Write-Output "NEXT_HOLD=SELECT_WORKTREE_FAILED"
    return
}

$mainShort = $mainHead.Substring(0,12)
$gptOut = Join-Path $stateDir "next-select-main-$mainShort-gpt.txt"
$claudeOut = Join-Path $stateDir "next-select-main-$mainShort-claude.txt"
$specPath = Join-Path $stateDir "next-slice-main-$mainShort.json"
$selectionPath = Join-Path $stateDir "next-selection.json"

function Save-Selection {
    param(
        [string]$Decision,
        [string]$Category = "",
        [string]$Reason = "",
        [string]$SliceId = "",
        [string]$Title = ""
    )

    [ordered]@{
        main = $mainHead
        decision = $Decision
        hold_category = $Category
        hold_reason = $Reason
        slice_id = $SliceId
        slice_title = $Title
        gpt_selection = $gptOut
        claude_check = $claudeOut
        updated_at = (Get-Date).ToString("o")
    } | ConvertTo-Json -Depth 5 | Set-Content -Encoding utf8 $selectionPath
}

# -------------------------------------------------
# 1. GPT selection (fresh canonical read at exact main)
# -------------------------------------------------

$selectPrompt = @"
[ICBM-NEW] AUTO-NEXT ROADMAP SELECTION

ROLE:
Architect/planner. READ ONLY.

CANONICAL MAIN:
$mainHead

HOST-VERIFIED FACTS:
- the working directory is a host-owned worktree at exact canonical main
- the post-merge audit of this exact main is GPT PASS + Claude PASS with no blocker

TASK:
Read the CURRENT canonical documents on this exact main. Do not rely on any earlier plan, lookahead or memory:
- CLAUDE.md
- ROADMAP.md (especially §12 Development sequence and §14 Immediate next work)
- docs/ARCHITECTURE.md
- relevant docs/adr/*
- relevant docs/acceptance/*

Identify the single next incomplete step in canonical order.

DECISION=PROCEED only if ALL of these hold:
1. The canonical documents at this main already fix both the scope and the order of that step (what to build, where, and its acceptance).
2. No canonical document says this step still needs its own/separate authorization, a kickoff, an architect decision or user approval that is not already recorded in a canonical document at this main.
   A canonical standing authorization (for example an ADR such as ADR-0020) whose conditions this step fully meets IS such a recorded authorization.
3. It needs no new architecture or policy decision and no scope beyond what the canonical documents define.
4. It performs no real provider/marketplace call, no LIVE switch and no real canary, and needs no residual-risk acceptance.
   Endpoint adoption in code (request/response/error classification, no call) is allowed only where a canonical contract already defines it.
5. The canonical documents do not conflict about it.
6. Any schema/migration/data model it needs is already concretely decided by a canonical contract (then SCHEMA_CHANGE=AUTHORIZED_BY:file:section); otherwise HOLD.
7. It is exactly one slice: never combine two canonically separate slices (for example CREATE and SEARCH) into one.

A step whose scope and order are fixed by the canonical documents and which they do not gate behind a separate authorization is PROCEED. Do not hold it merely to ask permission.
Do not infer authorization merely because a step appears in ROADMAP either: if the canonical text gates it behind a separate authorization, that gate stands.

Otherwise DECISION=HOLD with the most specific HOLD_CATEGORY.
If every roadmap step is complete, DECISION=DONE.

RULES:
- READ ONLY. No edits, commits, pushes, branches or PRs.
- No provider/marketplace calls. No LIVE. No canary.
- Cite exact file:section evidence.

Return EXACTLY these lines first:

NEXT_MAIN=$mainHead
DECISION=<PROCEED|HOLD|DONE>
SLICE_ID=<short-kebab-id or NONE>
SLICE_TITLE=<one line>
HOLD_CATEGORY=<NONE|ROADMAP_ADR_CONFLICT|NEXT_UNCLEAR|ARCHITECTURE_OR_POLICY|SCOPE_EXPANSION|PROVIDER_CALL|LIVE|CANARY|RESIDUAL_RISK_APPROVAL|SEPARATE_AUTHORIZATION_REQUIRED|USER_JUDGMENT>
HOLD_REASON=<one line with file:section evidence, or NONE>
CANONICAL_SOURCES=<file:section references that fix scope and order>
ALLOWED_PATHS=<comma-separated repository path prefixes the implementation may touch, or NONE>
SCHEMA_CHANGE=<NONE|AUTHORIZED_BY:file:section>
ACCEPTANCE=<one line>
FORBIDDEN=<one line>

Then:

SPEC:
<concise implementation specification for PROCEED, at most 40 lines; NONE otherwise>
"@

$gptValid = $false

if (Test-Path $gptOut) {
    $gptText = Get-Content $gptOut -Raw -Encoding utf8
    $gptValid = ((Get-Field $gptText "NEXT_MAIN") -eq $mainHead -and (Get-Field $gptText "DECISION"))
}

if ($gptValid) {
    Write-Host "NEXT_SELECT_GPT_CACHE=HIT"
}
else {
    Write-Host "NEXT_SELECT_GPT_CACHE=MISS"

    $gptErr = Join-Path $logDir "next-select-main-$mainShort-gpt-$ts.stderr.txt"
    Remove-Item $gptOut -Force -ErrorAction SilentlyContinue

    $oldEap = $ErrorActionPreference

    try {
        $ErrorActionPreference = "Continue"

        $selectPrompt | codex exec `
            -C $selectWorktree `
            -s read-only `
            --ephemeral `
            --model gpt-5.6-sol `
            --config 'model_reasoning_effort="high"' `
            -o $gptOut `
            - `
            2> $gptErr |
            Out-Host

        $gptExit = $LASTEXITCODE
    }
    finally {
        $ErrorActionPreference = $oldEap
    }

    if ($gptExit -ne 0 -or -not (Test-Path $gptOut)) {
        Write-Host "GPT_EXIT=$gptExit"
        Write-Output "NEXT_HOLD=SELECTOR_GPT_EXEC_FAILED"
        return
    }

    Add-ToolStamp -Path $gptOut -Auditor "GPT" -Policy "auto-next-select"
    $gptText = Get-Content $gptOut -Raw -Encoding utf8
}

$decision = Get-Field $gptText "DECISION"
$sliceId = (Get-Field $gptText "SLICE_ID") -replace '[^A-Za-z0-9._-]', '-'
$sliceTitle = Get-Field $gptText "SLICE_TITLE"
$holdCategory = Get-Field $gptText "HOLD_CATEGORY"
$holdReason = Get-Field $gptText "HOLD_REASON"
$allowedPaths = Get-Field $gptText "ALLOWED_PATHS"
$schemaChange = Get-Field $gptText "SCHEMA_CHANGE"

Write-Host ""
Write-Host "GPT_DECISION=$decision"
Write-Host "GPT_SLICE=$sliceId $sliceTitle"
Write-Host "GPT_HOLD=$holdCategory $holdReason"

if ((Get-Field $gptText "NEXT_MAIN") -ne $mainHead -or $decision -notin @("PROCEED", "HOLD", "DONE")) {
    Save-Selection -Decision "HOLD" -Category "NEXT_UNCLEAR" -Reason "selector output unreadable"
    Write-Output "NEXT_DECISION=HOLD"
    Write-Output "NEXT_HOLD_CATEGORY=NEXT_UNCLEAR"
    return
}

if ($decision -eq "DONE") {
    Save-Selection -Decision "DONE"
    Write-Output "NEXT_DECISION=DONE"
    return
}

if ($decision -eq "HOLD") {
    if (-not $holdCategory -or $holdCategory -eq "NONE") {
        $holdCategory = "NEXT_UNCLEAR"
    }

    Save-Selection -Decision "HOLD" -Category $holdCategory -Reason $holdReason -SliceId $sliceId -Title $sliceTitle
    Write-Output "NEXT_DECISION=HOLD"
    Write-Output "NEXT_HOLD_CATEGORY=$holdCategory"
    Write-Output "NEXT_HOLD_REASON=$holdReason"
    return
}

# PROCEED 형식 검증 (결정적)
if (-not $sliceId -or $sliceId -eq "NONE" -or -not $allowedPaths -or $allowedPaths -eq "NONE") {
    Save-Selection -Decision "HOLD" -Category "NEXT_UNCLEAR" -Reason "PROCEED without slice id or allowed paths"
    Write-Output "NEXT_DECISION=HOLD"
    Write-Output "NEXT_HOLD_CATEGORY=NEXT_UNCLEAR"
    return
}

# -------------------------------------------------
# 2. Claude independent confirmation (PROCEED only)
# -------------------------------------------------

$checkPrompt = @"
[ICBM-NEW] AUTO-NEXT SELECTION CROSS-CHECK

ROLE:
Independent architect. READ ONLY.

CANONICAL MAIN:
$mainHead

A planner proposes that the next roadmap step below may be implemented automatically, without a new user
authorization. Verify this against the CURRENT canonical documents on this exact main yourself
(CLAUDE.md, ROADMAP.md §12/§14, docs/ARCHITECTURE.md, relevant docs/adr/*, docs/acceptance/*).

AGREE only if ALL hold:
1. It is the next incomplete step in canonical order.
2. The canonical documents already fix its scope and order.
3. No canonical document gates it behind a separate authorization, kickoff, architect decision or user approval
   that is not already recorded in a canonical document at this main. A canonical standing authorization
   (for example ADR-0020) whose conditions it fully meets counts as recorded authorization.
4. It needs no new architecture/policy decision, no scope expansion, no provider call, no LIVE, no canary
   and no residual-risk acceptance.
5. ALLOWED_PATHS and SCHEMA_CHANGE below are no wider than the canonical documents authorize.

PROPOSAL:
$gptText

Return EXACTLY these lines first:

CHECK_MAIN=$mainHead
CHECK=<AGREE|DISAGREE>
CHECK_REASON=<one line with file:section evidence>
"@

$claudeValid = $false

if (Test-Path $claudeOut) {
    $claudeText = Get-Content $claudeOut -Raw -Encoding utf8
    $claudeValid = ((Get-Field $claudeText "CHECK_MAIN") -eq $mainHead -and (Get-Field $claudeText "CHECK"))
}

if ($claudeValid) {
    Write-Host "NEXT_SELECT_CLAUDE_CACHE=HIT"
}
else {
    Write-Host "NEXT_SELECT_CLAUDE_CACHE=MISS"

    $claudeErr = Join-Path $logDir "next-select-main-$mainShort-claude-$ts.stderr.txt"
    $oldEap = $ErrorActionPreference

    try {
        $ErrorActionPreference = "Continue"
        Push-Location $selectWorktree

        try {
            $claudeLines = @(
                $checkPrompt |
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

    if ($claudeExit -ne 0 -or $claudeLines.Count -eq 0) {
        Write-Host "CLAUDE_EXIT=$claudeExit"
        Write-Output "NEXT_HOLD=SELECTOR_CLAUDE_EXEC_FAILED"
        return
    }

    $claudeText = ($claudeLines -join "`n")
    [System.IO.File]::WriteAllText($claudeOut, $claudeText, $utf8)
    Add-ToolStamp -Path $claudeOut -Auditor "CLAUDE" -Policy "auto-next-select"
}

$check = Get-Field $claudeText "CHECK"
$checkReason = Get-Field $claudeText "CHECK_REASON"

if (-not $check) {
    # 판정 줄이 문장 안에 들어간 경우(예: "Verdict: CHECK=AGREE.") — 값이 하나로 일치할 때만 인정
    $inline = @([regex]::Matches($claudeText, 'CHECK=(AGREE|DISAGREE)') | ForEach-Object { $_.Groups[1].Value } | Sort-Object -Unique)
    if ($inline.Count -eq 1) {
        $check = $inline[0]
        Write-Host "CLAUDE_CHECK_PARSE=INLINE"
    }
}

Write-Host "CLAUDE_CHECK=$check $checkReason"

if ((Get-Field $claudeText "CHECK_MAIN") -ne $mainHead -or $check -ne "AGREE") {
    Save-Selection -Decision "HOLD" -Category "NEXT_SELECTION_DISPUTED" -Reason $checkReason -SliceId $sliceId -Title $sliceTitle
    Write-Output "NEXT_DECISION=HOLD"
    Write-Output "NEXT_HOLD_CATEGORY=NEXT_SELECTION_DISPUTED"
    Write-Output "NEXT_HOLD_REASON=$checkReason"
    return
}

# -------------------------------------------------
# 3. Freshness + slice spec
# -------------------------------------------------

$mainNow = "$(gh api "repos/$repoSlug/commits/main" --jq .sha)".Trim()

if ($mainNow -ne $mainHead) {
    Write-Output "NEXT_HOLD=MAIN_MOVED"
    return
}

$specMatch = [regex]::Match($gptText, '(?s)^\s*SPEC:\s*(.*)$', [System.Text.RegularExpressions.RegexOptions]::Multiline)

$sliceSpec = [ordered]@{
    main = $mainHead
    slice_id = $sliceId
    slice_title = $sliceTitle
    canonical_sources = Get-Field $gptText "CANONICAL_SOURCES"
    allowed_paths = @($allowedPaths -split "," | ForEach-Object { $_.Trim() } | Where-Object { $_ })
    schema_change = $schemaChange
    acceptance = Get-Field $gptText "ACCEPTANCE"
    forbidden = Get-Field $gptText "FORBIDDEN"
    spec = if ($specMatch.Success) { $specMatch.Groups[1].Value.Trim() } else { "" }
    gpt_selection = $gptOut
    claude_check = $claudeOut
    claude_reason = $checkReason
    created_at = (Get-Date).ToString("o")
}

# 사용자(owner)가 승인한 범위 보정: 같은 exact main + 같은 slice 에만 적용하고, 그 사실을 spec 에 남긴다.
$amendPath = Join-Path $stateDir "next-scope-amendment-$mainShort.json"

if (Test-Path $amendPath) {
    $amend = Get-Content $amendPath -Raw -Encoding utf8 | ConvertFrom-Json

    if ([string]$amend.main -eq $mainHead -and [string]$amend.slice_id -eq $sliceId) {
        $added = @($amend.add_allowed_paths | ForEach-Object { ([string]$_).Trim() } | Where-Object { $_ -and ($sliceSpec.allowed_paths -notcontains $_) })
        $sliceSpec.allowed_paths = @($sliceSpec.allowed_paths) + $added
        $sliceSpec.forbidden = "$($sliceSpec.forbidden) Also forbidden: $($amend.add_forbidden)"
        $sliceSpec.spec = "$($sliceSpec.spec)`n`nOWNER-APPROVED SCOPE AMENDMENT ($($amend.approved_by)): ALLOWED_PATHS += $(@($amend.add_allowed_paths) -join ', '). Reason: $($amend.reason) Limits: $($amend.limits)"
        foreach ($sup in @($amend.spec_supersede)) {
            if (-not $sup -or -not $sup.old) { continue }
            if ($sliceSpec.spec.Contains([string]$sup.old)) {
                $sliceSpec.spec = $sliceSpec.spec.Replace([string]$sup.old, "[OWNER-APPROVED AMENDMENT - supersedes the selected line per $($sup.basis)] $($sup.new)")
                Write-Host "NEXT_SPEC_SUPERSEDED=$($sup.basis)"
            }
            else {
                Write-Host "NEXT_SPEC_SUPERSEDE_NOT_FOUND=$($sup.basis)"
            }
        }
        $sliceSpec.owner_scope_amendment = $amendPath
        Write-Host "NEXT_SCOPE_AMENDMENT=$amendPath (+$(@($amend.add_allowed_paths) -join ','))"
    }
    else {
        Write-Host "NEXT_SCOPE_AMENDMENT_IGNORED=$amendPath (main/slice mismatch)"
    }
}

$sliceSpec | ConvertTo-Json -Depth 5 | Set-Content -Encoding utf8 $specPath

Save-Selection -Decision "PROCEED" -SliceId $sliceId -Title $sliceTitle

Write-Output "NEXT_DECISION=PROCEED"
Write-Output "NEXT_SLICE_ID=$sliceId"
Write-Output "NEXT_SLICE_TITLE=$sliceTitle"
Write-Output "NEXT_SPEC=$specPath"
