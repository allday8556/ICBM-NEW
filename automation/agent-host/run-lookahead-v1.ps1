param(
    [Parameter(Mandatory=$true)]
    [int]$CurrentPr
)

$ErrorActionPreference = "Stop"

$nativeUtf8 = New-Object System.Text.UTF8Encoding($false)
$script:OutputEncoding = $nativeUtf8
[Console]::OutputEncoding = $nativeUtf8

$hostRoot = "C:\Users\user\ICBM-Agent-Host"

$configPath = Join-Path $hostRoot "state\orchestrator-config.json"
$statePath  = Join-Path $hostRoot "state\orchestrator-state.json"
$planDir    = Join-Path $hostRoot "state\lookahead"

New-Item -ItemType Directory -Force -Path $planDir | Out-Null

$config = Get-Content $configPath -Raw -Encoding utf8 | ConvertFrom-Json
$state  = Get-Content $statePath  -Raw -Encoding utf8 | ConvertFrom-Json

$repoSlug = [string]$config.repository
$repoPath = [string]$config.repo_path

$pr = gh pr view $CurrentPr `
    --repo $repoSlug `
    --json number,state,headRefOid,headRefName |
    ConvertFrom-Json

$mainHead = gh api "repos/$repoSlug/commits/main" --jq .sha

if ($pr.state -ne "OPEN") {
    Write-Host "LOOKAHEAD_HOLD=PR_NOT_OPEN"
    return
}

$localHead = git -C $repoPath rev-parse HEAD
$localBranch = git -C $repoPath branch --show-current
$dirty = git -C $repoPath status --porcelain

if ($localHead -ne $pr.headRefOid) {
    Write-Host "LOOKAHEAD_HOLD=HEAD_MISMATCH"
    return
}

if ($localBranch -ne $pr.headRefName) {
    Write-Host "LOOKAHEAD_HOLD=BRANCH_MISMATCH"
    return
}

if ($dirty) {
    Write-Host "LOOKAHEAD_HOLD=WORKTREE_DIRTY"
    return
}

$slots = @(
    [pscustomobject]@{
        Name = "NEXT-1"
        Property = "next_1"
    },
    [pscustomobject]@{
        Name = "NEXT-2"
        Property = "next_2"
    },
    [pscustomobject]@{
        Name = "NEXT-3"
        Property = "next_3"
    }
)

$needsRefresh = @()

foreach ($slotDef in $slots) {

    $slot = $state.($slotDef.Property)

    if (-not $slot) {
        continue
    }

    $fresh = (
        [string]$slot.base_main -eq $mainHead -and
        [string]$slot.based_on_current_pr_head -eq $pr.headRefOid -and
        [string]$slot.state -ne "STALE_PREP"
    )

    if ($fresh -and $slot.PSObject.Properties["plan_file"]) {
        if (Test-Path ([string]$slot.plan_file)) {
            Write-Host "$($slotDef.Name)=FRESH"
            continue
        }
    }

    $needsRefresh += $slotDef
}

if ($needsRefresh.Count -eq 0) {
    Write-Host "LOOKAHEAD_RESULT=ALREADY_FRESH"
    return
}

# -------------------------------------------------
# Build one planning request for all stale slots.
# Repository is read-only to Codex.
# -------------------------------------------------

$slotPacket = @()

foreach ($slotDef in $needsRefresh) {
    $slotPacket += @"
============================================================
$($slotDef.Name)
============================================================
$($state.($slotDef.Property) | ConvertTo-Json -Depth 12)
"@
}

$planningPrompt = @"
[ICBM-NEW] ROADMAP LOOKAHEAD PREPARATION

ROLE:
You are preparing future work only.
You are NOT implementing anything.

CURRENT PR:
#$CurrentPr

CURRENT PR HEAD:
$($pr.headRefOid)

CANONICAL MAIN:
$mainHead

GOAL:
Prepare the stale NEXT slots below from repository-canonical ROADMAP,
ARCHITECTURE, ADR and acceptance documents.

STRICT RULES:
- READ ONLY.
- Do not edit files.
- Do not commit.
- Do not push.
- Do not create a branch or PR.
- Do not call a provider.
- Do not enable LIVE.
- Do not open a canary.
- Do not start implementation.
- Do not infer a later milestone when the ROADMAP does not authorize it.
- Existing ROADMAP/ADR/acceptance text is source of truth.
- Read only the minimum named/relevant canonical files.
- Do not enumerate the entire repository.
- If canonical sources conflict or the next work is ambiguous, say AMBIGUOUS.
- A preparation is never implementation authorization.

SLOTS TO PREPARE:

$($slotPacket -join "`n")

Return exactly this structure for EACH supplied slot:

