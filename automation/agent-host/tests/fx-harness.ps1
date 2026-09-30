param(
    [Parameter(Mandatory=$true)][string]$Scenario,
    [Parameter(Mandatory=$true)][string]$Root,
    # the directory that holds the host scripts under test; default: this repository's own copy
    [string]$SrcHost = ""
)

# Fixture harness: local bare origin + dirty user clone on main + mocked gh/codex/claude.
# Real GitHub, real ICBM-NEW repo and real Agent Host state are never touched.
# The host configuration is the tracked fixture tests\fx-config.json, never a runtime's own.

if (-not $SrcHost) { $SrcHost = Split-Path $PSScriptRoot -Parent }

$ErrorActionPreference = "Continue"
$srcHost = $SrcHost

$fx = Join-Path $Root $Scenario
if (Test-Path $fx) { Remove-Item $fx -Recurse -Force }
New-Item -ItemType Directory -Force -Path $fx | Out-Null

$origin = Join-Path $fx "origin.git"
$seed = Join-Path $fx "seed"
$user = Join-Path $fx "userrepo"
$hostDir = Join-Path $fx "host"
$promptDir = Join-Path $fx "prompts"
New-Item -ItemType Directory -Force -Path $promptDir, (Join-Path $hostDir "state"), (Join-Path $hostDir "logs") | Out-Null

function global:G { param([string[]]$A) $ErrorActionPreference = "Continue"; $o = & git @A 2>$null; return $o }

# ---------------- origin + seed ----------------
G @("init", "--bare", "--initial-branch=main", $origin) | Out-Null
G @("clone", "--quiet", $origin, $seed) | Out-Null
G @("-C", $seed, "config", "user.name", "fixture") | Out-Null
G @("-C", $seed, "config", "user.email", "fixture@example.invalid") | Out-Null
G @("-C", $seed, "checkout", "-q", "-b", "main") | Out-Null

function W { param([string]$Rel, [string]$Text, [string]$Base = $seed)
    $p = Join-Path $Base $Rel
    New-Item -ItemType Directory -Force -Path (Split-Path $p -Parent) | Out-Null
    [System.IO.File]::WriteAllText($p, $Text, (New-Object System.Text.UTF8Encoding($false)))
}

W "README.md" "fixture repo`n"
W "docs/contract.md" "# Contract`nrule: never resend CREATE`n"
W "docs/stale.md" "# Stale`nok`n"
W "app/x.py" "def x():`n    return 1`n"
W "app/__init__.py" "MILESTONE = ""M5""`n"
W "app/unrelated.py" "def u():`n    return 0`n"
W "tests/test_x.py" "def test_x():`n    assert True`n"
W "docs/old-name.md" (("line of old doc content`n") * 20)
$bigBase = (1..50 | ForEach-Object { "base line $_" }) -join "`n"
W "docs/big.md" ($bigBase + "`n")

if ($Scenario -like "*remediation*") {
    W "docs/stale.md" "# Stale`nSTALE_DOC_MARKER: endpoint adoption waits for new evidence`n"
    if ($Scenario -eq "remediation-claude-only") { W "docs/stale.md" "# Stale`nCLAUDE_ONLY_STALE ui ruling`n" }
}

G @("-C", $seed, "add", "-A") | Out-Null
G @("-C", $seed, "commit", "-q", "-m", "base") | Out-Null
G @("-C", $seed, "push", "-q", "origin", "main") | Out-Null

function New-PrBranch {
    param([string]$Branch, [scriptblock]$Change)
    G @("-C", $seed, "checkout", "-q", "-B", $Branch, "origin/main") | Out-Null
    G @("-C", $seed, "fetch", "-q", "origin") | Out-Null
    G @("-C", $seed, "reset", "-q", "--hard", "origin/main") | Out-Null
    & $Change
    G @("-C", $seed, "add", "-A") | Out-Null
    G @("-C", $seed, "commit", "-q", "-m", "pr $Branch") | Out-Null
    G @("-C", $seed, "push", "-q", "-f", "origin", "HEAD:refs/heads/$Branch") | Out-Null
    G @("-C", $seed, "checkout", "-q", "main") | Out-Null
}

$global:FxSeed = $seed
$global:FxOrigin = $origin
$global:FxPrs = @{}
$global:FxCiFail = @{}
$global:FxComments = @{}
$global:FxStreams = @{}        # "issue:<n>" | "reviews:<pr>" | "rc:<pr>" -> comment ids (objects in FxComments)
$global:FxGptPrCount = 0
$global:FxIssueBodies = @{}    # issue/PR number -> body
$global:FxStreamFail = @{}     # stream key -> "page" | "perm" | "truncate"
$global:FxOpenPrListFail = $false
function global:Get-FxHead { param($n)
    $p = $global:FxPrs["$n"]
    if ($p.state -eq "MERGED") { return $p.mergedHead }
    return "$(& git --git-dir=$global:FxOrigin rev-parse "refs/heads/$($p.headRefName)" 2>$null)".Trim()
}
function global:Get-FxMain { return "$(& git --git-dir=$global:FxOrigin rev-parse refs/heads/main 2>$null)".Trim() }
$global:FxGuardHook = $null
$global:FxBeforeMerge = $null
$global:FxNextPr = 1
$global:FxCiPending = @{}      # sha -> remaining pending polls
$global:FxCiDefaultPending = 0
$global:FxCalls = New-Object System.Collections.Generic.List[string]

function global:Add-FxPr { param([string]$Branch, [string]$Title = "fixture PR", [string]$CreatedAt = ((Get-Date).ToUniversalTime().AddMinutes(5).ToString("yyyy-MM-ddTHH:mm:ssZ")), [string]$Body = "", [string[]]$Labels = @(), [string]$Base = "main")
    $n = $global:FxNextPr; $global:FxNextPr++
    $global:FxPrs["$n"] = @{ number = $n; title = $Title; headRefName = $Branch; state = "OPEN"; mergedAt = $null; mergedHead = $null; createdAt = $CreatedAt; isDraft = $false; mergeable = "MERGEABLE"; mergeCommit = $null; body = $Body; labels = @($Labels); base = $Base }
    return $n
}

function global:Merge-FxPr { param([int]$N)
    $seed = $global:FxSeed; $origin = $global:FxOrigin
    $p = $global:FxPrs["$N"]
    $head = (G @("--git-dir=$origin", "rev-parse", "refs/heads/$($p.headRefName)"))
    G @("-C", $seed, "fetch", "-q", "origin") | Out-Null
    G @("-C", $seed, "checkout", "-q", "main") | Out-Null
    G @("-C", $seed, "reset", "-q", "--hard", "origin/main") | Out-Null
    G @("-C", $seed, "merge", "-q", "--no-ff", "-m", "merge #$N", "origin/$($p.headRefName)") | Out-Null
    G @("-C", $seed, "push", "-q", "origin", "main") | Out-Null
    $p.state = "MERGED"; $p.mergedAt = "2026-09-27T00:00:00Z"; $p.mergedHead = "$head".Trim()
    $p.mergeCommit = "$(& git --git-dir=$origin rev-parse refs/heads/main 2>$null)".Trim()
    return $p.mergeCommit
}

switch ($Scenario) {
    "gpt-loop" {
        New-PrBranch "feat/gpt" { W "docs/contract.md" "# Contract`nrule: never resend CREATE`nBUG_MARKER_GPT`n"; W "app/x.py" "def x():`n    return 2`n" }
        [void](Add-FxPr "feat/gpt")
        $global:FxCiDefaultPending = 2
    }
    "claude-loop" {
        New-PrBranch "feat/claude" { W "docs/contract.md" "# Contract`nrule: never resend CREATE`nBUG_MARKER_CLAUDE`n" }
        [void](Add-FxPr "feat/claude")
    }
    { $_ -in @("big-pr", "packet-big") } {
        New-PrBranch "feat/big" {
            $big = (1..1600 | ForEach-Object { "added big line $_ : lorem ipsum dolor sit amet" }) -join "`n"
            W "docs/big.md" ($bigBase + "`n" + $big + "`n")
            for ($i = 1; $i -le 10; $i++) {
                $t = (1..60 | ForEach-Object { "file$i unique content row $_ ZZFILE${i}ZZ" }) -join "`n"
                W "app/mod$i.py" ($t + "`n")
            }
            W "tests/test_x.py" "def test_x():`n    assert 1 == 1  # ZZTESTZZ`n"
            G @("-C", $seed, "mv", "docs/old-name.md", "docs/new-name.md") | Out-Null
        }
        [void](Add-FxPr "feat/big")
    }
    { $_ -like "auto-next*" -or $_ -like "config-*" -or $_ -in @("ci-flaky", "ci-hard-fail", "automerge-success", "guard-head-moved", "guard-main-moved", "guard-ci-pending", "guard-ci-failed", "gpt-insufficient", "gpt-human", "gpt-human-uncategorised", "merge-sha-mismatch", "draft-pr", "unmergeable", "behind-base", "owner-hold", "post-merge-tree-mismatch") } {
        New-PrBranch "feat/clean" { W "docs/contract.md" "# Contract`nrule: never resend CREATE`nclarified`n" }
        $n = Add-FxPr "feat/clean"
        $pushBranch = {
            param($Branch, $Rel, $Text)
            G @("-C", $global:FxSeed, "fetch", "-q", "origin") | Out-Null
            G @("-C", $global:FxSeed, "checkout", "-q", "-B", "tmp-hook", "origin/$Branch") | Out-Null
            Add-Content (Join-Path $global:FxSeed $Rel) $Text
            G @("-C", $global:FxSeed, "commit", "-q", "-am", "hook commit") | Out-Null
            G @("-C", $global:FxSeed, "push", "-q", "origin", "HEAD:refs/heads/$Branch") | Out-Null
            G @("-C", $global:FxSeed, "checkout", "-q", "main") | Out-Null
        }
        $global:FxPushBranch = $pushBranch
        switch ($Scenario) {
            "guard-head-moved" { $global:FxGuardHook = { param($n) & $global:FxPushBranch "feat/clean" "docs/contract.md" "late commit after audit" } }
            "guard-main-moved" { $global:FxGuardHook = { param($n) & $global:FxPushBranch "main" "README.md" "unrelated main commit" } }
            "guard-ci-pending" { $global:FxGuardHook = { param($n) $global:FxCiPending[(Get-FxHead $n)] = 1 } }
            "guard-ci-failed"  { $global:FxGuardHook = { param($n) $global:FxCiFail[(Get-FxHead $n)] = $true } }
            "merge-sha-mismatch" { $global:FxBeforeMerge = { param($n) & $global:FxPushBranch "feat/clean" "docs/contract.md" "race commit during merge" } }
            "draft-pr" { $global:FxPrs["$n"].isDraft = $true }
            # main moves before the first pass: the PR HEAD is behind the base and is brought up to date first
            "behind-base" { & $global:FxPushBranch "main" "README.md" "main moved before the audit" }
            "unmergeable" { $global:FxPrs["$n"].mergeable = "CONFLICTING" }
            { $_ -in @("ci-flaky", "ci-hard-fail") } { $global:FxCiFail["$(G @("--git-dir=$origin", "rev-parse", "refs/heads/feat/clean"))".Trim()] = $true }
        }
    }
    { $_ -like "two-merges*" } {
        New-PrBranch "feat/one" { W "docs/contract.md" "# Contract`nrule: never resend CREATE`none`n" }
        [void](Add-FxPr "feat/one")
        if ($Scenario -eq "two-merges-milestone") {
            New-PrBranch "feat/two" { W "app/__init__.py" "MILESTONE = ""M6""`n" }
        }
        elseif ($Scenario -eq "two-merges-adr") {
            New-PrBranch "feat/two" { W "docs/adr/0099-fixture-decision.md" "# ADR-0099`nStatus: ACCEPTED`n" }
        }
        else {
            New-PrBranch "feat/two" { W "app/x.py" "def x():`n    return 22`n" }
        }
        [void](Add-FxPr "feat/two")
    }
    { $_ -like "packet-*" -and $_ -ne "packet-big" } {
        New-PrBranch "feat/packet" {
            W "docs/contract.md" "# Contract`nrule: never resend CREATE`npacket clarified`n"
            if ($Scenario -eq "packet-many-files") {
                # a large PR whose changed-file manifest alone is longer than the 42K audit call limit
                for ($i = 1; $i -le 700; $i++) {
                    W ("docs/many/a-deliberately-long-directory-name-for-the-changed-file-manifest/file-{0:D4}-with-a-long-descriptive-name.md" -f $i) "row $i`n"
                }
            }
        }
        $n = Add-FxPr "feat/packet"
        if ($Scenario -eq "packet-guard-digest") { $global:FxPrs["$n"].isDraft = $true }
    }
    { $_ -in @("claude-blocker-hold", "fixer-human") } {
        New-PrBranch "feat/claudehold" { W "docs/contract.md" "# Contract`nBUG_MARKER_CLAUDE`n" }
        [void](Add-FxPr "feat/claudehold")
    }
    "scope-expansion" {
        New-PrBranch "feat/scope" { W "docs/contract.md" "# Contract`nBUG_MARKER_GPT`n" }
        [void](Add-FxPr "feat/scope")
    }
    "max-cycles" {
        New-PrBranch "feat/max" { W "docs/contract.md" "# Contract`nalways-blocked`n" }
        [void](Add-FxPr "feat/max")
    }
    { $_ -like "*remediation*" } {
        New-PrBranch "feat/merged" { W "app/x.py" "def x():`n    return 3`n" }
        $n = Add-FxPr "feat/merged"
        Merge-FxPr $n
        if ($Scenario -eq "post-merge-remediation") {
            # stale unrelated PR opened long before this main (like #54) must NOT block remediation
            New-PrBranch "feat/old-ui" { W "README.md" "old ui change`n" }
            [void](Add-FxPr "feat/old-ui" "old ui PR" "2020-01-01T00:00:00Z")
        }
        if ($Scenario -eq "remediation-open-pr-guard") {
            New-PrBranch "fix/human-remediation" { W "docs/stale.md" "# Stale`nhuman fix`n" }
            [void](Add-FxPr "fix/human-remediation")
        }
    }
    { $_ -in @("i2-dup", "i2-dup-legacy") } {
        # real case: #142 open for smartstore-create-adoption on an OLDER main; a new selection on a newer main created #146
        $oldMain = Get-FxMain
        $oldBranch = "feat/agent-host-fixture-feature-$($oldMain.Substring(0,12))"
        New-PrBranch $oldBranch { W "docs/feature.md" "# Feature`nolder draft`n" }
        [void](Add-FxPr $oldBranch "feat: fixture-feature (auto-next, older main)" "2020-01-01T00:00:00Z" "Auto-next slice for exact main older.`n`n**Slice:** fixture-feature — Fixture feature doc and test`n")
        New-PrBranch "feat/other" { W "README.md" "other change moves main`n" }
        $n2 = Add-FxPr "feat/other"
        [void](Merge-FxPr $n2)
        $global:FxOldMain = $oldMain
        $global:FxOldBranch = $oldBranch
    }
    "i2-remediation" {
        # an open remediation PR from an OLDER main (created before the current main) must block a second remediation PR
        [void](Add-FxPr "fix/agent-host-remediation-main-0123456789ab" "fix: post-merge full-audit remediation (main 0123456789ab)" "2020-01-01T00:00:00Z" "Post-merge full-audit remediation for exact main ``0123456789ab``.`n")
    }
}

