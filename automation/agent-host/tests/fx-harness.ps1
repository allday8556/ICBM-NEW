param(
    [Parameter(Mandatory=$true)][string]$Scenario,
    [Parameter(Mandatory=$true)][string]$Root,
    [string]$SrcHost = "C:\Users\user\ICBM-Agent-Host"
)

# Fixture harness: local bare origin + dirty user clone on main + mocked gh/codex/claude.
# Real GitHub, real ICBM-NEW repo and real Agent Host state are never touched.

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
    { $_ -like "auto-next*" -or $_ -in @("ci-flaky", "ci-hard-fail", "automerge-success", "guard-head-moved", "guard-main-moved", "guard-ci-pending", "guard-ci-failed", "gpt-insufficient", "merge-sha-mismatch", "draft-pr", "unmergeable") } {
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
        New-PrBranch "feat/packet" { W "docs/contract.md" "# Contract`nrule: never resend CREATE`npacket clarified`n" }
        $n = Add-FxPr "feat/packet"
        if ($Scenario -eq "packet-guard-digest") { $global:FxPrs["$n"].isDraft = $true }
    }
    "claude-blocker-hold" {
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

$cfg = Get-Content (Join-Path $srcHost "state\orchestrator-config.json") -Raw -Encoding utf8 | ConvertFrom-Json
$cfg.repository = "fixture/icbm"
$cfg.repo_path = $user
$cfg | ConvertTo-Json -Depth 10 | Set-Content -Encoding utf8 (Join-Path $hostDir "state\orchestrator-config.json")

# ---------------- V2 packet sources (packet-* scenarios; canonical V2 at main a0643e4) ----------------
# Designated streams: issue #1 comments/body, PR #1 reviews/review comments, issue #89 comments.
# Classification comes ONLY from the marked [OWNER-AMENDMENT] record 9300 (preamble names PR #1).
function global:Fx-D { param([string]$Body) $t = $Body.Replace("`r`n", "`n").Replace("`r", "`n"); $s = [System.Security.Cryptography.SHA256]::Create(); return (([BitConverter]::ToString($s.ComputeHash((New-Object System.Text.UTF8Encoding($false)).GetBytes($t)))) -replace '-', '').ToLower() }
function global:Fx-C { param($Id, $Body, $Issue = 1, $UpdatedAt = "2026-09-28T00:00:00Z") $global:FxComments["$Id"] = @{ id = [int64]$Id; user = @{ login = "fixture" }; html_url = "https://github.com/fixture/icbm/issues/$Issue#issuecomment-$Id"; issue_url = "https://api.github.com/repos/fixture/icbm/issues/$Issue"; body = $Body; created_at = "2026-09-28T00:00:00Z"; updated_at = $UpdatedAt; submitted_at = $UpdatedAt } }
function global:Fx-Record {
    param([string]$Preamble = "Fixture classification record (designated streams: #1, #89)", [string]$Extra = "", [string]$ClassOf9004 = "evidence-only", [string]$ScopeLine = "scope: PR #1", [string]$GitLine = "")
    $blob = "$(& git --git-dir=$global:FxOrigin rev-parse "$(Get-FxHead 1):docs/contract.md" 2>$null)".Trim()
    $req = @(
        "- issue-comment 9001 sha256 $(Fx-D $global:FxComments['9001'].body) — architect instruction"
        "- issue-comment 9002 sha256 $(Fx-D $global:FxComments['9002'].body) — official evidence E1 (unmarked, on #89)"
        "- pr-review 1/9101 sha256 $(Fx-D $global:FxComments['9101'].body)"
        $(if ($GitLine) { $GitLine } else { "- git-blob HEAD:docs/contract.md $blob" })
    )
    $ev = @("- issue-comment 9004 sha256 $(Fx-D $global:FxComments['9004'].body) — background")
    $scope = if ($ScopeLine) { "$ScopeLine`n" } else { "" }
    $body = "[OWNER-AMENDMENT]`n`n$Preamble`n$scope`nrequired:`n$($req -join "`n")`n"
    if ($ClassOf9004 -eq "required") { $body += "$($ev -join "`n")`n" }
    $body += "`nevidence-only:`n"
    if ($ClassOf9004 -ne "required") { $body += "$($ev -join "`n")`n" }
    $body += "$Extra`nexcluded: none. Any other marked source → HOLD.`n"
    return $body
}

if ($Scenario -like "packet-*") {
    Fx-C 9001 "  [ARCHITECT-INSTRUCTION]  `r`n`r`nArchitect: keep the contract rule; clarify wording only.`r`n"
    Fx-C 9002 "Evidence E1 (unmarked; classified by record 9300): the contract rule is official.`n" 89
    Fx-C 9003 "status: the host reads ``[ARCHITECT-INSTRUCTION]`` sources (inline code, prose mention; not authority)`n"
    Fx-C 9004 "[EVIDENCE-PACKET]`nOptional background evidence.`n" 89
    Fx-C 9050 "old ordinary comment, created before any scan`n"
    Fx-C 9061 "> [ARCHITECT-INSTRUCTION]`nquoted, not a marker`n"
    Fx-C 9062 "``````text`n[ARCHITECT-INSTRUCTION]`n```````n"
    Fx-C 9063 "[ARCHITECT-INSTRUCTION] see below`n"
    Fx-C 9064 "[architect-instruction]`nother case`n"
    Fx-C 9065 "note first`n[EVIDENCE-PACKET]`ntoken on a later line`n"
    Fx-C 9101 "[EVIDENCE-PACKET]`nReview evidence: the diff matches E1.`n" 1
    Fx-C 9200 "ordinary discussion`n"
    $global:FxStreams["issue:1"] = @(9001, 9003, 9050, 9061, 9062, 9063, 9064, 9065, 9200, 9300)
    $global:FxStreams["issue:89"] = @(9002, 9004)
    $global:FxStreams["reviews:1"] = @(9101)
    $global:FxStreams["rc:1"] = @()
    $global:FxIssueBodies["1"] = "fixture PR body (unmarked)`n"
    Fx-C 9300 (Fx-Record)
    if ($Scenario -eq "packet-unclassified") { Fx-C 9005 "[OWNER-AMENDMENT]`nnew amendment nobody classified`n"; $global:FxStreams["issue:1"] = @($global:FxStreams["issue:1"]) + 9005 }
    if ($Scenario -eq "packet-source-missing") { Fx-C 9300 ((Fx-Record) + "") ; $global:FxComments["9300"].body = $global:FxComments["9300"].body.Replace("- pr-review 1/9101", "- issue-comment 9999 sha256 $('cd' * 32) — missing source`n- pr-review 1/9101") }
    if ($Scenario -eq "packet-stream-page-fail") { $global:FxStreamFail["issue:89"] = "page" }
    if ($Scenario -eq "packet-stream-perm") { $global:FxStreamFail["reviews:1"] = "perm" }
    if ($Scenario -eq "packet-stream-truncated") { $global:FxStreamFail["issue:1"] = "truncate" }

    $global:FxManifest = [ordered]@{
        pr = 1
        note = "designation only"
        designated_streams = @(
            [ordered]@{ type = "issue_comments"; issue = 1 }
            [ordered]@{ type = "pr_body"; pr = 1 }
            [ordered]@{ type = "pr_reviews"; pr = 1 }
            [ordered]@{ type = "pr_review_comments"; pr = 1 }
            [ordered]@{ type = "issue_comments"; issue = 89; watermark = 0 }
        )
    }
    $global:FxManifest | ConvertTo-Json -Depth 10 | Set-Content -Encoding utf8 (Join-Path $hostDir "state\audit-sources-pr-1.json")

    if ($Scenario -in @("packet-edit-to-marker", "packet-clsedit", "packet-record-edit-guard")) { $global:FxPrs["1"].isDraft = $true }
    if ($Scenario -eq "packet-edit-to-marker") {
        # DUAL PASS 후 ready 시점: 스캔 이전부터 있던 unmarked 9050 을 in-place 로 marker-first 로 편집 → §7.1 재생성이 HOLD 해야 한다
        $global:FxReadyHook = { $global:FxComments["9050"].body = "[ARCHITECT-INSTRUCTION]`nold comment edited in place to add a marker`n"; $global:FxComments["9050"].updated_at = "2026-09-29T00:00:00Z"; $global:FxCalls.Add("OLD_UNMARKED_EDITED_TO_MARKER 9050") }
    }
    if ($Scenario -eq "packet-clsedit") {
        $global:FxReadyHook = { $global:FxComments["9002"].body = "Evidence E1 EDITED after the audit.`n"; $global:FxCalls.Add("CLASSIFIED_SOURCE_EDITED 9002") }
    }
    if ($Scenario -eq "packet-record-edit-guard") {
        # architect 가 record 자체를 (유효하게) 편집 → digest 변경 → DUAL PASS 무효 → 새 identity 로 재감사 (§7.1)
        $global:FxReadyHook = { $global:FxComments["9300"].body = (Fx-Record -Preamble "PR #1 fixture classification record, revised by the architect"); $global:FxCalls.Add("RECORD_EDITED 9300") }
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
                if ("$($opt['--json'])" -like "*isDraft*" -and $global:FxGuardHook -and $p.state -eq "OPEN") {
                    # MERGE_GUARD 조회 시점에 한 번만 외부 변화 주입
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
            $wr = @(@{ id = 9001; check_suite_id = 9101; created_at = "2020-01-01T00:00:00Z"; status = "completed"; conclusion = $(if ($anyFail) { "failure" } else { "success" }) })
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
        $ids = @($ids | Where-Object { $_ -notlike "github_issue_comment:9001@*" })
    }
    if ($sc -eq "packet-evwrong" -and $Who -eq "gpt") {
        $ids = @($ids | ForEach-Object { if ($_ -like "github_issue_comment:9001@*") { "github_issue_comment:9001@$('ab' * 32)" } else { $_ } })
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
            if ($sc -eq "max-cycles") { return (Fx-Verdict "AUDIT_HEAD" $h "BLOCKER" "always blocked") }
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
            if ($sc -eq "scope-expansion") { Fx-Replace $cwd "app/unrelated.py" "return 0" "return 99"; return "FIXER_RESULT=READY`nFIXER_SUMMARY=touched unrelated" }
            if ($sc -eq "max-cycles") { Add-Content (Join-Path $cwd "docs/contract.md") "attempt $(Get-Random)"; return "FIXER_RESULT=READY`nFIXER_SUMMARY=attempt" }
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
                return "FIXER_RESULT=READY`nFIXER_SUMMARY=added migration"
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
        $global:FxChecks.digest_1 = $d1
        $global:FxChecks.same_inputs_twice_same_digest = ($d1 -and $d1 -eq $d2)
        $global:FxChecks.write_1_2 = "$(Fx-Line $r1 'PACKET_WRITE')/$(Fx-Line $r2 'PACKET_WRITE')"
        $global:FxChecks.packet_files_after_2_builds = $pk.Count
        $global:FxChecks.digest_is_sha256_of_packet_bytes = ((Fx-Sha $bytes) -eq $d1)
        $global:FxChecks.no_bom_no_cr = ($bytes[0] -ne 0xEF) -and (-not ($bytes -contains 13))
        $global:FxChecks.no_abs_path = (-not $ptext.Contains($hostDir)) -and (-not $ptext.Contains("C:\"))
        $global:FxChecks.no_watermark_or_updated_at_in_packet = (-not ($ptext -match 'watermark|updated_at|2026-09-28T00:00:00Z'))
        $global:FxChecks.source_identities = $idents -join ' '
        $global:FxChecks.each_section_once = (@($idents | Where-Object { ([regex]::Matches($ptext, [regex]::Escape("[SOURCE identity=$_ "))).Count -ne 1 -or ([regex]::Matches($ptext, [regex]::Escape("[/SOURCE identity=$_]"))).Count -ne 1 }).Count -eq 0) -and $idents.Count -eq 6
        $global:FxChecks.required_line = ([regex]::Match($ptext, '(?m)^REQUIRED_SOURCES=.*$')).Value
        $calls = @(Get-ChildItem (Join-Path $hostDir "logs") -Filter "pr-1-packet-call*-*.txt")
        $global:FxChecks.call_files_have_digest_and_all_sources = ($calls.Count -gt 0) -and (@($calls | Where-Object { $ct = [System.IO.File]::ReadAllText($_.FullName); -not $ct.Contains("PACKET_DIGEST=$d1") -or @($idents | Where-Object { -not $ct.Contains("[SOURCE identity=$_ ") }).Count -gt 0 }).Count -eq 0)
        $scan = Get-Content (Join-Path $pkDir "pr-1-head-$((Get-FxHead 1).Substring(0,12)).scan.json") -Raw | ConvertFrom-Json
        $global:FxChecks.scan_provenance = (@($scan.streams | ForEach-Object { "$($_.stream)#$($_.items)@wm$($_.watermark_max_id)" }) -join ',')
        $global:FxChecks.scan_has_updated_at = (@($scan.sources | Where-Object { $_.updated_at }).Count -gt 0)
        # provenance-only changes: updated_at of a source, stream order and watermark in the manifest → same digest
        $global:FxComments["9004"].updated_at = "2026-09-29T11:11:11Z"
        $m2 = $global:FxManifest; $m2.designated_streams = @($m2.designated_streams[4], $m2.designated_streams[3], $m2.designated_streams[2], $m2.designated_streams[1], $m2.designated_streams[0]); $m2.designated_streams[0].watermark = 99999
        $m2 | ConvertTo-Json -Depth 10 -Compress | Set-Content -Encoding utf8 $mfPath
        $r3 = Fx-RunAudit @{ PrNumber = 1; PacketOnly = $true }
        $global:FxChecks.provenance_only_changes_same_digest = ((Fx-Line $r3 "PACKET_DIGEST") -eq $d1)
        # immutability: tamper → PACKET_IMMUTABILITY_VIOLATION
        [System.IO.File]::WriteAllBytes($pk[0].FullName, [byte[]]($bytes + [byte]0x78))
        $r4 = Fx-RunAudit @{ PrNumber = 1; PacketOnly = $true }
        $global:FxChecks.tampered_rebuild = Fx-Line $r4 "AUDIT_BLOCKED"
        [System.IO.File]::WriteAllBytes($pk[0].FullName, $bytes)
        # classified body edited (record not updated) → mapping no longer matches → HOLD
        $orig9004 = $global:FxComments["9004"].body
        Fx-SetBody 9004 "[EVIDENCE-PACKET]`nOptional background evidence (EDITED).`n"
        $r5 = Fx-RunAudit @{ PrNumber = 1; PacketOnly = $true }
        $global:FxChecks.classified_body_edit_hold = Fx-Reason $r5
        # architect re-issues the record with the new body digest → clean packet with a NEW digest
        $origRecord = $global:FxComments["9300"].body
        Fx-SetBody 9300 (Fx-Record)
        $r6 = Fx-RunAudit @{ PrNumber = 1; PacketOnly = $true }
        $d6 = Fx-Line $r6 "PACKET_DIGEST"
        $global:FxChecks.reclassified_edit_new_digest = ((Fx-Line $r6 "PACKET_COMPLETE") -eq "True" -and $d6 -and $d6 -ne $d1)
        # the classification record's own body edited (still valid) → record digest and packet digest change, no HOLD
        Fx-SetBody 9300 (Fx-Record -Preamble "PR #1 fixture classification record (wording revised)")
        $r7 = Fx-RunAudit @{ PrNumber = 1; PacketOnly = $true }
        $d7 = Fx-Line $r7 "PACKET_DIGEST"
        $global:FxChecks.record_body_edit_new_digest = ((Fx-Line $r7 "PACKET_COMPLETE") -eq "True" -and $d7 -and $d7 -ne $d6 -and $d7 -ne $d1)
        # revert everything → original digest, byte-identical existing packet
        Fx-SetBody 9004 $orig9004; Fx-SetBody 9300 $origRecord
        $r8 = Fx-RunAudit @{ PrNumber = 1; PacketOnly = $true }
        $global:FxChecks.reverted_digest_equals_first = ((Fx-Line $r8 "PACKET_DIGEST") -eq $d1)
        $global:FxChecks.reverted_write = Fx-Line $r8 "PACKET_WRITE"
        # old UNMARKED comment 9050 (created before every earlier scan) edited in place to add a marker → next generation HOLD
        Fx-SetBody 9050 "[ARCHITECT-INSTRUCTION]`nold comment edited in place to add a marker`n"
        $r9 = Fx-RunAudit @{ PrNumber = 1; PacketOnly = $true }
        $global:FxChecks.old_unmarked_edited_to_marker = Fx-Reason $r9
        $global:FxChecks.no_current_pointer_after_hold = (-not (Test-Path (Join-Path $pkDir "pr-1-head-$((Get-FxHead 1).Substring(0,12)).current")))
        Fx-SetBody 9050 "old ordinary comment, created before any scan`n"
        $global:FxChecks.packet_files_final = @(Get-ChildItem $pkDir -Filter "*.packet.txt").Count
        $pm = Get-Content (Get-ChildItem $pkDir -Filter "*$($d1.Substring(0,12)).manifest.json")[0].FullName -Raw | ConvertFrom-Json
        $global:FxChecks.manifest_json_sources = @($pm.sources | ForEach-Object { "$($_.class):$($_.kind):$($_.origin)" }) -join ","
        $global:FxChecks.manifest_json_required_count = @($pm.required).Count
        $global:FxChecks.ai_prompts = @(Get-ChildItem $promptDir).Count
    }
    if ($Scenario -eq "packet-authority") {
        $skipOrch = $true
        $origRecord = $global:FxComments["9300"].body
        function Fx-Try { param([string]$Body) Fx-SetBody 9300 $Body; $o = Fx-RunAudit @{ PrNumber = 1; PacketOnly = $true }; Fx-SetBody 9300 $origRecord; $r = Fx-Reason $o; if (-not $r) { $r = "COMPLETE=$(Fx-Line $o 'PACKET_COMPLETE') digest=$((Fx-Line $o 'PACKET_DIGEST').Substring(0, [Math]::Min(12, (Fx-Line $o 'PACKET_DIGEST').Length)))" }; return $r }
        $headSha = Get-FxHead 1
        $blob = "$(& git --git-dir=$global:FxOrigin rev-parse "$($headSha):docs/contract.md" 2>$null)".Trim()
        $base = Fx-RunAudit @{ PrNumber = 1; PacketOnly = $true }
        $global:FxChecks.a0_clean_with_grammar_negatives_in_stream = Fx-Line $base "PACKET_COMPLETE"
        # a) host manifest may not classify
        '{"pr":1,"designated_streams":[{"type":"issue_comments","issue":1}],"sources":[{"id":"x","kind":"EVIDENCE-PACKET","locator":{"type":"github_issue_comment","id":9005},"required":true}]}' | Set-Content -Encoding utf8 $mfPath
        $global:FxChecks.a_host_manifest_classifies = Fx-Reason (Fx-RunAudit @{ PrNumber = 1; PacketOnly = $true })
        $global:FxManifest | ConvertTo-Json -Depth 10 | Set-Content -Encoding utf8 $mfPath
        # b) new marked source without classification → HOLD; unchanged rerun → still HOLD (host cannot clear it)
        Fx-C 9005 "[OWNER-AMENDMENT]`nnew amendment nobody classified`n"; $global:FxStreams["issue:1"] = @($global:FxStreams["issue:1"]) + 9005
        $global:FxChecks.b_unclassified = Fx-Reason (Fx-RunAudit @{ PrNumber = 1; PacketOnly = $true })
        $global:FxChecks.b_rerun_still_hold = Fx-Reason (Fx-RunAudit @{ PrNumber = 1; PacketOnly = $true })
        $global:FxStreams["issue:1"] = @($global:FxStreams["issue:1"] | Where-Object { $_ -ne 9005 })
        # c) conflicting classifications across two records → HOLD
        Fx-C 9301 ("[ARCHITECT-INSTRUCTION]`n`nsecond record`nscope: PR #1`n`nrequired:`n- issue-comment 9004 sha256 $(Fx-D $global:FxComments['9004'].body) — architect wants it required`n")
        $global:FxStreams["issue:1"] = @($global:FxStreams["issue:1"]) + 9301
        $global:FxChecks.c_conflict = Fx-Reason (Fx-RunAudit @{ PrNumber = 1; PacketOnly = $true })
        $global:FxStreams["issue:1"] = @($global:FxStreams["issue:1"] | Where-Object { $_ -ne 9301 })
        # d) classified (unmarked) source body changed → digest mismatch → HOLD
        $o9002 = $global:FxComments["9002"].body; Fx-SetBody 9002 "Evidence E1 silently edited.`n"
        $global:FxChecks.d_classified_digest_changed = Fx-Reason (Fx-RunAudit @{ PrNumber = 1; PacketOnly = $true })
        Fx-SetBody 9002 $o9002
        # e) required source that cannot be read → HOLD
        $global:FxChecks.e_required_unreadable = Fx-Try ($origRecord.Replace("- pr-review 1/9101", "- issue-comment 9999 sha256 $('cd' * 32) — gone`n- pr-review 1/9101"))
        # f) excluded entry without a reason → HOLD
        $global:FxChecks.f_excluded_without_reason = Fx-Try ($origRecord.Replace("excluded: none. Any other marked source → HOLD.", "excluded:`n- issue-comment 9063 sha256 $(Fx-D $global:FxComments['9063'].body)"))
        # --- B1: git sources are path-bound ---
        $global:FxChecks.b1_path_bound_head_ok = Fx-Try (Fx-Record)
        $global:FxChecks.b1_path_bound_commit_sha_ref_ok = Fx-Try (Fx-Record -GitLine "- git-blob $($headSha):docs/contract.md $blob")
        $global:FxChecks.b1_wrong_path = Fx-Try (Fx-Record -GitLine "- git-blob HEAD:docs/no-such-file.md $blob")
        $global:FxChecks.b1_blob_elsewhere_in_tree = Fx-Try (Fx-Record -GitLine "- git-blob HEAD:README.md $blob")
        $global:FxChecks.b1_shorthand_at_base = Fx-Try (Fx-Record -GitLine "- git blobs at HEAD: contract.md $blob")
        $global:FxChecks.b1_space_form = Fx-Try (Fx-Record -GitLine "- git blob HEAD:docs/contract.md $blob")
        $global:FxChecks.b1_dotdot_path = Fx-Try (Fx-Record -GitLine "- git-blob HEAD:docs/../docs/contract.md $blob")
        # --- B2: explicit scope line ---
        $global:FxChecks.b2_scope_present = Fx-Try (Fx-Record)
        $global:FxChecks.b2_scope_absent = Fx-Try (Fx-Record -ScopeLine "" -Preamble "PR #1 fixture classification record")
        $global:FxChecks.b2_scope_other_pr = Fx-Try (Fx-Record -ScopeLine "scope: PR #7")
        $global:FxChecks.b2_scope_two_lines = Fx-Try (Fx-Record -ScopeLine "scope: PR #1`nscope: PR #1")
        $global:FxChecks.b2_scope_wrong_case = Fx-Try (Fx-Record -ScopeLine "Scope: PR #1")
        $global:FxChecks.b2_scope_trailing_text = Fx-Try (Fx-Record -ScopeLine "scope: PR #1 and #2")
        $global:FxChecks.b2_scope_after_sections = Fx-Try ((Fx-Record -ScopeLine "") + "scope: PR #1`n")
        # --- B2: canonical typed entries only ---
        $d9004 = Fx-D $global:FxComments['9004'].body
        $global:FxChecks.b2_typed_issue_comment_ok = Fx-Try (Fx-Record)
        $global:FxChecks.b2_untyped_id_hex = Fx-Try ($origRecord.Replace("- issue-comment 9004 sha256 $d9004 — background", "- 9004 $d9004 (background)"))
        $global:FxChecks.b2_review_findings = Fx-Try ($origRecord.Replace("- issue-comment 9004 sha256 $d9004 — background", "- review findings 9004 $d9004"))
        $global:FxChecks.b2_legacy_annotated_issue_comment = Fx-Try ($origRecord.Replace("- issue-comment 9004 sha256", "- issue-comment 9004 (#89) sha256"))
        $global:FxChecks.b2_legacy_review_kind = Fx-Try ($origRecord.Replace("- pr-review 1/9101", "- review 1/9101"))
        $global:FxChecks.b2_free_text_entry = Fx-Try (Fx-Record -Extra "- maybe also the design doc, probably")
        $global:FxChecks.b2_pr_body_typed_ok = Fx-Try (Fx-Record -Extra "- pr-body 1 sha256 $(Fx-D $global:FxIssueBodies['1']) — PR description")
        # j) no host manifest → the PR's own 4 streams are designated; typed issue-comments on #89 are read directly by locator
        Remove-Item $mfPath -Force
        $rj = Fx-RunAudit @{ PrNumber = 1; PacketOnly = $true }
        $global:FxChecks.j_default_streams = (@($rj | Where-Object { $_ -match '^SCANNED_STREAM' }) -join ' | ')
        $global:FxChecks.j_default_complete = Fx-Line $rj "PACKET_COMPLETE"
        $global:FxChecks.ai_prompts = @(Get-ChildItem $promptDir).Count
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
            api_patch_edit_unmarked_to_marked = @("api", "-X", "PATCH", "repos/fixture/icbm/issues/comments/9050", "-f", "body=[ARCHITECT-INSTRUCTION]`nedited")
            pr_edit_body_marked = @("pr", "edit", "1", "--repo", "fixture/icbm", "--body-file", $draft)
            pr_create_marked = @("pr", "create", "--repo", "fixture/icbm", "--base", "main", "--head", "x", "--title", "t", "--body-file", $draft)
            api_review_field_file = @("api", "-X", "POST", "repos/fixture/icbm/pulls/1/reviews", "-F", "body=@$draft")
            api_input_json = @("api", "-X", "POST", "repos/fixture/icbm/pulls/1/reviews", "--input", $jsonIn)
            pr_review_comment_marked = @("pr", "review", "1", "--comment", "--body", "  [EVIDENCE-PACKET]`r`nx")
            body_file_missing = @("pr", "comment", "1", "--body-file", (Join-Path $fx "no-such-file.md"))
        }
        $allowed = [ordered]@{
            pr_comment_unmarked = @("pr", "comment", "1", "--repo", "fixture/icbm", "--body-file", $unmarked)
            api_patch_unmarked = @("api", "-X", "PATCH", "repos/fixture/icbm/issues/comments/9050", "-f", "body=plain status update")
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
    key_lines = @(Select-String -Path $transcript -Pattern '^(AUDIT_BLOCKED|AUDIT_HOLD|HUMAN_HOLD|MERGE_GUARD|GUARD_PACKET_DIGEST|GUARD_CURRENT_PACKET_DIGEST|AUDIT_CACHE|GPT_CACHE|CLAUDE_CACHE|PACKET_DIGEST|AUDIT_IDENTITY|GPT_EVIDENCE_SEEN|CLAUDE_EVIDENCE_SEEN|PACKET_HOLD_REASON)=' | ForEach-Object { $_.Line } | Select-Object -Unique)
    prompt_source_sections = @(Get-ChildItem $promptDir -Filter "*-pr.txt" | ForEach-Object { $pt = [System.IO.File]::ReadAllText($_.FullName); "$($_.Name):sections=$([regex]::Matches($pt, '(?m)^\[(ARCHITECT-INSTRUCTION|EVIDENCE-PACKET|OWNER-AMENDMENT) id=').Count):closers=$([regex]::Matches($pt, '(?m)^\[/(ARCHITECT-INSTRUCTION|EVIDENCE-PACKET|OWNER-AMENDMENT)\]').Count):call=$([regex]::Match($pt, '(?m)^AUDIT_CALL=(\S+)').Groups[1].Value)" })
    checks = $global:FxChecks
    log = $transcript
    fx = $fx
}

$result | ConvertTo-Json -Depth 5 | Set-Content -Encoding utf8 (Join-Path $fx "result.json")
$result | ConvertTo-Json -Depth 5