=== NEXT-X ===
RESULT=<PREPARED|AMBIGUOUS>
TITLE=<canonical next work>
DEPENDENCIES=<concise>
DESIGN=<concise preparation/design>
FILES_EXPECTED=<likely files or NONE if not safely known>
TEST_PLAN=<concise>
ACCEPTANCE=<concise>
FORBIDDEN=<concise>
STALE_TRIGGERS=<concise>
=== END NEXT-X ===
"@

$mainShort = $mainHead.Substring(0,12)
$headShort = $pr.headRefOid.Substring(0,12)

$planFile = Join-Path `
    $planDir `
    "pr-$CurrentPr-main-$mainShort-head-$headShort.txt"

Write-Host ""
Write-Host "[LOOKAHEAD] Preparing NEXT-1..NEXT-3"
Write-Host "MODEL=gpt-5.6-sol"
Write-Host "REASONING=high"
Write-Host ""

$codexErrFile = Join-Path `
    $planDir `
    "pr-$CurrentPr-main-$mainShort-head-$headShort-codex.stderr.txt"

Remove-Item $codexErrFile -Force -ErrorAction SilentlyContinue

$oldErrorActionPreference = $ErrorActionPreference

try {
    # Codex CLI의 정상 시작 배너가 stderr로 출력되어도
    # PowerShell terminating error로 오인하지 않는다.
    $ErrorActionPreference = "Continue"

    $planningPrompt | codex exec `
        -C $repoPath `
        -s read-only `
        --ephemeral `
        --model gpt-5.6-sol `
        --config 'model_reasoning_effort="high"' `
        -o $planFile `
        - `
        2> $codexErrFile

    $codexExitCode = $LASTEXITCODE
}
finally {
    $ErrorActionPreference = $oldErrorActionPreference
}

if ($codexExitCode -ne 0 -or -not (Test-Path $planFile)) {
    Write-Host "LOOKAHEAD_HOLD=CODEX_PREP_FAILED"
    Write-Host "CODEX_EXIT=$codexExitCode"

    if (Test-Path $codexErrFile) {
        Get-Content $codexErrFile -Tail 20 |
            ForEach-Object {
                Write-Host "CODEX_STDERR=$_"
            }
    }

    return
}

$planText = Get-Content $planFile -Raw -Encoding utf8

if ($planText -match '(?m)^RESULT=AMBIGUOUS\s*$') {
    Write-Host "LOOKAHEAD_HOLD=ROADMAP_AMBIGUOUS"
    Write-Host "PLAN=$planFile"
    return
}

# -------------------------------------------------
# Restore prepared state and pin it to the current HEAD/main.
# The detailed plan remains in the local plan file.
# -------------------------------------------------

$now = (Get-Date).ToString("o")

foreach ($slotDef in $needsRefresh) {

    $slot = $state.($slotDef.Property)

    $preparedState = if (
        $slot.PSObject.Properties["prepared_state"] -and
        $slot.prepared_state
    ) {
        [string]$slot.prepared_state
    }
    else {
        switch ($slotDef.Name) {
            "NEXT-1" { "PREPARED_DESIGN" }
            "NEXT-2" { "PREPARED_SCOPE" }
            "NEXT-3" { "PREPARED_RESEARCH" }
        }
    }

    $slot.state = $preparedState
    $slot.base_main = $mainHead
    $slot.based_on_current_pr_head = $pr.headRefOid

    if ($slot.PSObject.Properties["stale_reason"]) {
        $slot.stale_reason = $null
    }

    if ($slot.PSObject.Properties["plan_file"]) {
        $slot.plan_file = $planFile
    }
    else {
        $slot |
            Add-Member `
                -NotePropertyName plan_file `
                -NotePropertyValue $planFile
    }

    if ($slot.PSObject.Properties["validated_at"]) {
        $slot.validated_at = $now
    }
    else {
        $slot |
            Add-Member `
                -NotePropertyName validated_at `
                -NotePropertyValue $now
    }

    if ($slot.PSObject.Properties["authorization"]) {
        $slot.authorization = "PREP_ONLY"
    }
}

$state.canonical_main = $mainHead
$state.updated_at = $now

$state |
    ConvertTo-Json -Depth 20 |
    Set-Content -Encoding utf8 $statePath

Write-Host ""
Write-Host "LOOKAHEAD_RESULT=PREPARED"
Write-Host "MAIN=$mainHead"
Write-Host "PR_HEAD=$($pr.headRefOid)"
Write-Host "PLAN=$planFile"

foreach ($slotDef in $slots) {
    $slot = $state.($slotDef.Property)

    if ($slot) {
        Write-Host "$($slotDef.Name)=$($slot.state)"
    }
}

Write-Host "IMPLEMENTATION_AUTHORIZED=FALSE"
Write-Host "AUTO_MERGE=FALSE"