# ---------------- user repo: main + dirty ----------------
G @("clone", "--quiet", $origin, $user) | Out-Null
G @("-C", $user, "config", "user.name", "fixture-user") | Out-Null
G @("-C", $user, "config", "user.email", "user@example.invalid") | Out-Null
Add-Content -Path (Join-Path $user "README.md") -Value "user local uncommitted edit"
W "scratch-untracked.txt" "user untracked file`n" $user

function Get-UserSnapshot {
    $files = Get-ChildItem $user -Recurse -File -Force |
        Where-Object { $_.FullName -notmatch '\\\.git(\\|$)' } |
        Sort-Object FullName |
        ForEach-Object { "$($_.FullName.Substring($user.Length))=$((Get-FileHash $_.FullName -Algorithm SHA256).Hash)" }
    [pscustomobject]@{
        Branch = "$(G @('-C', $user, 'branch', '--show-current'))"
        Head = "$(G @('-C', $user, 'rev-parse', 'HEAD'))"
        Status = (G @("-C", $user, "status", "--porcelain")) -join "|"
        LocalBranches = (G @("-C", $user, "for-each-ref", "refs/heads", "refs/remotes", "--format=%(refname) %(objectname)")) -join "|"
        Files = $files -join "|"
    }
}

$before = Get-UserSnapshot

# ---------------- host copy ----------------
foreach ($f in "orchestrator-v1.3.ps1", "orchestrator-v1.2.ps1", "run-audit-v1.1.ps1", "run-repair-v1.1.ps1", "run-full-audit-v1.ps1", "resume-orchestrator-v1.3.ps1", "run-lookahead-main-v1.ps1", "agent-host-authority-v2.ps1") {
    if (Test-Path (Join-Path $srcHost $f)) { Copy-Item (Join-Path $srcHost $f) (Join-Path $hostDir $f) }
}

$cfg = Get-Content (Join-Path $PSScriptRoot "fx-config.json") -Raw -Encoding utf8 | ConvertFrom-Json
$cfg.repository = "fixture/icbm"
$cfg.repo_path = $user
if ($Scenario -eq "config-no-automerge") { $cfg.auto_merge = $false }
if ($Scenario -eq "config-no-autonext") { $cfg.auto_next.enabled = $false }
if ($Scenario -eq "auto-next-track") { $cfg | Add-Member -NotePropertyName track -NotePropertyValue ([pscustomobject]@{ name = "fixture-track"; scope = "fixture feature work only" }) }
$cfg | ConvertTo-Json -Depth 10 | Set-Content -Encoding utf8 (Join-Path $hostDir "state\orchestrator-config.json")

# ---------------- packet sources (packet-* scenarios; AGENT_HOST_AUDIT_PROTOCOL sections 3-4, ADR-0022) ----------------
# The slice declaration is the body of PR #1. It names "Issue #89" and cites three source ids, so those three sources
# are the packet's required sources. Nothing is classified by anyone: the legacy record 100009300 (a pre-ADR-0022 human
# classification record) and every other marked source are history, never a hold.
function global:Fx-D { param([string]$Body) $t = $Body.Replace("`r`n", "`n").Replace("`r", "`n"); $s = [System.Security.Cryptography.SHA256]::Create(); return (([BitConverter]::ToString($s.ComputeHash((New-Object System.Text.UTF8Encoding($false)).GetBytes($t)))) -replace '-', '').ToLower() }
function global:Fx-C { param($Id, $Body, $Issue = 1, $UpdatedAt = "2026-09-28T00:00:00Z") $global:FxComments["$Id"] = @{ id = [int64]$Id; user = @{ login = "fixture" }; html_url = "https://github.com/fixture/icbm/issues/$Issue#issuecomment-$Id"; issue_url = "https://api.github.com/repos/fixture/icbm/issues/$Issue"; body = $Body; created_at = "2026-09-28T00:00:00Z"; updated_at = $UpdatedAt; submitted_at = $UpdatedAt } }
function global:Fx-Record {
    param([string]$Preamble = "Fixture classification record (designated streams: #1, #89)", [string]$Extra = "", [string]$ClassOf9004 = "evidence-only", [string]$ScopeLine = "scope: PR #1", [string]$GitLine = "")
    $blob = "$(& git --git-dir=$global:FxOrigin rev-parse "$(Get-FxHead 1):docs/contract.md" 2>$null)".Trim()
    $req = @(
        "- issue-comment 100009001 sha256 $(Fx-D $global:FxComments['100009001'].body) — architect instruction"
        "- issue-comment 100009002 sha256 $(Fx-D $global:FxComments['100009002'].body) — official evidence E1 (unmarked, on #89)"
        "- pr-review 1/100009101 sha256 $(Fx-D $global:FxComments['100009101'].body)"
        $(if ($GitLine) { $GitLine } else { "- git-blob HEAD:docs/contract.md $blob" })
    )
    $ev = @("- issue-comment 100009004 sha256 $(Fx-D $global:FxComments['100009004'].body) — background")
    $scope = if ($ScopeLine) { "$ScopeLine`n" } else { "" }
    $body = "[OWNER-AMENDMENT]`n`n$Preamble`n$scope`nrequired:`n$($req -join "`n")`n"
    if ($ClassOf9004 -eq "required") { $body += "$($ev -join "`n")`n" }
    $body += "`nevidence-only:`n"
    if ($ClassOf9004 -ne "required") { $body += "$($ev -join "`n")`n" }
    $body += "$Extra`nexcluded: none. Any other marked source → HOLD.`n"
    return $body
}

$global:FxPrBody = "fixture PR body (unmarked)`n`nAuthority (Issue #89): architect instruction ``100009001``, official evidence ``100009002``, review ``100009101``.`nNot a citation (no code span): CI run 36699770595.`n"

if ($Scenario -like "packet-*") {
    Fx-C 100009001 "  [ARCHITECT-INSTRUCTION]  `r`n`r`nArchitect: keep the contract rule; clarify wording only.`r`n"
    Fx-C 100009002 "Evidence E1 (unmarked; cited by the PR body): the contract rule is official.`n" 89
    Fx-C 100009003 "status: the host reads ``[ARCHITECT-INSTRUCTION]`` sources (inline code, prose mention; not authority)`n"
    Fx-C 100009004 "[EVIDENCE-PACKET]`nOptional background evidence (marked, not cited).`n" 89
    Fx-C 100009050 "old ordinary comment, created before any scan`n"
    Fx-C 100009061 "> [ARCHITECT-INSTRUCTION]`nquoted, not a marker`n"
    Fx-C 100009062 "``````text`n[ARCHITECT-INSTRUCTION]`n```````n"
    Fx-C 100009063 "[ARCHITECT-INSTRUCTION] see below`n"
    Fx-C 100009064 "[architect-instruction]`nother case`n"
    Fx-C 100009065 "note first`n[EVIDENCE-PACKET]`ntoken on a later line`n"
    Fx-C 100009101 "[EVIDENCE-PACKET]`nReview evidence: the diff matches E1.`n" 1
    Fx-C 100009200 "ordinary discussion`n"
    $global:FxStreams["issue:1"] = @(100009001, 100009003, 100009050, 100009061, 100009062, 100009063, 100009064, 100009065, 100009200, 100009300)
    $global:FxStreams["issue:89"] = @(100009002, 100009004)
    $global:FxStreams["reviews:1"] = @(100009101)
    $global:FxStreams["rc:1"] = @()
    $global:FxIssueBodies["1"] = $global:FxPrBody
    # a legacy human classification record: history only, never read as a mapping and never a hold
    Fx-C 100009300 (Fx-Record)
    if ($Scenario -eq "packet-no-record") { $global:FxStreams["issue:1"] = @($global:FxStreams["issue:1"] | Where-Object { $_ -ne 100009300 }) }
    if ($Scenario -eq "packet-unclassified") { Fx-C 100009005 "[OWNER-AMENDMENT]`nnew amendment nobody classified`n"; $global:FxStreams["issue:1"] = @($global:FxStreams["issue:1"]) + 100009005 }
    if ($Scenario -eq "packet-stream-page-fail") { $global:FxStreamFail["issue:89"] = "page" }
    if ($Scenario -eq "packet-stream-perm") { $global:FxStreamFail["reviews:1"] = "perm" }
    if ($Scenario -eq "packet-stream-truncated") { $global:FxStreamFail["issue:1"] = "truncate" }

    # an optional host manifest (designation only); by default there is none and issue #89 comes from the PR body
    $global:FxManifest = [ordered]@{
        pr = 1
        note = "designation only"
        designated_streams = @(
            [ordered]@{ type = "issue_comments"; issue = 89; watermark = 0 }
        )
    }

    if ($Scenario -in @("packet-edit-to-marker", "packet-clsedit", "packet-record-edit-guard", "packet-cite-added", "packet-unclassified", "packet-no-record")) { $global:FxPrs["1"].isDraft = $true }
    if ($Scenario -eq "packet-edit-to-marker") {
        # DUAL PASS 후 ready 시점: 스캔 이전부터 있던 unmarked 100009050 을 in-place 로 marker-first 로 편집.
        # It is not cited, so it is provenance: no hold, the same digest, the merge goes on.
        $global:FxReadyHook = { $global:FxComments["100009050"].body = "[ARCHITECT-INSTRUCTION]`nold comment edited in place to add a marker`n"; $global:FxComments["100009050"].updated_at = "2026-09-29T00:00:00Z"; $global:FxCalls.Add("OLD_UNMARKED_EDITED_TO_MARKER 100009050") }
    }
    if ($Scenario -eq "packet-clsedit") {
        # a CITED source edited after the audit → its digest and the packet digest change → DUAL PASS invalid → re-audit (§7.1)
        $global:FxReadyHook = { $global:FxComments["100009002"].body = "Evidence E1 EDITED after the audit.`n"; $global:FxCalls.Add("CITED_SOURCE_EDITED 100009002") }
    }
    if ($Scenario -eq "packet-record-edit-guard") {
        # the legacy record edited after the audit: it is not a packet input, so nothing changes
        $global:FxReadyHook = { $global:FxComments["100009300"].body = (Fx-Record -Preamble "PR #1 fixture classification record, revised"); $global:FxCalls.Add("LEGACY_RECORD_EDITED 100009300") }
    }
    if ($Scenario -eq "packet-cite-added") {
        # the declaration itself changes after the audit (a new citation) → new packet digest → re-audit
        $global:FxReadyHook = { $global:FxIssueBodies["1"] = $global:FxPrBody + "Also: background ``100009004``.`n"; $global:FxCalls.Add("PR_BODY_CITES_ONE_MORE 100009004") }
    }
}

# ---------------- mocks ----------------
$global:FxOrigin = $origin
$global:FxPromptDir = $promptDir
$global:FxPromptSeq = 0

function global:Get-FxHead { param($n)
    $p = $global:FxPrs["$n"]
    if ($p.state -eq "MERGED") { return $p.mergedHead }
    return "$(& git --git-dir=$global:FxOrigin rev-parse "refs/heads/$($p.headRefName)" 2>$null)".Trim()
}
function global:Get-FxMain { return "$(& git --git-dir=$global:FxOrigin rev-parse refs/heads/main 2>$null)".Trim() }

function global:gh {
    $a = @($args | ForEach-Object { if ($_ -is [array]) { $_ -join "," } else { "$_" } })
    $global:FxCalls.Add("gh " + ($a -join " "))
    $global:LASTEXITCODE = 0
    $opt = @{}; $pos = New-Object System.Collections.Generic.List[string]; $fields = @{}
    for ($i = 0; $i -lt $a.Count; $i++) {
        if ($a[$i] -in @("-f", "-F")) { $kv = $a[$i + 1] -split "=", 2; $fields[$kv[0]] = $kv[1]; $i++ }
        elseif ($a[$i] -in @("-H", "-X", "--jq", "--repo", "--json", "--base", "--head", "--title", "--body-file", "--state")) { $opt[$a[$i]] = $a[$i + 1]; $i++ }
        elseif ($a[$i] -like "--*" -or $a[$i] -like "-*") { $opt[$a[$i]] = $true }
        else { $pos.Add($a[$i]) }
    }
    if ($pos[0] -eq "run" -and $pos[1] -eq "rerun") {
        $global:FxCalls.Add("CI_RERUN $($pos[2])")
        if ($global:FxScenario -ne "ci-hard-fail") { foreach ($k in @($global:FxCiFail.Keys)) { $global:FxCiFail[$k] = $false } }
        return
    }
    if ($pos[0] -eq "pr") {
        switch ($pos[1]) {
            "view" {
                $n = $pos[2]; $p = $global:FxPrs["$n"]
                if (-not $p) { $global:LASTEXITCODE = 1; return }
                if ("$($opt['--json'])" -like "*mergeable*" -and $global:FxGuardHook -and $p.state -eq "OPEN") {
                    # MERGE_GUARD 조회 시점 (mergeable 을 묻는 유일한 조회) 에 한 번만 외부 변화 주입
                    $hook = $global:FxGuardHook; $global:FxGuardHook = $null
                    & $hook $n
                    $global:FxCalls.Add("GUARD_HOOK_FIRED")
                }
                $mc = if ($p.mergeCommit) { @{ oid = $p.mergeCommit } } else { $null }
                $o = [ordered]@{ number = [int]$n; title = $p.title; state = $p.state; mergedAt = $p.mergedAt; headRefOid = (Get-FxHead $n); headRefName = $p.headRefName; baseRefName = "main"; url = "https://github.com/fixture/icbm/pull/$n"; isDraft = $p.isDraft; mergeable = $p.mergeable; mergeCommit = $mc }
                if ($opt["--jq"] -eq ".headRefOid") { return $o.headRefOid }
                return ($o | ConvertTo-Json -Compress)
            }
            "list" {
                $open = @($global:FxPrs.Values | Where-Object { $_.state -eq "OPEN" } | ForEach-Object { [ordered]@{ number = $_.number; headRefName = $_.headRefName; createdAt = $_.createdAt } })
                if ($open.Count -eq 0) { return "[]" }
                return (ConvertTo-Json -InputObject $open -Compress)
            }
            "create" {
                if ($global:FxScenario -eq "remediation-fallback") { Remove-Item (Join-Path $global:FxHostDir "state\full-audit-baseline.json") -Force -ErrorAction SilentlyContinue; $global:FxCalls.Add("BASELINE_FILE_REMOVED") }
                $n = $global:FxNextPr; $global:FxNextPr++
                $bodyText = if ($opt["--body-file"] -and (Test-Path $opt["--body-file"])) { [System.IO.File]::ReadAllText($opt["--body-file"]) } else { "" }
                $global:FxPrs["$n"] = @{ number = $n; title = $opt["--title"]; headRefName = $opt["--head"]; state = "OPEN"; mergedAt = $null; mergedHead = $null; base = $opt["--base"]; createdAt = (Get-Date).ToUniversalTime().ToString("yyyy-MM-ddTHH:mm:ssZ"); isDraft = [bool]$opt["--draft"]; mergeable = "MERGEABLE"; mergeCommit = $null; body = $bodyText; labels = @() }
                $global:FxCalls.Add("PR_CREATE pr=$n draft=$([bool]$opt['--draft'])")
                return "https://github.com/fixture/icbm/pull/$n"
            }
            "ready" {
                $n = $pos[2]; $p = $global:FxPrs["$n"]
                if (-not $p) { $global:LASTEXITCODE = 1; return }
                $p.isDraft = [bool]$opt["--undo"]
                $global:FxCalls.Add("PR_READY pr=$n draft=$($p.isDraft) head=$(Get-FxHead $n)")
                if (-not $p.isDraft -and $global:FxReadyHook) { $hook = $global:FxReadyHook; $global:FxReadyHook = $null; & $hook }
                $global:LASTEXITCODE = 0
                return
            }
            { $_ -in @("comment", "edit", "review") } { if ($global:FxScenario -eq "authority-unit") { $global:FxCalls.Add("WRITE_SENT gh pr $($pos[1])"); return } $global:FxCalls.Add("FORBIDDEN gh pr $($pos[1])"); $global:LASTEXITCODE = 1; return }
            default { $global:FxCalls.Add("FORBIDDEN gh pr $($pos[1])"); $global:LASTEXITCODE = 1; return }
        }
    }
    if ($pos[0] -eq "issue" -and $global:FxScenario -eq "authority-unit") { $global:FxCalls.Add("WRITE_SENT gh issue $($pos[1])"); return }
    if ($pos[0] -eq "api") {
        $path = ($pos[1] -split "\?")[0]
        if ($opt["-X"] -eq "PUT" -and $path -match '/pulls/(\d+)/merge$') {
            $n = $Matches[1]
            if ($global:FxBeforeMerge) { $hook = $global:FxBeforeMerge; $global:FxBeforeMerge = $null; & $hook $n }
            $cur = Get-FxHead $n
            $global:FxCalls.Add("MERGE_CALL pr=$n sha=$($fields['sha']) method=$($fields['merge_method']) remote_head=$cur")
            if ($fields["sha"] -ne $cur) {
                Write-Error "gh: Head branch was modified. Review and try the merge again. (HTTP 409)"
                $global:LASTEXITCODE = 1
                return
            }
            $mergeSha = Merge-FxPr ([int]$n)
            return (@{ sha = $mergeSha; merged = $true; message = "Pull Request successfully merged" } | ConvertTo-Json -Compress)
        }
        if ($opt["-X"] -eq "PUT" -and $path -match '/pulls/(\d+)/update-branch$') {
            # GitHub "update branch": merge the current base into the PR head (no force)
            $n = $Matches[1]; $p = $global:FxPrs["$n"]; $cur = Get-FxHead $n
            $global:FxCalls.Add("UPDATE_BRANCH pr=$n expected=$($fields['expected_head_sha']) remote_head=$cur")
            if ($fields["expected_head_sha"] -ne $cur -or $global:FxScenario -eq "behind-base-conflict") { Write-Error "gh: merge conflict (HTTP 422)"; $global:LASTEXITCODE = 1; return }
            G @("-C", $global:FxSeed, "fetch", "-q", "origin") | Out-Null
            G @("-C", $global:FxSeed, "checkout", "-q", "-B", "tmp-sync", "origin/$($p.headRefName)") | Out-Null
            G @("-C", $global:FxSeed, "merge", "-q", "--no-ff", "-m", "merge main into $($p.headRefName)", "origin/main") | Out-Null
            G @("-C", $global:FxSeed, "push", "-q", "origin", "HEAD:refs/heads/$($p.headRefName)") | Out-Null
            G @("-C", $global:FxSeed, "checkout", "-q", "main") | Out-Null
            return (@{ message = "Updating pull request branch." } | ConvertTo-Json -Compress)
        }
        if ($path -match '/compare/([0-9a-f]{40})\.\.\.([0-9a-f]{40})$') {
            $behind = "$(& git --git-dir=$global:FxOrigin rev-list --count "$($Matches[2])..$($Matches[1])" 2>$null)".Trim()
            if ($behind -notmatch '^\d+$') { $global:LASTEXITCODE = 1; return }
            $global:LASTEXITCODE = 0
            if ($opt["--jq"] -eq ".behind_by") { return $behind }
            return (@{ behind_by = [int]$behind } | ConvertTo-Json -Compress)
        }
        if ($path -match '/git/commits/([0-9a-f]{40})$') {
            $commitSha = $Matches[1]
            $tree = "$(& git --git-dir=$global:FxOrigin rev-parse "$commitSha^{tree}" 2>$null)".Trim()
            if ($tree.Length -ne 40) { $global:LASTEXITCODE = 1; return }
            # the merged main reports another tree than the audited HEAD: POST_MERGE_VERIFY must not pass
            if ($global:FxScenario -eq "post-merge-tree-mismatch" -and $commitSha -eq (Get-FxMain)) { $tree = "0" * 40 }
            $global:LASTEXITCODE = 0
            if ($opt["--jq"] -eq ".tree.sha") { return $tree }
            return (@{ sha = $commitSha; tree = @{ sha = $tree } } | ConvertTo-Json -Compress)
        }
        if ($opt["-X"] -in @("POST", "PATCH") -or ($fields.Count -gt 0 -and $opt["-X"] -ne "PUT") -or $opt["--input"]) {
            if ($global:FxScenario -eq "authority-unit") { $global:FxCalls.Add("WRITE_SENT gh api $($opt['-X']) $path"); return "{}" }
            $global:FxCalls.Add("FORBIDDEN gh api write $path"); $global:LASTEXITCODE = 1; return
        }
        if ($path -match '/issues/comments/(\d+)$') {
            $c = $global:FxComments[$Matches[1]]
            if (-not $c) { $global:LASTEXITCODE = 1; return }
            return ($c | ConvertTo-Json -Depth 5 -Compress)
        }
        # V2 packet: source discovery streams + review / review-comment fetch
        $pageNo = 1; if ("$($pos[1])" -match '[?&]page=(\d+)') { $pageNo = [int]$Matches[1] }
        $streamKey = $null
        if ($path -match '/issues/(\d+)/comments$') { $streamKey = "issue:$($Matches[1])" }
        elseif ($path -match '/pulls/(\d+)/reviews$') { $streamKey = "reviews:$($Matches[1])" }
        elseif ($path -match '/pulls/(\d+)/comments$') { $streamKey = "rc:$($Matches[1])" }
        if ($streamKey) {
            $global:FxCalls.Add("STREAM_LIST $streamKey page=$pageNo")
            $fail = $global:FxStreamFail[$streamKey]
            if ($fail -eq "page") { Write-Error "gh: HTTP 502 Bad Gateway (page $pageNo)"; $global:LASTEXITCODE = 1; return }
            if ($fail -eq "perm") { Write-Error "gh: Resource not accessible by integration (HTTP 403)"; $global:LASTEXITCODE = 1; return }
            $items = @(); if ($pageNo -eq 1 -and $global:FxStreams.ContainsKey($streamKey)) { $items = @($global:FxStreams[$streamKey] | ForEach-Object { $global:FxComments["$_"] } | Where-Object { $_ }) }
            if ($items.Count -eq 0) { return "[]" }
            return (ConvertTo-Json -InputObject $items -Depth 5 -Compress)
        }
        if ($path -match '/pulls/(\d+)/reviews/(\d+)$' -or $path -match '/pulls/comments/(\d+)$') {
            $cid = if ($path -match '/reviews/(\d+)$') { $Matches[1] } else { ([regex]::Match($path, '/pulls/comments/(\d+)$')).Groups[1].Value }
            # review comments live in their own id space: only ids listed in an rc:* stream resolve here
            if ($path -match '/pulls/comments/' -and -not @($global:FxStreams.Keys | Where-Object { $_ -like "rc:*" } | Where-Object { @($global:FxStreams[$_]) -contains [int64]$cid }).Count) { $global:LASTEXITCODE = 1; return }
            $c = $global:FxComments[$cid]
            if (-not $c) { $global:LASTEXITCODE = 1; return }
            return ($c | ConvertTo-Json -Depth 5 -Compress)
        }
        if ($path -match '/actions/runs$') {
            $anyFail = @($global:FxCiFail.Values | Where-Object { $_ }).Count -gt 0
            $wr = @(@{ id = 100009001; check_suite_id = 100009101; created_at = "2020-01-01T00:00:00Z"; status = "completed"; conclusion = $(if ($anyFail) { "failure" } else { "success" }) })
            return (@{ workflow_runs = $wr } | ConvertTo-Json -Depth 5 -Compress)
        }
        if ($path -match '/commits/main$') { return (Get-FxMain) }
        if ($path -match '/commits/([0-9a-f]{40})/check-runs$') {
            $sha = $Matches[1]
            if (-not $global:FxCiPending.ContainsKey($sha)) { $global:FxCiPending[$sha] = $global:FxCiDefaultPending }
            $status = "completed"; $concl = "success"
            if ($global:FxCiPending[$sha] -gt 0) { $global:FxCiPending[$sha]--; $status = "in_progress"; $concl = $null }
            elseif ($global:FxCiFail[$sha]) { $concl = "failure" }
            return (@{ check_runs = @(@{ name = "Tests"; status = $status; conclusion = $concl }, @{ name = "Lint"; status = "completed"; conclusion = "success" }) } | ConvertTo-Json -Depth 5 -Compress)
        }
        if ($path -match '/pulls/(\d+)/files$') {
            $n = $Matches[1]; $head = Get-FxHead $n; $main = Get-FxMain
            $lines = @(& git --git-dir=$global:FxOrigin -c core.quotepath=false diff --name-status -M "$main...$head" 2>$null)
            $out = foreach ($l in $lines) {
                $c = $l -split "`t"
                if ($c[0] -like "R*") { $fn = $c[2]; $prev = $c[1] } else { $fn = $c[1]; $prev = $c[1] }
                if ($opt["--jq"] -like "*@tsv*") { "$fn`t$prev" } else { $fn }
            }
            return $out
        }
        if ($path -match '/pulls/(\d+)$') {
            $n = $Matches[1]; $head = Get-FxHead $n; $main = Get-FxMain
            $lines = @(& git --git-dir=$global:FxOrigin diff --name-status -M "$main...$head" 2>$null)
            if ($opt["--jq"] -eq ".changed_files") { return "$($lines.Count)" }
            $rc = @($global:FxStreams["rc:$n"] | Where-Object { $null -ne $_ }).Count
            if ($global:FxStreamFail["rc:$n"] -eq "truncate") { $rc++ }
            return ([ordered]@{ number = [int]$n; body = $global:FxIssueBodies["$n"]; review_comments = $rc; changed_files = $lines.Count } | ConvertTo-Json -Compress)
        }
        if ($path -match '/issues/(\d+)$') {
            $n = $Matches[1]
            $cc = @($global:FxStreams["issue:$n"] | Where-Object { $null -ne $_ }).Count
            if ($global:FxStreamFail["issue:$n"] -eq "truncate") { $cc++ }
            $b = if ($global:FxIssueBodies.ContainsKey("$n")) { $global:FxIssueBodies["$n"] } else { "fixture issue $n" }
            return ([ordered]@{ number = [int]$n; body = $b; comments = $cc; updated_at = "2026-09-28T00:00:00Z" } | ConvertTo-Json -Compress)
        }
        if ($path -match '/pulls$') {
            $global:FxCalls.Add("OPEN_PR_LIST page=$pageNo")
            if ($global:FxOpenPrListFail) { Write-Error "gh: HTTP 502"; $global:LASTEXITCODE = 1; return }
            if ($pageNo -gt 1) { return "[]" }
            $open = @($global:FxPrs.Values | Where-Object { $_.state -eq "OPEN" } | Sort-Object { [int]$_.number } | ForEach-Object {
                [ordered]@{ number = $_.number; head = @{ ref = $_.headRefName }; base = @{ ref = $(if ($_.base) { $_.base } else { "main" }) }; body = "$($_.body)"; labels = @(@($_.labels) | Where-Object { $_ } | ForEach-Object { @{ name = $_ } }); user = @{ login = "fixture" }; assignees = @(); draft = [bool]$_.isDraft; created_at = $_.createdAt }
            })
            if ($open.Count -eq 0) { return "[]" }
            return (ConvertTo-Json -InputObject $open -Depth 5 -Compress)
        }
    }
    $global:FxCalls.Add("UNHANDLED gh " + ($a -join " "))
    $global:LASTEXITCODE = 1
}

function global:Save-FxPrompt { param($Role, $Prompt)
    $global:FxPromptSeq++
    [System.IO.File]::WriteAllText((Join-Path $global:FxPromptDir ("{0:D3}-{1}.txt" -f $global:FxPromptSeq, $Role)), $Prompt, (New-Object System.Text.UTF8Encoding($false)))
}

function global:codex {
    if (@($args) -contains "--version") { $global:LASTEXITCODE = 0; return "fixture-codex 0.0.0" }
    $prompt = (@($input) -join "`n")
    $a = @($args | ForEach-Object { "$_" })
    $o = $a[[array]::IndexOf($a, "-o") + 1]
    $c = $a[[array]::IndexOf($a, "-C") + 1]
    $role = if ($prompt -match 'AUTO-NEXT ROADMAP SELECTION') { "gpt-next" } elseif ($prompt -match 'POST-MERGE DELTA AUDIT') { "gpt-delta" } elseif ($prompt -match 'POST-MERGE FULL REPOSITORY AUDIT') { "gpt-full" } else { "gpt-pr" }
    Save-FxPrompt $role $prompt
    $text = & $global:FxAi $role $prompt $c
    if ($role -eq "gpt-pr") { $text = "$text" + (Fx-EvidenceLine "gpt" $prompt) }
    [System.IO.File]::WriteAllText($o, $text, (New-Object System.Text.UTF8Encoding($false)))
    "codex-mock: wrote $role verdict"
    $global:LASTEXITCODE = 0
}

function global:claude {
    if (@($args) -contains "--version") { $global:LASTEXITCODE = 0; return "fixture-claude 0.0.0" }
    $prompt = (@($input) -join "`n")
    $role = if ($prompt -match 'AUTO-NEXT SELECTION CROSS-CHECK') { "claude-next" }
        elseif ($prompt -match 'AUTO-NEXT SLICE IMPLEMENTATION') { "implementer" }
        elseif ($prompt -match 'INDEPENDENT POST-MERGE DELTA AUDIT') { "claude-delta" }
        elseif ($prompt -match 'INDEPENDENT POST-MERGE FULL REPOSITORY AUDIT') { "claude-full" }
        elseif ($prompt -match 'POST-MERGE FULL-AUDIT BLOCKER REMEDIATION') { "fixer-rem" }
        elseif ($prompt -match 'CURRENT PR exact-head BLOCKER repair') { "fixer-pr" }
        else { "claude-pr" }
    Save-FxPrompt $role $prompt
    $text = & $global:FxAi $role $prompt ((Get-Location).Path)
    if ($role -eq "claude-pr") { $text = "$text" + (Fx-EvidenceLine "claude" $prompt) }
    $global:LASTEXITCODE = 0
    return @($text -split "`n")
}

function global:Fx-Verdict { param($Kind, $Sha, $V, $S) return "$Kind=$Sha`nVERDICT=$V`nSUMMARY=$S`n" }
# V2 auditor contract: EVIDENCE_SEEN = every marked source id in the packet (default); scenarios may omit one.
function global:Fx-EvidenceLine { param($Who, $Prompt)
    # content-bound identities exactly as the packet prints them after identity=
    $ids = @([regex]::Matches($Prompt, '(?m)^\[SOURCE identity=(\S+) ') | ForEach-Object { $_.Groups[1].Value } | Select-Object -Unique)
    $sc = $global:FxScenario
    if (($sc -eq "packet-evidence-omit" -and $Who -eq "gpt") -or ($sc -eq "packet-omit-claude" -and $Who -eq "claude")) {
        $ids = @($ids | Where-Object { $_ -notlike "github_issue_comment:100009001@*" })
    }
    if ($sc -eq "packet-evwrong" -and $Who -eq "gpt") {
        $ids = @($ids | ForEach-Object { if ($_ -like "github_issue_comment:100009001@*") { "github_issue_comment:100009001@$('ab' * 32)" } else { $_ } })
    }
    if ($sc -eq "packet-evidence-idonly" -and $Who -eq "gpt") {
        $ids = @($ids | ForEach-Object { ($_ -split '@')[0] -replace '^[a-z_]+:', '' })
    }
    if ($ids.Count -eq 0) { return "EVIDENCE_SEEN=NONE`n" }
    return "EVIDENCE_SEEN=$($ids -join ',')`n"
}
function global:Fx-Replace { param($Dir, $Rel, $From, $To)
    $p = Join-Path $Dir $Rel
    $t = [System.IO.File]::ReadAllText($p)
    [System.IO.File]::WriteAllText($p, $t.Replace($From, $To), (New-Object System.Text.UTF8Encoding($false)))
}
# FIXER sandbox check: push must be impossible from inside the fixer.
function global:Fx-TryPush { param($Dir)
    & git -C $Dir push -q origin "HEAD:refs/heads/fixer-escape" 2>$null
    $global:FxCalls.Add("FIXER_PUSH_EXIT=$LASTEXITCODE")
}

$global:FxAi = {
    param($role, $prompt, $cwd)
    $h = [regex]::Match($prompt, 'AUDIT_HEAD=([0-9a-f]{40})').Groups[1].Value
    $m = [regex]::Match($prompt, '(?m)^AUDIT_MAIN=([0-9a-f]{40})').Groups[1].Value
    $sc = $global:FxScenario
    switch ($role) {
        "gpt-next" {
            $nm = [regex]::Match($prompt, '(?m)^NEXT_MAIN=([0-9a-f]{40})').Groups[1].Value
            if ($sc -in @("auto-next-human", "auto-next-live")) {
                $cat = if ($sc -eq "auto-next-live") { "LIVE" } else { "NEW_PRODUCT_FEATURE" }
                return "NEXT_MAIN=$nm`nDECISION=HOLD`nSLICE_ID=fixture-decision`nSLICE_TITLE=needs the user`nHOLD_CATEGORY=$cat`nHOLD_REASON=fixture: this step is the user's decision`nCANONICAL_SOURCES=ROADMAP.md 14`nALLOWED_PATHS=NONE`nSCHEMA_CHANGE=NONE`nACCEPTANCE=NONE`nFORBIDDEN=NONE`n`nSPEC:`nNONE`n"
            }
            if ($sc -eq "auto-next-hold") {
                return "NEXT_MAIN=$nm`nDECISION=HOLD`nSLICE_ID=m5-create-adoption`nSLICE_TITLE=CREATE adoption`nHOLD_CATEGORY=SEPARATE_AUTHORIZATION_REQUIRED`nHOLD_REASON=ROADMAP.md 14: each later slice needs its own authorization`nCANONICAL_SOURCES=ROADMAP.md 14`nALLOWED_PATHS=NONE`nSCHEMA_CHANGE=NONE`nACCEPTANCE=NONE`nFORBIDDEN=NONE`n`nSPEC:`nNONE`n"
            }
            if ($sc -like "auto-next*" -and -not (Test-Path (Join-Path $cwd "docs/feature.md"))) {
                return "NEXT_MAIN=$nm`nDECISION=PROCEED`nSLICE_ID=fixture-feature`nSLICE_TITLE=Fixture feature doc and test`nHOLD_CATEGORY=NONE`nHOLD_REASON=NONE`nCANONICAL_SOURCES=ROADMAP.md fixture`nALLOWED_PATHS=docs/feature.md,tests/`nSCHEMA_CHANGE=NONE`nACCEPTANCE=feature doc exists and test pins it`nFORBIDDEN=provider calls`n`nSPEC:`nAdd docs/feature.md and tests/test_feature.py.`n"
            }
            return "NEXT_MAIN=$nm`nDECISION=DONE`nSLICE_ID=NONE`nSLICE_TITLE=roadmap complete`nHOLD_CATEGORY=NONE`nHOLD_REASON=NONE`nCANONICAL_SOURCES=ROADMAP.md`nALLOWED_PATHS=NONE`nSCHEMA_CHANGE=NONE`nACCEPTANCE=NONE`nFORBIDDEN=NONE`n`nSPEC:`nNONE`n"
        }
        "claude-next" {
            $cm = [regex]::Match($prompt, '(?m)^CHECK_MAIN=([0-9a-f]{40})').Groups[1].Value
            if ($sc -eq "auto-next-disputed") { return "CHECK_MAIN=$cm`nCHECK=DISAGREE`nCHECK_REASON=ROADMAP.md 14 gates this slice behind a separate authorization" }
            return "CHECK_MAIN=$cm`nCHECK=AGREE`nCHECK_REASON=scope and order fixed; no separate authorization gate"
        }
        "implementer" {
            Fx-TryPush $cwd
            W "docs/feature.md" "# Feature`nimplemented by auto-next`n" $cwd
            W "tests/test_feature.py" "def test_feature():`n    assert True`n" $cwd
            if ($sc -eq "auto-next-scope") { Fx-Replace $cwd "app/unrelated.py" "return 0" "return 7" }
            return "FIXER_RESULT=READY`nFIXER_SUMMARY=added feature doc and test"
        }
        "gpt-pr" {
            $global:FxGptPrCount++
            if ($sc -eq "packet-blocker-no-cache" -and $global:FxGptPrCount -eq 1) { return (Fx-Verdict "AUDIT_HEAD" $h "BLOCKER" "first-run blocker that must never be reused as cache") }
            if ($sc -like "remediation-authorized*" -and $prompt -notmatch 'AUTHORIZATION EVIDENCE') { return (Fx-Verdict "AUDIT_HEAD" $h "INSUFFICIENT" "no authorization evidence in packet") }
            if ($sc -eq "gpt-insufficient") { return (Fx-Verdict "AUDIT_HEAD" $h "INSUFFICIENT" "packet not enough") }
            if ($sc -eq "gpt-human") { return (Fx-Verdict "AUDIT_HEAD" $h "HUMAN_DECISION_REQUIRED" "NEW_PRODUCT_FEATURE: the diff adds a feature no canonical requirement contains") }
            # an auditor that asks for the user without naming a category of the closed list has decided nothing
            if ($sc -eq "gpt-human-uncategorised") { return (Fx-Verdict "AUDIT_HEAD" $h "HUMAN_DECISION_REQUIRED" "someone should look at this architecture choice") }
            # blocked until a repair re-analyses the problem (the marker only an INDEPENDENT RE-ANALYSIS writes)
            if ($sc -eq "max-cycles" -and $prompt -notmatch '\+REANALYSED_ROOT_CAUSE') { return (Fx-Verdict "AUDIT_HEAD" $h "BLOCKER" "still blocked") }
            if ($prompt -match '\+BUG_MARKER_GPT') { return (Fx-Verdict "AUDIT_HEAD" $h "BLOCKER" "BUG_MARKER_GPT contradicts contract") }
            return (Fx-Verdict "AUDIT_HEAD" $h "PASS" "gpt ok")
        }
        "claude-pr" {
            if ($prompt -match '\+BUG_MARKER_CLAUDE') { return (Fx-Verdict "AUDIT_HEAD" $h "BLOCKER" "BUG_MARKER_CLAUDE weakens rule") }
            return (Fx-Verdict "AUDIT_HEAD" $h "PASS" "claude ok")
        }
        { $_ -in @("gpt-full", "gpt-delta") } {
            if ($role -eq "gpt-delta" -and $sc -eq "two-merges-escalate") { return (Fx-Verdict "AUDIT_MAIN" $m "PASS" "delta ok") + "ESCALATE_FULL=YES: shared runtime path across owners`n" }
            if ($role -eq "gpt-delta" -and $sc -eq "two-merges-insufficient") { return (Fx-Verdict "AUDIT_MAIN" $m "INSUFFICIENT" "cannot judge from delta") + "ESCALATE_FULL=NO`n" }
            $stale = Select-String -Path (Join-Path $cwd "docs\*.md") -Pattern "STALE_DOC_MARKER" -Quiet
            if ($stale) { return (Fx-Verdict "AUDIT_MAIN" $m "BLOCKER" "docs/stale.md stale adoption condition") + "`nBLOCKERS:`n1. docs/stale.md STALE_DOC_MARKER`n" }
            return (Fx-Verdict "AUDIT_MAIN" $m "PASS" "main ok")
        }
        { $_ -in @("claude-full", "claude-delta") } {
            $stale = Select-String -Path (Join-Path $cwd "docs\*.md") -Pattern "STALE_DOC_MARKER|CLAUDE_ONLY_STALE" -Quiet
            if ($stale) { return (Fx-Verdict "AUDIT_MAIN" $m "BLOCKER" "stale doc contradiction") }
            return (Fx-Verdict "AUDIT_MAIN" $m "PASS" "main ok")
        }
        "fixer-pr" {
            Fx-TryPush $cwd
            if ($sc -eq "claude-blocker-hold") { return "FIXER_RESULT=HUMAN_HOLD`nFIXER_REASON=needs policy decision" }
            if ($sc -eq "fixer-human") { return "FIXER_RESULT=HUMAN_DECISION_REQUIRED`nFIXER_CATEGORY=PRODUCT_DIRECTION_UNDECIDED`nFIXER_REASON=two product directions, no canonical text decides" }
            if ($sc -eq "scope-expansion") {
                Fx-Replace $cwd "app/unrelated.py" "return 0" "return 99"
                Fx-Replace $cwd "docs/contract.md" "BUG_MARKER_GPT" "fixed-by-fixer"
                return "FIXER_RESULT=READY`nFIXER_SUMMARY=fixed the marker; the fix also needed app/unrelated.py"
            }
            if ($sc -eq "max-cycles") {
                if ($prompt -match 'INDEPENDENT RE-ANALYSIS') { Add-Content (Join-Path $cwd "docs/contract.md") "REANALYSED_ROOT_CAUSE"; return "FIXER_RESULT=READY`nFIXER_SUMMARY=re-analysed and fixed the root cause" }
                Add-Content (Join-Path $cwd "docs/contract.md") "attempt $(Get-Random)"; return "FIXER_RESULT=READY`nFIXER_SUMMARY=attempt"
            }
            Fx-Replace $cwd "docs/contract.md" "BUG_MARKER_GPT" "fixed-by-fixer"
            Fx-Replace $cwd "docs/contract.md" "BUG_MARKER_CLAUDE" "fixed-by-fixer"
            return "done`nFIXER_RESULT=READY`nFIXER_SUMMARY=removed marker"
        }
        "fixer-rem" {
            Fx-TryPush $cwd
            if ($sc -like "remediation-auth*") {
                if ($prompt -notmatch 'ARCHITECT RULING') { return "FIXER_RESULT=HUMAN_HOLD`nFIXER_HOLD_CATEGORY=ARCHITECTURE_OR_POLICY`nFIXER_REASON=needs architect" }
                if ($sc -eq "remediation-authorized-scope") { Fx-Replace $cwd "app/unrelated.py" "return 0" "return 5" }
            }
            if ($sc -eq "remediation-migration-hold") {
                $p = Join-Path $cwd "app/db/migrations/versions/0099_fix.py"
                New-Item -ItemType Directory -Force -Path (Split-Path $p -Parent) | Out-Null
                Set-Content $p "rev = '0099'"
                Fx-Replace $cwd "docs/stale.md" "STALE_DOC_MARKER: endpoint adoption waits for new evidence" "adoption needs its own authorization"
                return "FIXER_RESULT=READY`nFIXER_SUMMARY=fixed the stale condition; the fix needed a migration"
            }
            Fx-Replace $cwd "docs/stale.md" "STALE_DOC_MARKER: endpoint adoption waits for new evidence" "adoption needs its own authorization"
            Fx-Replace $cwd "docs/stale.md" "CLAUDE_ONLY_STALE ui ruling" "ui ruling aligned"
            return "FIXER_RESULT=READY`nFIXER_SUMMARY=removed stale adoption condition"
        }
    }
}
$global:FxScenario = $Scenario
$global:FxHostDir = $hostDir

# ---------------- run ----------------
$startPr = 1
if ($Scenario -like "*remediation*") {
    # resume from runtime state (merged PR #1), no argument
    @{ version = "1.3"; current_pr = 1; status = "MERGED"; action = "LOOKAHEAD_PROMOTION"; auto_merge = $false } |
        ConvertTo-Json | Set-Content -Encoding utf8 (Join-Path $hostDir "state\orchestrator-runtime.json")
}

if ($Scenario -like "remediation-auth*") {
    $login = if ($Scenario -eq "remediation-auth-invalid") { "mallory" } else { "fixture" }
    $global:FxComments["777"] = @{ id = 777; user = @{ login = $login }; html_url = "https://github.com/fixture/icbm/issues/89#issuecomment-777"; body = "## Architect ruling`n**Exact canonical main:** ``$(Get-FxMain)```nR1 - fix docs/stale.md adoption wording only." }
    @{ main = (Get-FxMain); comment_id = 777; items = @("R1"); allowed_files = @("docs/stale.md") } |
        ConvertTo-Json | Set-Content -Encoding utf8 (Join-Path $hostDir "state\remediation-authorization.json")
}

if ($Scenario -eq "owner-hold") {
    @{ reason = "owner placed a hold on this PR" } | ConvertTo-Json | Set-Content -Encoding utf8 (Join-Path $hostDir "state\merge-hold-pr-1.json")
}

$originMainBefore = Get-FxMain
$transcript = Join-Path $fx "orchestrator.log"
Start-Transcript -Path $transcript -Force | Out-Null
$global:FxChecks = [ordered]@{}
$skipOrch = $false

function Fx-RunAudit { param([hashtable]$P)
    $o = @(& (Join-Path $hostDir "run-audit-v1.1.ps1") @P *>&1 | ForEach-Object { "$_" })
    $o | ForEach-Object { Write-Host "  [direct] $_" }
    return ,$o
}
function Fx-Line { param($Out, $Key)
    $m = @($Out | Where-Object { $_ -match "^$Key=" } | Select-Object -Last 1)
    if ($m.Count) { return ($m[0] -replace "^$Key=", "") }
    return ""
}
function Fx-Sha { param([byte[]]$B) $s = [System.Security.Cryptography.SHA256]::Create(); return (([BitConverter]::ToString($s.ComputeHash($B))) -replace '-', '').ToLower() }

try {
    function Fx-SetBody { param($Id, $Body) $global:FxComments["$Id"].body = $Body }
    function Fx-Reason { param($Out) return (@($Out | Where-Object { $_ -match '^PACKET_HOLD_REASON=' } | ForEach-Object { $_ -replace '^PACKET_HOLD_REASON=', '' }) -join ';') }
    $mfPath = Join-Path $hostDir "state\audit-sources-pr-1.json"

    if ($Scenario -eq "packet-reproducible") {
        # Cited evidence only, deterministic, edit-aware, and never held by a marker or a missing classification.
        $skipOrch = $true
        $pkDir = Join-Path $hostDir "state\packets"
        $r1 = Fx-RunAudit @{ PrNumber = 1; PacketOnly = $true }
        $r2 = Fx-RunAudit @{ PrNumber = 1; PacketOnly = $true }
        $d1 = Fx-Line $r1 "PACKET_DIGEST"; $d2 = Fx-Line $r2 "PACKET_DIGEST"
        $pk = @(Get-ChildItem $pkDir -Filter "*.packet.txt")
        $bytes = [System.IO.File]::ReadAllBytes($pk[0].FullName)
        $ptext = (New-Object System.Text.UTF8Encoding($false)).GetString($bytes)
        $idents = @([regex]::Matches($ptext, '(?m)^SOURCE=(\S+) ') | ForEach-Object { $_.Groups[1].Value })
        $global:FxChecks.complete_1 = Fx-Line $r1 "PACKET_COMPLETE"
        $global:FxChecks.hold_reasons_1 = Fx-Reason $r1
        $global:FxChecks.same_inputs_twice_same_digest = ($d1 -and $d1 -eq $d2)
        $global:FxChecks.write_1_2 = "$(Fx-Line $r1 'PACKET_WRITE')/$(Fx-Line $r2 'PACKET_WRITE')"
        $global:FxChecks.digest_is_sha256_of_packet_bytes = ((Fx-Sha $bytes) -eq $d1)
        $global:FxChecks.no_bom_no_cr = ($bytes[0] -ne 0xEF) -and (-not ($bytes -contains 13))
        $global:FxChecks.no_abs_path = (-not $ptext.Contains($hostDir)) -and (-not $ptext.Contains("C:\"))
        $global:FxChecks.no_watermark_or_updated_at_in_packet = (-not ($ptext -match 'watermark|updated_at|2026-09-28T00:00:00Z'))
        # exactly the three cited sources, each once, all required; the legacy record and the uncited marker are not in it
        $global:FxChecks.source_locators = (@($idents | ForEach-Object { ($_ -split '@')[0] }) -join ' ')
        $global:FxChecks.each_section_once = (@($idents | Where-Object { ([regex]::Matches($ptext, [regex]::Escape("[SOURCE identity=$_ "))).Count -ne 1 -or ([regex]::Matches($ptext, [regex]::Escape("[/SOURCE identity=$_]"))).Count -ne 1 }).Count -eq 0)
        $global:FxChecks.legacy_record_not_in_packet = (-not $ptext.Contains("100009300")) -and (-not $ptext.Contains("scope: PR #1"))
        $global:FxChecks.uncited_marker_not_in_packet = (-not $ptext.Contains("100009004"))
        $pm = Get-Content (Get-ChildItem $pkDir -Filter "*$($d1.Substring(0,12)).manifest.json")[0].FullName -Raw | ConvertFrom-Json
        $global:FxChecks.manifest_json_sources = @($pm.sources | ForEach-Object { "$($_.class):$($_.kind):$($_.origin)" }) -join ","
        $global:FxChecks.manifest_json_required_count = @($pm.required).Count
        $global:FxChecks.packet_format = $pm.packet_format
        $scan = Get-Content (Join-Path $pkDir "pr-1-head-$((Get-FxHead 1).Substring(0,12)).scan.json") -Raw | ConvertFrom-Json
        $global:FxChecks.scanned_streams = (@($scan.streams | ForEach-Object { $_.stream }) -join ',')
        $global:FxChecks.scan_unresolved = (@($scan.unresolved_references) -join ',')
        $global:FxChecks.scan_marked_not_cited = (@($scan.marked_not_cited | ForEach-Object { $_.locator -replace '^github_issue_comment:', '' }) -join ',')
        # provenance-only change: updated_at of a source → same digest
        $global:FxComments["100009002"].updated_at = "2026-09-29T11:11:11Z"
        $r3 = Fx-RunAudit @{ PrNumber = 1; PacketOnly = $true }
        $global:FxChecks.provenance_only_change_same_digest = ((Fx-Line $r3 "PACKET_DIGEST") -eq $d1)
        # immutability: tamper → PACKET_IMMUTABILITY_VIOLATION
        [System.IO.File]::WriteAllBytes($pk[0].FullName, [byte[]]($bytes + [byte]0x78))
        $r4 = Fx-RunAudit @{ PrNumber = 1; PacketOnly = $true }
        $global:FxChecks.tampered_rebuild = Fx-Line $r4 "AUDIT_BLOCKED"
        [System.IO.File]::WriteAllBytes($pk[0].FullName, $bytes)
        # a CITED body edited → complete, NEW digest (no hold, nobody re-classifies anything)
        $orig9002 = $global:FxComments["100009002"].body
        Fx-SetBody 100009002 "Evidence E1 (EDITED).`n"
        $r5 = Fx-RunAudit @{ PrNumber = 1; PacketOnly = $true }
        $d5 = Fx-Line $r5 "PACKET_DIGEST"
        $global:FxChecks.cited_body_edit_new_digest = ((Fx-Line $r5 "PACKET_COMPLETE") -eq "True" -and $d5 -and $d5 -ne $d1 -and -not (Fx-Reason $r5))
        Fx-SetBody 100009002 $orig9002
        $r6 = Fx-RunAudit @{ PrNumber = 1; PacketOnly = $true }
        $global:FxChecks.reverted_digest_equals_first = ((Fx-Line $r6 "PACKET_DIGEST") -eq $d1)
        $global:FxChecks.reverted_write = Fx-Line $r6 "PACKET_WRITE"
        # an UNCITED marked source edited, an old unmarked comment edited into a marker, a brand-new OWNER-AMENDMENT,
        # the legacy record edited and then removed: all provenance → complete, the SAME digest, no hold
        Fx-SetBody 100009004 "[EVIDENCE-PACKET]`nOptional background evidence (EDITED).`n"
        Fx-SetBody 100009050 "[ARCHITECT-INSTRUCTION]`nold comment edited in place to add a marker`n"
        Fx-C 100009005 "[OWNER-AMENDMENT]`nnew amendment nobody classified`n"; $global:FxStreams["issue:1"] = @($global:FxStreams["issue:1"]) + 100009005
        Fx-SetBody 100009300 (Fx-Record -Preamble "legacy record, edited" -ScopeLine "scope: PR #7")
        $r7 = Fx-RunAudit @{ PrNumber = 1; PacketOnly = $true }
        $global:FxChecks.markers_and_legacy_record_are_provenance = ((Fx-Line $r7 "PACKET_COMPLETE") -eq "True" -and (Fx-Line $r7 "PACKET_DIGEST") -eq $d1 -and -not (Fx-Reason $r7))
        $global:FxStreams["issue:1"] = @($global:FxStreams["issue:1"] | Where-Object { $_ -ne 100009300 })
        $r8 = Fx-RunAudit @{ PrNumber = 1; PacketOnly = $true }
        $global:FxChecks.no_record_at_all_same_digest = ((Fx-Line $r8 "PACKET_COMPLETE") -eq "True" -and (Fx-Line $r8 "PACKET_DIGEST") -eq $d1)
        $global:FxChecks.pointer_present = (Test-Path (Join-Path $pkDir "pr-1-head-$((Get-FxHead 1).Substring(0,12)).current"))
        # the declaration cites one more source → it becomes a fourth required source, new digest
        $global:FxIssueBodies["1"] = $global:FxPrBody + "Also: background ``100009004``.`n"
        $r9 = Fx-RunAudit @{ PrNumber = 1; PacketOnly = $true }
        $global:FxChecks.new_citation_new_source = ((Fx-Line $r9 "PACKET_COMPLETE") -eq "True" -and (Fx-Line $r9 "PACKET_DIGEST") -ne $d1 -and @($r9 | Where-Object { $_ -match '^PACKET_SOURCE=' }).Count -eq 5)
        $global:FxIssueBodies["1"] = $global:FxPrBody
        # an optional host manifest that designates a stream the body already names changes no source
        $global:FxManifest | ConvertTo-Json -Depth 10 | Set-Content -Encoding utf8 $mfPath
        $r10 = Fx-RunAudit @{ PrNumber = 1; PacketOnly = $true }
        $global:FxChecks.optional_manifest_same_sources = ((Fx-Line $r10 "PACKET_COMPLETE") -eq "True" -and (Fx-Line $r10 "PACKET_SOURCES_REQUIRED") -eq (Fx-Line $r1 "PACKET_SOURCES_REQUIRED"))
        Remove-Item $mfPath -Force
        $global:FxChecks.ai_prompts = @(Get-ChildItem $promptDir).Count
        $expected = [ordered]@{
            complete_1 = "True"; hold_reasons_1 = ""; same_inputs_twice_same_digest = $true; write_1_2 = "WRITTEN/EXISTING_BYTE_IDENTICAL"
            digest_is_sha256_of_packet_bytes = $true; no_bom_no_cr = $true; no_abs_path = $true; no_watermark_or_updated_at_in_packet = $true
            source_locators = "github_issue_comment:100009001 github_issue_comment:100009002 github_pr_body:1 github_pr_review:1/100009101"
            each_section_once = $true; legacy_record_not_in_packet = $true; uncited_marker_not_in_packet = $true
            manifest_json_sources = "required:ARCHITECT-INSTRUCTION:referenced,required:UNMARKED:referenced,required:UNMARKED:declaration,required:EVIDENCE-PACKET:referenced"
            manifest_json_required_count = 4; packet_format = "icbm-audit-packet-v3"
            scanned_streams = "issue_comments:1,issue_comments:89,pr_body:1,pr_review_comments:1,pr_reviews:1"
            scan_unresolved = ""; scan_marked_not_cited = "100009300,100009004"
            provenance_only_change_same_digest = $true; tampered_rebuild = "PACKET_IMMUTABILITY_VIOLATION"
            cited_body_edit_new_digest = $true; reverted_digest_equals_first = $true; reverted_write = "EXISTING_BYTE_IDENTICAL"
            markers_and_legacy_record_are_provenance = $true; no_record_at_all_same_digest = $true; pointer_present = $true
            new_citation_new_source = $true; optional_manifest_same_sources = $true; ai_prompts = 0
        }
        $failed = @($expected.Keys | Where-Object { "$($global:FxChecks[$_])" -ne "$($expected[$_])" })
        $global:FxChecks.expect = if ($failed.Count -eq 0) { "PASS" } else { "FAIL:" + ($failed -join ",") }
        Write-Host "FX_EXPECT=$($global:FxChecks.expect)"
    }
    if ($Scenario -eq "packet-many-files") {
        # Segmented packet: the full manifest sits once in the canonical packet; every audit call stays within the
        # call limit and carries only the manifest's count + sha256 and its own FILES; coverage is host-verified.
        $skipOrch = $true
        $r1 = Fx-RunAudit @{ PrNumber = 1; PacketOnly = $true }
        $d1 = Fx-Line $r1 "PACKET_DIGEST"
        $global:FxChecks.complete = Fx-Line $r1 "PACKET_COMPLETE"
        $global:FxChecks.incomplete_reasons = (@($r1 | Where-Object { $_ -match '^PACKET_INCOMPLETE_REASON=' }) -join ';')
        $calls = @(Get-ChildItem (Join-Path $hostDir "logs") -Filter "pr-1-packet-call*-*.txt")
        $global:FxChecks.calls = $calls.Count
        $callTexts = @($calls | ForEach-Object { [System.IO.File]::ReadAllText($_.FullName) })
        # A call body is the diff material the host limits (auditCallCharLimit = 42000): the call manifest reference plus
        # its segments, without the call preamble and the source block that every call carries.
        $bodies = @($callTexts | ForEach-Object { $_.Substring($_.IndexOf('CHANGED FILE MANIFEST (GitHub')) })
        $global:FxChecks.max_call_body_chars = (@($bodies | ForEach-Object { $_.Length }) | Measure-Object -Maximum).Maximum
        $global:FxChecks.every_call_body_within_42000 = (@($bodies | Where-Object { $_.Length -gt 42000 }).Count -eq 0)
        $global:FxChecks.every_call_names_manifest_digest = (@($callTexts | Where-Object { $_ -notmatch 'CHANGED FILE MANIFEST \(GitHub PR files API, 701 files, sha256=[0-9a-f]{64} ' }).Count -eq 0)
        $manyInCalls = @($callTexts | ForEach-Object { [regex]::Matches($_, '(?m)^docs/many/\S+\.md\r?$').Count } | Measure-Object -Sum).Sum
        $global:FxChecks.each_file_listed_once_across_calls = ($manyInCalls -eq 700)
        $pk = @(Get-ChildItem (Join-Path $hostDir "state\packets") -Filter "*$($d1.Substring(0,12)).packet.txt")
        $ptext = [System.IO.File]::ReadAllText($pk[0].FullName)
        $section = [regex]::Match($ptext, '(?s)\n=== CHANGED FILE MANIFEST \(701 files, sha256=([0-9a-f]{64})\) ===\n(.*?)\n=== END CHANGED FILE MANIFEST ===\n')
        $global:FxChecks.canonical_manifest_once = ($section.Success -and ([regex]::Matches($ptext, '=== CHANGED FILE MANIFEST \(')).Count -eq 1)
        $listed = if ($section.Success) { $section.Groups[2].Value } else { "" }
        $global:FxChecks.canonical_manifest_digest_matches = ($section.Success -and (Fx-Sha ((New-Object System.Text.UTF8Encoding($false)).GetBytes($listed))) -eq $section.Groups[1].Value)
        $global:FxChecks.canonical_manifest_lists_all = (@($listed -split "`n" | Where-Object { $_ }).Count -eq 701)
        $r2 = Fx-RunAudit @{ PrNumber = 1; PacketOnly = $true }
        $global:FxChecks.reproducible = ((Fx-Line $r2 "PACKET_DIGEST") -eq $d1)
        $global:FxChecks.ai_prompts = @(Get-ChildItem $promptDir).Count
        # Pinned expectations: every value below is required; any other value is a failure of this scenario.
        $expected = [ordered]@{
            complete = "True"; incomplete_reasons = ""; every_call_body_within_42000 = $true
            every_call_names_manifest_digest = $true; each_file_listed_once_across_calls = $true
            canonical_manifest_once = $true; canonical_manifest_digest_matches = $true
            canonical_manifest_lists_all = $true; reproducible = $true; ai_prompts = 0
        }
        $failed = @($expected.Keys | Where-Object { "$($global:FxChecks[$_])" -ne "$($expected[$_])" })
        if ($global:FxChecks.calls -lt 2) { $failed += "calls>=2" }
        if (-not ($global:FxChecks.max_call_body_chars -gt 0 -and $global:FxChecks.max_call_body_chars -le 42000)) { $failed += "max_call_body_chars<=42000" }
        $global:FxChecks.expect = if ($failed.Count -eq 0) { "PASS" } else { "FAIL:" + ($failed -join ",") }
        Write-Host "FX_EXPECT=$($global:FxChecks.expect)"
    }

    if ($Scenario -eq "packet-authority") {
        # What can still stop a packet is technical only: an unreadable declaration or stream, a bad host manifest.
        $skipOrch = $true
        function Fx-Class { param($Out) return (@($Out | Where-Object { $_ -match '^HOLD_CLASS=' } | Select-Object -Unique) -join ';') }
        $base = Fx-RunAudit @{ PrNumber = 1; PacketOnly = $true }
        $global:FxChecks.a0_clean_with_grammar_negatives_and_markers_in_stream = Fx-Line $base "PACKET_COMPLETE"
        # a) a host manifest that tries to classify → invalid manifest, a TECHNICAL_HOLD (never a classification)
        '{"pr":1,"designated_streams":[{"type":"issue_comments","issue":1}],"sources":[{"id":"x","required":true}]}' | Set-Content -Encoding utf8 $mfPath
        $ra = Fx-RunAudit @{ PrNumber = 1; PacketOnly = $true }
        $global:FxChecks.a_host_manifest_with_unknown_key = Fx-Reason $ra
        $global:FxChecks.a_hold_class = Fx-Class $ra
        Remove-Item $mfPath -Force
        # b) a marked source nobody classified → no hold; unchanged rerun → the same clean digest
        Fx-C 100009005 "[OWNER-AMENDMENT]`nnew amendment nobody classified`n"; $global:FxStreams["issue:1"] = @($global:FxStreams["issue:1"]) + 100009005
        $rb = Fx-RunAudit @{ PrNumber = 1; PacketOnly = $true }
        $global:FxChecks.b_unclassified_marker_no_hold = "$(Fx-Line $rb 'PACKET_COMPLETE')/$(Fx-Reason $rb)/$((Fx-Line $rb 'PACKET_DIGEST') -eq (Fx-Line $base 'PACKET_DIGEST'))"
        # c) a second legacy record that "conflicts" with the first → both are history, no hold
        Fx-C 100009301 ("[ARCHITECT-INSTRUCTION]`n`nsecond record`nscope: PR #1`n`nrequired:`n- issue-comment 100009004 sha256 $(Fx-D $global:FxComments['100009004'].body) — architect wants it required`n")
        $global:FxStreams["issue:1"] = @($global:FxStreams["issue:1"]) + 100009301
        $rc = Fx-RunAudit @{ PrNumber = 1; PacketOnly = $true }
        $global:FxChecks.c_conflicting_legacy_records_no_hold = "$(Fx-Line $rc 'PACKET_COMPLETE')/$(Fx-Reason $rc)/$((Fx-Line $rc 'PACKET_DIGEST') -eq (Fx-Line $base 'PACKET_DIGEST'))"
        # d) an unparseable legacy record → history, no hold
        Fx-SetBody 100009300 "[OWNER-AMENDMENT]`nscope: PR #1`nrequired:`n- 100009004 deadbeef (untyped)`n- git blobs at base: CLAUDE.md abc`n"
        $rd = Fx-RunAudit @{ PrNumber = 1; PacketOnly = $true }
        $global:FxChecks.d_unparseable_legacy_record_no_hold = "$(Fx-Line $rd 'PACKET_COMPLETE')/$(Fx-Reason $rd)"
        # e) the declaration cites an id no scanned stream holds → declared evidence cannot be read: TECHNICAL_HOLD
        $global:FxIssueBodies["1"] = $global:FxPrBody + "gone ``100009998``.`n"
        $re = Fx-RunAudit @{ PrNumber = 1; PacketOnly = $true }
        $global:FxChecks.e_unresolved_citation_holds = "$(Fx-Reason $re)/$(Fx-Class $re)/digest=$(Fx-Line $re 'PACKET_DIGEST')"
        # e2) a cited source deleted after it was cited → the same hold, never a silently smaller packet
        $global:FxIssueBodies["1"] = $global:FxPrBody
        $kept = $global:FxStreams["issue:89"]; $global:FxStreams["issue:89"] = @($kept | Where-Object { $_ -ne 100009002 })
        $re2 = Fx-RunAudit @{ PrNumber = 1; PacketOnly = $true }
        $global:FxChecks.e2_deleted_cited_source_holds = "$(Fx-Reason $re2)/$(Fx-Class $re2)"
        $global:FxStreams["issue:89"] = $kept
        # f) the declaration cites evidence of an issue it does not name → that stream is not scanned, the citation
        #    is unresolved and holds; naming the issue is what makes it readable
        $global:FxIssueBodies["1"] = "fixture PR body`n`nSources: ``100009001``, ``100009002``, ``100009101``.`n"
        $rf = Fx-RunAudit @{ PrNumber = 1; PacketOnly = $true }
        $global:FxChecks.f_streams_without_issue_reference = (@($rf | Where-Object { $_ -match 'SCANNED_STREAM' } | ForEach-Object { ([regex]::Match($_, 'SCANNED_STREAM\s*:\s*(\S+)')).Groups[1].Value }) -join ',')
        $global:FxChecks.f_unnamed_issue_citation_holds = Fx-Reason $rf
        # f2) numbers outside a code span are not citations
        $global:FxIssueBodies["1"] = $global:FxPrBody + "run 100009998 and 36699770595, 100009004 too.`n"
        $rf2 = Fx-RunAudit @{ PrNumber = 1; PacketOnly = $true }
        $global:FxChecks.f2_bare_numbers_are_not_citations = "$(Fx-Line $rf2 'PACKET_COMPLETE')/$((Fx-Line $rf2 'PACKET_DIGEST') -ne (Fx-Line $base 'PACKET_DIGEST'))/$(@($rf2 | Where-Object { $_ -match '^PACKET_SOURCE=' }).Count)"
        $global:FxIssueBodies["1"] = $global:FxPrBody
        # g) unreadable / truncated streams → TECHNICAL_HOLD, never a partial scan
        $global:FxStreamFail["issue:89"] = "page"
        $rg = Fx-RunAudit @{ PrNumber = 1; PacketOnly = $true }
        $global:FxChecks.g_stream_page_fail = "$(Fx-Reason $rg)/$(Fx-Class $rg)"
        $global:FxStreamFail.Remove("issue:89"); $global:FxStreamFail["issue:1"] = "truncate"
        $rh = Fx-RunAudit @{ PrNumber = 1; PacketOnly = $true }
        $global:FxChecks.h_stream_truncated = "$(Fx-Reason $rh)/$(Fx-Class $rh)"
        $global:FxStreamFail.Remove("issue:1")
        $global:FxChecks.ai_prompts = @(Get-ChildItem $promptDir).Count
        $expected = [ordered]@{
            a0_clean_with_grammar_negatives_and_markers_in_stream = "True"
            a_host_manifest_with_unknown_key = "SOURCE_MANIFEST_INVALID:UNKNOWN_KEY:sources"; a_hold_class = "HOLD_CLASS=TECHNICAL_HOLD"
            b_unclassified_marker_no_hold = "True//True"; c_conflicting_legacy_records_no_hold = "True//True"
            d_unparseable_legacy_record_no_hold = "True/"
            e_unresolved_citation_holds = "CITED_SOURCE_UNRESOLVED:100009998/HOLD_CLASS=TECHNICAL_HOLD/digest="
            e2_deleted_cited_source_holds = "CITED_SOURCE_UNRESOLVED:100009002/HOLD_CLASS=TECHNICAL_HOLD"
            f_streams_without_issue_reference = "issue_comments:1,pr_body:1,pr_review_comments:1,pr_reviews:1"
            f_unnamed_issue_citation_holds = "CITED_SOURCE_UNRESOLVED:100009002"
            f2_bare_numbers_are_not_citations = "True/True/4"
            g_stream_page_fail = "STREAM_UNREADABLE:issue_comments:89/HOLD_CLASS=TECHNICAL_HOLD"
            h_stream_truncated = "STREAM_TRUNCATED:issue_comments:1/HOLD_CLASS=TECHNICAL_HOLD"; ai_prompts = 0
        }
        $failed = @($expected.Keys | Where-Object { "$($global:FxChecks[$_])" -ne "$($expected[$_])" })
        $global:FxChecks.expect = if ($failed.Count -eq 0) { "PASS" } else { "FAIL:" + ($failed -join ",") }
        Write-Host "FX_EXPECT=$($global:FxChecks.expect)"
    }
    if ($Scenario -eq "authority-unit") {
        $skipOrch = $true
        . (Join-Path $hostDir "agent-host-authority-v2.ps1")
        $pos = [ordered]@{
            exact = "[ARCHITECT-INSTRUCTION]"
            form_5870526033 = "[ARCHITECT-INSTRUCTION]`n`nPR #146 — Agent Host protocol trial on the current candidate.`n"
            surrounding_ws_and_trailing_cr = "  `t[EVIDENCE-PACKET] `r`nbody`r`n"
            leading_blank_lines = "`n  `n`r`n[OWNER-AMENDMENT]`nbody"
        }
        $neg = [ordered]@{
            later_line = "intro`n[ARCHITECT-INSTRUCTION]`n"
            prose = "The host reads [ARCHITECT-INSTRUCTION] sources.`n"
            quote = "> [ARCHITECT-INSTRUCTION]`nx"
            inline_code = "``[ARCHITECT-INSTRUCTION]``"
            code_block = "``````text`n[ARCHITECT-INSTRUCTION]`n``````"
            followed_by_text = "[ARCHITECT-INSTRUCTION] see below"
            other_case = "[architect-instruction]"
            other_token = "[ARCHITECT-INSTRUCTIONS]"
            empty = ""
        }
        $global:FxChecks.grammar_positive = (@($pos.Keys | ForEach-Object { "$_=$(Get-AuthorityMarker ($pos[$_].Replace("`r`n", "`n")))" }) -join ', ')
        $global:FxChecks.grammar_positive_all_marked = (@($pos.Keys | Where-Object { -not (Get-AuthorityMarker ($pos[$_].Replace("`r`n", "`n"))) }).Count -eq 0)
        $global:FxChecks.grammar_negative_all_unmarked = (@($neg.Keys | Where-Object { Get-AuthorityMarker $neg[$_] }).Count -eq 0)
        $global:FxChecks.grammar_negative = (@($neg.Keys | ForEach-Object { "$_=$([bool](Get-AuthorityMarker $neg[$_]))" }) -join ', ')
        # authority write guard: refused before the GitHub write (no request), local draft allowed
        $draft = Join-Path $fx "draft-marked-body.md"
        [System.IO.File]::WriteAllText($draft, "`n[OWNER-AMENDMENT]`nprepared by automation as a DRAFT for the owner`n")
        $unmarked = Join-Path $fx "draft-unmarked.md"
        [System.IO.File]::WriteAllText($unmarked, "status: host mentions [ARCHITECT-INSTRUCTION] in prose`n")
        $jsonIn = Join-Path $fx "review.json"
        [System.IO.File]::WriteAllText($jsonIn, '{"body":"[EVIDENCE-PACKET]\nreview body","event":"COMMENT"}')
        $cases = [ordered]@{
            pr_comment_new_marked = @("pr", "comment", "1", "--repo", "fixture/icbm", "--body", "[ARCHITECT-INSTRUCTION]`nnew")
            issue_comment_body_file_marked = @("issue", "comment", "1", "--repo", "fixture/icbm", "--body-file", $draft)
            api_patch_edit_unmarked_to_marked = @("api", "-X", "PATCH", "repos/fixture/icbm/issues/comments/100009050", "-f", "body=[ARCHITECT-INSTRUCTION]`nedited")
            pr_edit_body_marked = @("pr", "edit", "1", "--repo", "fixture/icbm", "--body-file", $draft)
            pr_create_marked = @("pr", "create", "--repo", "fixture/icbm", "--base", "main", "--head", "x", "--title", "t", "--body-file", $draft)
            api_review_field_file = @("api", "-X", "POST", "repos/fixture/icbm/pulls/1/reviews", "-F", "body=@$draft")
            api_input_json = @("api", "-X", "POST", "repos/fixture/icbm/pulls/1/reviews", "--input", $jsonIn)
            pr_review_comment_marked = @("pr", "review", "1", "--comment", "--body", "  [EVIDENCE-PACKET]`r`nx")
            body_file_missing = @("pr", "comment", "1", "--body-file", (Join-Path $fx "no-such-file.md"))
        }
        $allowed = [ordered]@{
            pr_comment_unmarked = @("pr", "comment", "1", "--repo", "fixture/icbm", "--body-file", $unmarked)
            api_patch_unmarked = @("api", "-X", "PATCH", "repos/fixture/icbm/issues/comments/100009050", "-f", "body=plain status update")
            pr_ready_no_body = @("pr", "ready", "1", "--repo", "fixture/icbm")
        }
        $refusedOk = New-Object System.Collections.Generic.List[string]
        foreach ($k in $cases.Keys) {
            $callsBefore = $global:FxCalls.Count
            $w = Invoke-GhWrite -GhArgs $cases[$k] -Site "fixture:$k"
            $sent = @($global:FxCalls | Select-Object -Skip $callsBefore | Where-Object { $_ -like "gh *" }).Count
            $refusedOk.Add("$k=refused:$($w.Refused)/requests_sent:$sent/$($w.Reason)")
        }
        $allowedOk = New-Object System.Collections.Generic.List[string]
        foreach ($k in $allowed.Keys) {
            $callsBefore = $global:FxCalls.Count
            $w = Invoke-GhWrite -GhArgs $allowed[$k] -Site "fixture:$k"
            $sent = @($global:FxCalls | Select-Object -Skip $callsBefore | Where-Object { $_ -like "gh *" }).Count
            $allowedOk.Add("$k=refused:$($w.Refused)/requests_sent:$sent")
        }
        $global:FxChecks.guard_refused = $refusedOk -join ' | '
        $global:FxChecks.guard_all_refused_zero_requests = (@($refusedOk | Where-Object { $_ -notmatch 'refused:True/requests_sent:0/' }).Count -eq 0)
        $global:FxChecks.guard_allowed = $allowedOk -join ' | '
        $global:FxChecks.guard_allowed_sent = (@($allowedOk | Where-Object { $_ -notmatch 'refused:False/requests_sent:1$' }).Count -eq 0)
        $global:FxChecks.local_draft_written = (Test-Path $draft) -and ([System.IO.File]::ReadAllText($draft).Contains("[OWNER-AMENDMENT]"))
        # hold taxonomy (§5.1): the class comes from the category, and only the closed list is the user's
        $human = @("GPT_HUMAN_DECISION_REQUIRED_NEW_PRODUCT_FEATURE", "CLAUDE_HUMAN_DECISION_REQUIRED_COST", "NEXT_HOLD_NEW_PRODUCT_FEATURE", "NEXT_HOLD_PRODUCT_DIRECTION_UNDECIDED", "NEXT_HOLD_BEYOND_USER_REQUIREMENT", "NEXT_HOLD_LIVE", "NEXT_HOLD_PROVIDER_CALL", "NEXT_HOLD_CANARY", "NEXT_HOLD_REAL_EXTERNAL_READ", "NEXT_HOLD_RESIDUAL_RISK_APPROVAL", "NEXT_HOLD_COST", "NEXT_HOLD_EXTERNAL_DATA_TRANSFER", "NEXT_HOLD_DESTRUCTIVE", "PR_ON_OWNER_HOLD", "GUARD_PR_ON_OWNER_HOLD", "REPAIR_FIXER_HUMAN_DECISION_REQUIRED_LIVE")
        $technical = @("GPT_HUMAN_DECISION_REQUIRED", "HUMAN_DECISION_REQUIRED", "CLAUDE_HUMAN_DECISION_WITHOUT_CATEGORY", "REPAIR_FIXER_DECLINED_WITHOUT_A_HUMAN_CATEGORY", "AUDIT_BLOCKED_CITED_SOURCE_UNRESOLVED:100009998", "AUDIT_BLOCKED_UNCLASSIFIED_MARKED_SOURCE:1", "AUDIT_BLOCKED_STREAM_UNREADABLE:issue_comments:1", "GPT_INSUFFICIENT", "GPT_HOLD", "CLAUDE_HOLD", "REPAIR_MAX_REPAIR_CYCLES", "REPAIR_SCOPE_EXPANSION_REQUIRED", "REPAIR_NEW_SCHEMA_OR_MIGRATION_REQUIRED", "REPAIR_MAIN_MOVED_DURING_FIX", "NEXT_HOLD_SEPARATE_AUTHORIZATION_REQUIRED", "NEXT_HOLD_ARCHITECTURE_OR_POLICY", "NEXT_HOLD_NEXT_UNCLEAR", "NEXT_HOLD_ROADMAP_ADR_CONFLICT", "NEXT_HOLD_USER_JUDGMENT", "CI_FAILED", "BASE_SYNC_FAILED", "GUARD_NOT_MERGEABLE_CONFLICTING", "POST_MERGE_TREE_MISMATCH", "REPAIR_FIXER_DECLINED_LEGACY_HUMAN_HOLD", "DELIVERY_FAILED", "OLIVE_BRANCH", "MAX_WAIT_TIME_REACHED", "")
        $global:FxChecks.taxonomy_human_all = (@($human | Where-Object { (Get-HoldClass $_) -ne "HUMAN_DECISION_REQUIRED" }) -join ',')
        $global:FxChecks.taxonomy_technical_all = (@($technical | Where-Object { (Get-HoldClass $_) -ne "TECHNICAL_HOLD" }) -join ',')
        # citation grammar: "Issue #n" and bare 9-12 digit ids; hashes, short numbers, paths and PR numbers are not ids
        $refs = Get-EvidenceReferences "Authority (Issue #126, Issue #89): ``5906290729``, ``5907009512`` and bare 5909188774. PR #160, issue #7 lower-case, run 36699770595, ``36699770595x``, ``12345678``, ``1234567890123``, ``bb9906ccd61c``, ````5906712259````."
        $global:FxChecks.citation_issues = $refs.Issues -join ','
        $global:FxChecks.citation_ids = $refs.Ids -join ','
        $global:FxChecks.category_at_start = (@("NEW_PRODUCT_FEATURE: x", "[LIVE] y", "COST", "  PROVIDER_CALL - z") | ForEach-Object { Get-HumanDecisionCategory $_ }) -join ','
        $global:FxChecks.category_absent = (@("the diff needs a decision", "maybe LIVE later", "OWNER_HOLD: not an auditor's", "new_product_feature: lower", "") | ForEach-Object { "[$(Get-HumanDecisionCategory $_)]" }) -join ''
        $expected = [ordered]@{
            grammar_positive_all_marked = $true; grammar_negative_all_unmarked = $true; guard_all_refused_zero_requests = $true
            guard_allowed_sent = $true; local_draft_written = $true; taxonomy_human_all = ""; taxonomy_technical_all = ""
            citation_issues = "89,126"; citation_ids = "5906290729,5907009512"
            category_at_start = "NEW_PRODUCT_FEATURE,LIVE,COST,PROVIDER_CALL"; category_absent = "[][][][][]"
        }
        $failed = @($expected.Keys | Where-Object { "$($global:FxChecks[$_])" -ne "$($expected[$_])" })
        $global:FxChecks.expect = if ($failed.Count -eq 0) { "PASS" } else { "FAIL:" + ($failed -join ",") }
        Write-Host "FX_EXPECT=$($global:FxChecks.expect)"
    }
    if ($Scenario -eq "packet-blocker-no-cache") {
        $b1 = Fx-RunAudit @{ PrNumber = 1; SkipCiGate = $true }
        $gptCacheFiles = @(Get-ChildItem (Join-Path $hostDir "state") -Filter "*-pkt-*-gpt.txt")
        $gptResultFiles = @(Get-ChildItem (Join-Path $hostDir "state") -Filter "*-pkt-*-gpt.result.txt")
        $global:FxChecks.run1_gpt_verdict = Fx-Line $b1 "GPT_VERDICT"
        $global:FxChecks.run1_gpt_cache_files = $gptCacheFiles.Count
        $global:FxChecks.run1_result_verdict = if ($gptResultFiles.Count) { ([regex]::Match((Get-Content $gptResultFiles[0].FullName -Raw), '(?m)^VERDICT=\S+')).Value } else { "" }
        $b2 = Fx-RunAudit @{ PrNumber = 1; SkipCiGate = $true }
        $global:FxChecks.run2_gpt_cache = (@($b2 | Where-Object { $_ -like "GPT_CACHE=*" }) -join ";")
        $global:FxChecks.run2_audit_result = Fx-Line $b2 "AUDIT_RESULT"
        $global:FxChecks.run2_same_digest = ((Fx-Line $b1 "PACKET_DIGEST") -eq (Fx-Line $b2 "PACKET_DIGEST"))
        $global:FxChecks.gpt_prompts_after_run2 = @(Get-ChildItem $promptDir -Filter "*gpt-pr*").Count
    }
    if ($Scenario -eq "packet-legacy-cache") {
        # digestless legacy PASS (v6), draft-V2 v7 PASS with a digest, and a v8 PASS under a foreign digest must NOT satisfy V2 cache / DUAL PASS
        $h = Get-FxHead 1; $m = Get-FxMain
        $ev = "EVIDENCE_SEEN=NONE"
        foreach ($name in @(
            "packet-v6-sol-high-pr-1-main-$($m.Substring(0,12))-head-$($h.Substring(0,12))",
            "packet-v7-sol-high-pr-1-main-$($m.Substring(0,12))-head-$($h.Substring(0,12))-pkt-000000000000",
            "packet-v8-sol-high-pr-1-main-$($m.Substring(0,12))-head-$($h.Substring(0,12))",
            "packet-v8-sol-high-pr-1-main-$($m.Substring(0,12))-head-$($h.Substring(0,12))-pkt-000000000000")) {
            foreach ($aud in "gpt", "claude") {
                $pd = if ($name -like "*-pkt-*") { "PACKET_DIGEST=$('0' * 64)`n" } else { "" }
                [System.IO.File]::WriteAllText((Join-Path $hostDir "state\$name-$aud.txt"), "AUDIT_HEAD=$h`nVERDICT=PASS`nSUMMARY=legacy pass`n$pd$ev`n")
            }
        }
    }
    if ($Scenario -like "i2-*") {
        $skipOrch = $true
        $newMain = Get-FxMain
        $specPath = Join-Path $hostDir "state\next-slice-main-$($newMain.Substring(0,12)).json"
        [ordered]@{ main = $newMain; slice_id = "fixture-feature"; slice_title = "Fixture feature doc and test"; canonical_sources = "ROADMAP.md fixture"; allowed_paths = @("docs/feature.md", "tests/"); schema_change = "NONE"; acceptance = "feature doc exists"; forbidden = "provider calls"; spec = "Add docs/feature.md and tests/test_feature.py." } | ConvertTo-Json | Set-Content -Encoding utf8 $specPath
        function Fx-Repair { param([hashtable]$P) $o = @(& (Join-Path $hostDir "run-repair-v1.1.ps1") @P *>&1 | ForEach-Object { "$_" }); $o | ForEach-Object { Write-Host "  [repair] $_" }; return ,$o }
        function Fx-Hold { param($Out) return (@($Out | Where-Object { $_ -match '^(REPAIR_HOLD|NEXT_PR|REMEDIATION_PR)=' }) -join ';') }
        if ($Scenario -eq "i2-dup") {
            $p1 = $global:FxPrs["1"]
            $global:FxChecks.v1_branch_and_body_older_main = Fx-Hold (Fx-Repair @{ ImplementNext = $true })
            $global:FxChecks.v1_implementer_prompts = @(Get-ChildItem $promptDir -Filter "*implementer*").Count
            $p1.headRefName = "feat/renamed-branch"
            $global:FxChecks.v2_body_only = Fx-Hold (Fx-Repair @{ ImplementNext = $true })
            $p1.body = "unrelated body"
            @{ main = $global:FxOldMain; branch = "feat/renamed-branch"; slice_id = "fixture-feature"; pr = 1 } | ConvertTo-Json | Set-Content -Encoding utf8 (Join-Path $hostDir "state\next-main-$($global:FxOldMain.Substring(0,12)).json")
            $global:FxChecks.v3_registry_only = Fx-Hold (Fx-Repair @{ ImplementNext = $true })
            Remove-Item (Join-Path $hostDir "state\next-main-$($global:FxOldMain.Substring(0,12)).json") -Force
            $p1.headRefName = $global:FxOldBranch; $p1.body = "**Slice:** fixture-feature — Fixture feature doc and test"
            $p1.base = "release"
            $global:FxChecks.v4_same_slice_other_base = Fx-Hold (Fx-Repair @{ ImplementNext = $true })
            $p1.base = "main"
            $global:FxOpenPrListFail = $true
            $global:FxChecks.v5_open_pr_list_unreadable = Fx-Hold (Fx-Repair @{ ImplementNext = $true })
            $global:FxOpenPrListFail = $false
            $global:FxChecks.pr_creates_before_superseded = @($global:FxCalls | Where-Object { $_ -like "PR_CREATE*" }).Count
            $p1.labels = @("superseded")
            $global:FxChecks.v6_superseded_label_still_active = Fx-Hold (Fx-Repair @{ ImplementNext = $true })
            $global:FxChecks.pr_creates_after_label = @($global:FxCalls | Where-Object { $_ -like "PR_CREATE*" }).Count
            $p1.labels = @(); $p1.state = "CLOSED"
            $global:FxChecks.v7_closed_same_slice_inactive_proceeds = Fx-Hold (Fx-Repair @{ ImplementNext = $true })
            $global:FxChecks.pr_creates_total = @($global:FxCalls | Where-Object { $_ -like "PR_CREATE*" }).Count
        }
        if ($Scenario -eq "i2-dup-legacy") {
            # same fixture against the frozen pre-alignment host: shows the real #142 → #146 duplicate
            $global:FxChecks.v1_branch_and_body_older_main = Fx-Hold (Fx-Repair @{ ImplementNext = $true })
            $global:FxChecks.pr_creates_total = @($global:FxCalls | Where-Object { $_ -like "PR_CREATE*" }).Count
        }
        if ($Scenario -eq "i2-remediation") {
            [ordered]@{ main = $newMain; status = "BLOCKED"; policy = "full-audit-fx"; mode = "FULL" } | ConvertTo-Json | Set-Content -Encoding utf8 (Join-Path $hostDir "state\full-audit-state.json")
            $global:FxChecks.remediation_open_on_older_main = Fx-Hold (Fx-Repair @{ RemediateMain = $true })
            $global:FxChecks.pr_creates_total = @($global:FxCalls | Where-Object { $_ -like "PR_CREATE*" }).Count
        }
    }
    if ($skipOrch) {
        Write-Host "HARNESS_ORCHESTRATOR=SKIPPED"
    }
    elseif ($Scenario -like "*remediation*") {
        & (Join-Path $hostDir "resume-orchestrator-v1.3.ps1") -PollSeconds 1 -MaxWaitMinutes 10
    }
    else {
        & (Join-Path $hostDir "orchestrator-v1.3.ps1") -CurrentPr $startPr -PollSeconds 1 -MaxWaitMinutes 10
    }
}
catch {
    Write-Host "HARNESS_EXCEPTION=$($_.Exception.Message) @ $($_.InvocationInfo.PositionMessage)"; Write-Host "HARNESS_STACK=$($_.ScriptStackTrace)"
}

if ($Scenario -eq "auto-next-hold") {
    $global:FxPromptsBeforeResume = @(Get-ChildItem $promptDir).Count
    try {
        & (Join-Path $hostDir "resume-orchestrator-v1.3.ps1") -PollSeconds 1 -MaxWaitMinutes 10
    }
    catch {
        Write-Host "HARNESS_EXCEPTION2=$($_.Exception.Message)"
    }
    Write-Host "HARNESS_PROMPTS_BEFORE_RESUME=$global:FxPromptsBeforeResume AFTER=$(@(Get-ChildItem $promptDir).Count)"
}

if ($Scenario -like "two-merges*") {
    $bp = Join-Path $hostDir "state\full-audit-baseline.json"
    Write-Host "HARNESS_BASELINE_AFTER_RUN1=$(Get-Content $bp -Raw)"
    if ($Scenario -eq "two-merges-periodic") {
        $bj = Get-Content $bp -Raw -Encoding utf8 | ConvertFrom-Json
        $bj.last_full_at = (Get-Date).AddDays(-10).ToString("o")
        $bj | ConvertTo-Json | Set-Content -Encoding utf8 $bp
    }
    try {
        & (Join-Path $hostDir "orchestrator-v1.3.ps1") -CurrentPr 2 -PollSeconds 1 -MaxWaitMinutes 10
    }
    catch {
        Write-Host "HARNESS_EXCEPTION2=$($_.Exception.Message)"
    }
}
Stop-Transcript | Out-Null

$after = Get-UserSnapshot
if ($Scenario -eq "packet-blocker-no-cache") { $global:FxChecks.gpt_prompts_after_orchestrator = @(Get-ChildItem $promptDir -Filter "*gpt-pr*").Count }
$runtimePath = Join-Path $hostDir "state\orchestrator-runtime.json"
$runtime = if (Test-Path $runtimePath) { Get-Content $runtimePath -Raw -Encoding utf8 | ConvertFrom-Json } else { [pscustomobject]@{ status = "NONE"; action = "NONE"; current_pr = 0; auto_merge = $null; detail = "" } }
$stDir = Join-Path $hostDir "state"

# Pinned expectations (protocol §9 regression scenarios). A scenario listed here must end exactly like this.
#   status / action : the runtime state the run ended in          merged : merged PRs
#   human           : whether the run ended waiting for the user   gpt    : GPT exact-head audit prompts
#   fixer / created / sync / merge_calls : counts of those calls   lines  : transcript lines that must exist
$orchestratorExpect = @{
    "automerge-success"       = @{ status = "COMPLETE"; action = "ROADMAP_COMPLETE"; merged = 1; human = $false; gpt = 1; merge_calls = 1; lines = @("POST_MERGE_VERIFY=PASS", "MERGE_GUARD=MERGE (ALL_CHECKS_PASSED)", "GUARD_BEHIND_BY=0") }
    "gpt-loop"                = @{ status = "COMPLETE"; merged = 1; human = $false; gpt = 2; fixer = 1; merge_calls = 1 }
    "claude-loop"             = @{ status = "COMPLETE"; merged = 1; human = $false; gpt = 2; fixer = 1; merge_calls = 1 }
    "max-cycles"              = @{ status = "COMPLETE"; merged = 1; human = $false; fixer = 4; merge_calls = 1; lines = @("REPAIR_MODE=INDEPENDENT_REANALYSIS") }
    "auto-next"               = @{ status = "COMPLETE"; action = "ROADMAP_COMPLETE"; merged = 2; human = $false; created = 1; merge_calls = 2 }
    "auto-next-track"         = @{ status = "COMPLETE"; merged = 2; human = $false; created = 1 }
    "auto-next-human"         = @{ status = "HUMAN_DECISION_REQUIRED"; action = "NEXT_HOLD_NEW_PRODUCT_FEATURE"; merged = 1; human = $true; created = 0 }
    "auto-next-live"          = @{ status = "HUMAN_DECISION_REQUIRED"; action = "NEXT_HOLD_LIVE"; merged = 1; human = $true; created = 0 }
    "auto-next-hold"          = @{ status = "TECHNICAL_HOLD_EXHAUSTED"; action = "NEXT_HOLD_SEPARATE_AUTHORIZATION_REQUIRED"; merged = 1; human = $false; created = 0 }
    "gpt-human"               = @{ status = "HUMAN_DECISION_REQUIRED"; action = "GPT_HUMAN_DECISION_REQUIRED_NEW_PRODUCT_FEATURE"; merged = 0; human = $true; fixer = 0; merge_calls = 0 }
    "gpt-human-uncategorised" = @{ status = "TECHNICAL_HOLD_EXHAUSTED"; action = "GPT_HOLD"; merged = 0; human = $false; fixer = 0; merge_calls = 0 }
    "fixer-human"             = @{ status = "HUMAN_DECISION_REQUIRED"; action = "REPAIR_FIXER_HUMAN_DECISION_REQUIRED_PRODUCT_DIRECTION_UNDECIDED"; merged = 0; human = $true; merge_calls = 0 }
    "claude-blocker-hold"     = @{ status = "TECHNICAL_HOLD_EXHAUSTED"; action = "REPAIR_FIXER_DECLINED_LEGACY_HUMAN_HOLD"; merged = 0; human = $false; merge_calls = 0 }
    "gpt-insufficient"        = @{ status = "TECHNICAL_HOLD_EXHAUSTED"; action = "GPT_INSUFFICIENT"; merged = 0; human = $false; gpt = 3; merge_calls = 0 }
    "owner-hold"              = @{ status = "HUMAN_DECISION_REQUIRED"; action = "PR_ON_OWNER_HOLD"; merged = 0; human = $true; gpt = 0; merge_calls = 0 }
    "guard-head-moved"        = @{ status = "COMPLETE"; merged = 1; human = $false; gpt = 2; merge_calls = 1; lines = @("MERGE_GUARD=RELOOP (HEAD_MOVED_AFTER_AUDIT)") }
    "guard-main-moved"        = @{ status = "COMPLETE"; merged = 1; human = $false; gpt = 2; sync = 1; merge_calls = 1; lines = @("MERGE_GUARD=RELOOP (MAIN_MOVED_AFTER_AUDIT)") }
    "behind-base"             = @{ status = "COMPLETE"; merged = 1; human = $false; gpt = 1; sync = 1; merge_calls = 1; lines = @("BASE_SYNC=PR_HEAD_BEHIND_MAIN_BY_1") }
    "merge-sha-mismatch"      = @{ status = "COMPLETE"; merged = 1; human = $false; gpt = 2; merge_calls = 2 }
    "packet-unclassified"     = @{ status = "COMPLETE"; merged = 1; human = $false; gpt = 1; merge_calls = 1 }
    "packet-no-record"        = @{ status = "COMPLETE"; merged = 1; human = $false; gpt = 1; merge_calls = 1 }
    "packet-edit-to-marker"   = @{ status = "COMPLETE"; merged = 1; human = $false; gpt = 1; merge_calls = 1 }
    "packet-record-edit-guard" = @{ status = "COMPLETE"; merged = 1; human = $false; gpt = 1; merge_calls = 1 }
    "packet-clsedit"          = @{ status = "COMPLETE"; merged = 1; human = $false; gpt = 2; merge_calls = 1; lines = @("MERGE_GUARD=RELOOP (PACKET_DIGEST_CHANGED_AFTER_AUDIT)") }
    "packet-cite-added"       = @{ status = "COMPLETE"; merged = 1; human = $false; gpt = 2; merge_calls = 1; lines = @("MERGE_GUARD=RELOOP (PACKET_DIGEST_CHANGED_AFTER_AUDIT)") }
    "packet-stream-page-fail" = @{ status = "TECHNICAL_HOLD_EXHAUSTED"; action = "AUDIT_BLOCKED_STREAM_UNREADABLE:issue_comments:89"; merged = 0; human = $false; gpt = 0; merge_calls = 0 }
    "post-merge-remediation"  = @{ status = "COMPLETE"; human = $false; created = 1 }
    "ci-flaky"                = @{ status = "COMPLETE"; merged = 1; human = $false; merge_calls = 1 }
    "ci-hard-fail"            = @{ status = "TECHNICAL_HOLD_EXHAUSTED"; action = "CI_FAILED"; merged = 0; human = $false; merge_calls = 0 }
    "guard-ci-pending"        = @{ status = "COMPLETE"; merged = 1; human = $false; merge_calls = 1 }
    "unmergeable"             = @{ status = "TECHNICAL_HOLD_EXHAUSTED"; action = "GUARD_NOT_MERGEABLE_CONFLICTING"; merged = 0; human = $false; merge_calls = 0 }
    "post-merge-tree-mismatch" = @{ status = "TECHNICAL_HOLD_EXHAUSTED"; action = "POST_MERGE_TREE_MISMATCH"; human = $false; lines = @("POST_MERGE_VERIFY=FAIL") }
    "scope-expansion"         = @{ status = "COMPLETE"; merged = 1; human = $false; fixer = 1; merge_calls = 1; lines = @("SCOPE_WIDENED=app/unrelated.py") }
    "remediation-migration-hold" = @{ status = "COMPLETE"; human = $false; created = 1; lines = @("SCHEMA_OR_MIGRATION=app/db/migrations/versions/0099_fix.py") }
    "big-pr"                  = @{ status = "COMPLETE"; merged = 1; human = $false; merge_calls = 1 }
    "draft-pr"                = @{ status = "COMPLETE"; merged = 1; human = $false; merge_calls = 1 }
    "two-merges-adr"          = @{ status = "COMPLETE"; merged = 2; human = $false }
    "packet-legacy-cache"     = @{ status = "COMPLETE"; merged = 1; human = $false; gpt = 1; merge_calls = 1 }
    "packet-blocker-no-cache" = @{ status = "COMPLETE"; merged = 1; human = $false; merge_calls = 1 }
    "auto-next-scope"         = @{ status = "COMPLETE"; merged = 2; human = $false; created = 1; lines = @("SCOPE_WIDENED=app/unrelated.py") }
    "config-no-automerge"     = @{ status = "WAITING_FOR_MERGE_BY_CONFIG"; action = "DUAL_PASS_COMPLETE"; merged = 0; human = $false; merge_calls = 0 }
    "config-no-autonext"      = @{ status = "IDLE"; action = "AUTO_NEXT_DISABLED_BY_CONFIG"; merged = 1; human = $false; created = 0 }
}

if ($orchestratorExpect.ContainsKey($Scenario)) {
    $e = $orchestratorExpect[$Scenario]
    $logText = Get-Content $transcript -Raw
    $actual = @{
        status = "$($runtime.status)"
        action = "$($runtime.action)"
        merged = @($global:FxPrs.Values | Where-Object { $_.state -eq "MERGED" }).Count
        human = ("$($runtime.status)" -eq "HUMAN_DECISION_REQUIRED")
        gpt = @(Get-ChildItem $promptDir -Filter "*-gpt-pr.txt").Count
        fixer = @(Get-ChildItem $promptDir -Filter "*-fixer-pr.txt").Count
        created = @($global:FxCalls | Where-Object { $_ -like "PR_CREATE*" }).Count
        sync = @($global:FxCalls | Where-Object { $_ -like "UPDATE_BRANCH*" }).Count
        merge_calls = @($global:FxCalls | Where-Object { $_ -like "MERGE_CALL*" }).Count
    }
    $failed = New-Object System.Collections.Generic.List[string]
    foreach ($k in $e.Keys) {
        if ($k -eq "lines") {
            foreach ($l in $e.lines) { if (-not $logText.Contains($l)) { $failed.Add("line:$l") } }
        }
        elseif ("$($actual[$k])" -ne "$($e[$k])") { $failed.Add("$k=$($actual[$k])(want $($e[$k]))") }
    }
    # invariants of every control-loop scenario: no forbidden GitHub write, the user's repository untouched,
    # and a run never waits for the user unless the scenario says so
    if (@($global:FxCalls | Where-Object { $_ -like "FORBIDDEN*" -or $_ -like "UNHANDLED*" }).Count -gt 0) { $failed.Add("forbidden_or_unhandled_gh_call") }
    if (($before | ConvertTo-Json) -ne ($after | ConvertTo-Json)) { $failed.Add("user_repo_changed") }
    if ($logText -match '(?m)^HUMAN_HOLD=') { $failed.Add("legacy_HUMAN_HOLD_line") }
    $global:FxChecks.expect = if ($failed.Count -eq 0) { "PASS" } else { "FAIL:" + ($failed -join ";") }
    Write-Host "FX_EXPECT=$($global:FxChecks.expect)"
}

$result = [ordered]@{
    scenario = $Scenario
    runtime_status = $runtime.status
    runtime_action = $runtime.action
    runtime_pr = $runtime.current_pr
    runtime_auto_merge = $runtime.auto_merge
    user_repo_unchanged = (($before | ConvertTo-Json) -eq ($after | ConvertTo-Json))
    user_branch = $after.Branch
    user_status = $after.Status
    origin_main_before = $originMainBefore
    origin_main_after = (Get-FxMain)
    prs = ($global:FxPrs.Values | ForEach-Object { "#$($_.number) $($_.headRefName) $($_.state) head=$((Get-FxHead $_.number).Substring(0,7))" }) -join "; "
    forbidden_calls = @($global:FxCalls | Where-Object { $_ -like "FORBIDDEN*" -or $_ -like "UNHANDLED*" })
    merge_calls = @($global:FxCalls | Where-Object { $_ -like "MERGE_CALL*" -or $_ -like "CI_RERUN*" })
    guard_hook_fired = @($global:FxCalls | Where-Object { $_ -eq "GUARD_HOOK_FIRED" }).Count
    merged_prs = @($global:FxPrs.Values | Where-Object { $_.state -eq "MERGED" } | ForEach-Object { "#$($_.number)->$($_.mergeCommit)" })
    full_audit_mains = @(Get-ChildItem $promptDir | Where-Object { $_.Name -match "gpt-(full|delta)" } | ForEach-Object { [regex]::Match((Get-Content $_.FullName -Raw), '(?m)^AUDIT_MAIN=([0-9a-f]{40})').Groups[1].Value })
    runtime_detail = $runtime.detail
    resume_prompt_delta = "$(Select-String -Path $transcript -Pattern '^HARNESS_PROMPTS_BEFORE_RESUME=.*' | Select-Object -Last 1 | ForEach-Object { $_.Line })"
    audit_modes = @(Select-String -Path $transcript -Pattern '^(AUDIT_MODE=\w+|FULL_AUDIT_REASON=.*|FULL_AUDIT_ESCALATION=.*|BASELINE_MAIN=.*|DELTA_COUNT_SINCE_FULL=.*)$' | ForEach-Object { $_.Line } | Select-Object -Unique)
    baseline_file = "$(if (Test-Path (Join-Path $hostDir 'state\full-audit-baseline.json')) { (Get-Content (Join-Path $hostDir 'state\full-audit-baseline.json') -Raw | ConvertFrom-Json | ForEach-Object { "main=$($_.main.Substring(0,7)) mode=$($_.mode) status=$($_.status) last_full=$("$($_.last_full_main)".Substring(0,7)) delta_count=$($_.delta_count_since_full)" }) })"
    fixer_push_exits = @($global:FxCalls | Where-Object { $_ -like "FIXER_PUSH_EXIT=*" })
    fixer_escape_branch = "$(& git --git-dir=$origin rev-parse --verify -q refs/heads/fixer-escape 2>$null)"
    prompts = @(Get-ChildItem $promptDir | ForEach-Object { $_.Name })
    repair_states = @(Get-ChildItem (Join-Path $hostDir "state") -Filter "repair-pr-*.json" | ForEach-Object { "$($_.Name): cycles=$((Get-Content $_.FullName -Raw | ConvertFrom-Json).cycles)" })
    host_worktrees = @(& git -C $user worktree list 2>$null)
    packet_pointers = @(Get-ChildItem (Join-Path $stDir "packets") -Filter "*.current" -ErrorAction SilentlyContinue | ForEach-Object { "$($_.Name): " + (@(Get-Content $_.FullName | Where-Object { $_ -match '^(PACKET_DIGEST|REQUIRED_SOURCES)=' }) -join ' ') })
    packet_files = @(Get-ChildItem (Join-Path $stDir "packets") -ErrorAction SilentlyContinue | ForEach-Object { $_.Name })
    verdict_files = @(Get-ChildItem $stDir -Filter "*-pkt-*" | Where-Object { $_.Name -notmatch '-call\d+\.txt$' } | ForEach-Object { "$($_.Name): " + (@(Select-String -Path $_.FullName -Pattern '^(VERDICT|PACKET_DIGEST|EVIDENCE_SEEN|EVIDENCE_COVERED)=' | ForEach-Object { $_.Line }) -join ' ') })
    hold_files = @(Get-ChildItem $stDir -Filter "*packet-hold*" | ForEach-Object { "$($_.Name): " + (@(Select-String -Path $_.FullName -Pattern '^(VERDICT|SUMMARY)=' | ForEach-Object { $_.Line }) -join ' ') })
    legacy_named_verdicts = @(Get-ChildItem $stDir -Filter "packet-v*-head-*" | Where-Object { $_.Name -notmatch '-pkt-' } | ForEach-Object { $_.Name })
    key_lines = @(Select-String -Path $transcript -Pattern '^(AUDIT_BLOCKED|AUDIT_HOLD|HOLD_CLASS|HUMAN_DECISION_REQUIRED|TECHNICAL_HOLD|SUPERVISOR|BASE_SYNC|POST_MERGE_VERIFY|MERGE_GUARD|GUARD_PACKET_DIGEST|GUARD_CURRENT_PACKET_DIGEST|AUDIT_CACHE|GPT_CACHE|CLAUDE_CACHE|PACKET_DIGEST|AUDIT_IDENTITY|GPT_EVIDENCE_SEEN|CLAUDE_EVIDENCE_SEEN|PACKET_HOLD_REASON)=' | ForEach-Object { $_.Line } | Select-Object -Unique)
    prompt_source_sections = @(Get-ChildItem $promptDir -Filter "*-pr.txt" | ForEach-Object { $pt = [System.IO.File]::ReadAllText($_.FullName); "$($_.Name):sections=$([regex]::Matches($pt, '(?m)^\[(ARCHITECT-INSTRUCTION|EVIDENCE-PACKET|OWNER-AMENDMENT) id=').Count):closers=$([regex]::Matches($pt, '(?m)^\[/(ARCHITECT-INSTRUCTION|EVIDENCE-PACKET|OWNER-AMENDMENT)\]').Count):call=$([regex]::Match($pt, '(?m)^AUDIT_CALL=(\S+)').Groups[1].Value)" })
    checks = $global:FxChecks
    log = $transcript
    fx = $fx
}

$result | ConvertTo-Json -Depth 5 | Set-Content -Encoding utf8 (Join-Path $fx "result.json")
$result | ConvertTo-Json -Depth 5
