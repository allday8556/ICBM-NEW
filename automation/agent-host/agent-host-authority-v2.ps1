# AGENT_HOST_AUDIT_PROTOCOL §0.2, §3, §5.1 — shared authority helpers for every Host script.
#
#   Get-AuthorityMarker   : the deterministic marker grammar (§3 "Recognition grammar"). A marker is provenance:
#                           it never makes a source a packet input and never holds a packet (ADR-0022).
#   Invoke-GhWrite        : the single Host GitHub write path with the authority write guard (§3 "Authority write guard").
#   Get-HoldClass         : the closed hold taxonomy (§5.1): HUMAN_DECISION_REQUIRED or TECHNICAL_HOLD.
#   Get-EvidenceReferences: the ids a slice declaration cites, for referenced-evidence discovery (§3, §4).
#
# Dot-sourced by orchestrator-v1.3.ps1, resume-orchestrator-v1.3.ps1, run-audit-v1.1.ps1, run-repair-v1.1.ps1 and
# run-lookahead-main-v1.ps1. This file has no state and makes no network call of its own.

$script:AuthorityMarkerTokens = @("[ARCHITECT-INSTRUCTION]", "[EVIDENCE-PACKET]", "[OWNER-AMENDMENT]")

# A body carries a marker only when its FIRST NON-EMPTY LINE, with surrounding whitespace and a trailing CR
# removed, equals exactly one of the three tokens (case-sensitive, nothing else on the line).
# Returns the token without brackets (e.g. "ARCHITECT-INSTRUCTION") or $null.
function Get-AuthorityMarker {
    param([AllowNull()][string]$Body)

    if ($null -eq $Body) {
        return $null
    }

    foreach ($line in ($Body -split "`n")) {
        $t = $line.Trim()

        if ($t.Length -eq 0) {
            continue
        }

        foreach ($tok in $script:AuthorityMarkerTokens) {
            if ([string]::Equals($t, $tok, [System.StringComparison]::Ordinal)) {
                return $tok.Substring(1, $tok.Length - 2)
            }
        }

        # 첫 non-empty line 이 token 이 아니면 marker 없음 (뒤 줄의 token 은 ordinary text)
        return $null
    }

    return $null
}

# gh 인자에서 GitHub 에 쓰일 body 후보를 모두 꺼낸다.
#   --body/-b <text>, --body-file/-F <path|->  (gh pr/issue create|edit|comment|review)
#   gh api: -f/--raw-field body=<text>, -F/--field body=<text|@path>, --input <json path> (.body)
# 읽을 수 없는 body 파일 → $null (fail-closed: 호출자는 거부한다)
function Get-GhWriteBodies {
    param([string[]]$GhArgs)

    $bodies = New-Object System.Collections.Generic.List[string]
    $a = @($GhArgs | ForEach-Object { "$_" })
    $isApi = ($a.Count -gt 0 -and $a[0] -eq "api")

    for ($i = 0; $i -lt $a.Count; $i++) {
        $k = $a[$i]
        $v = if ($i + 1 -lt $a.Count) { $a[$i + 1] } else { $null }

        if ($k -match '^(--body|--body-file)=(.*)$') {
            $k = $Matches[1]
            $v = $Matches[2]
            $i--
        }

        if (-not $isApi -and $k -in @("--body", "-b")) {
            $bodies.Add([string]$v)
            $i++
            continue
        }

        if (-not $isApi -and $k -in @("--body-file", "-F")) {
            if ($v -eq "-" -or -not $v -or -not (Test-Path -LiteralPath $v -PathType Leaf)) {
                return $null
            }

            $bodies.Add([System.IO.File]::ReadAllText($v))
            $i++
            continue
        }

        if ($isApi -and $k -in @("-f", "--raw-field", "-F", "--field")) {
            $kv = "$v" -split "=", 2

            if ($kv.Count -eq 2 -and $kv[0] -in @("body", "title", "message", "commit_message")) {
                $val = $kv[1]

                if ($k -in @("-F", "--field") -and $val.StartsWith("@")) {
                    $p = $val.Substring(1)

                    if (-not (Test-Path -LiteralPath $p -PathType Leaf)) {
                        return $null
                    }

                    $val = [System.IO.File]::ReadAllText($p)
                }

                $bodies.Add($val)
            }

            $i++
            continue
        }

        if ($isApi -and $k -eq "--input") {
            if (-not $v -or -not (Test-Path -LiteralPath $v -PathType Leaf)) {
                return $null
            }

            try {
                $j = [System.IO.File]::ReadAllText($v) | ConvertFrom-Json

                foreach ($prop in @("body", "title", "message", "commit_message")) {
                    if ($null -ne $j.$prop) {
                        $bodies.Add([string]$j.$prop)
                    }
                }
            }
            catch {
                return $null
            }

            $i++
            continue
        }
    }

    return ,$bodies
}

# 모든 Host GitHub write 는 이 함수로만 보낸다. 쓰기 전에 body 를 검사하고,
# marker-first body 이면 요청을 보내지 않고 거부한다 (Refused=$true, HOLD 사유 반환).
# 같은 text 를 local draft 파일로 쓰는 것은 이 guard 대상이 아니다.
function Invoke-GhWrite {
    param(
        [string[]]$GhArgs,
        [string]$Site,
        [string]$ErrPath = ""
    )

    $bodies = Get-GhWriteBodies -GhArgs $GhArgs

    if ($null -eq $bodies) {
        Write-Host "GH_WRITE_REFUSED=$Site reason=AUTHORITY_WRITE_GUARD_BODY_UNREADABLE (no request sent)"
        return [pscustomobject]@{ Refused = $true; Reason = "AUTHORITY_WRITE_GUARD_BODY_UNREADABLE"; Output = $null; ExitCode = -1 }
    }

    foreach ($b in $bodies) {
        $m = Get-AuthorityMarker ($b.Replace("`r`n", "`n"))

        if ($m) {
            Write-Host "GH_WRITE_REFUSED=$Site reason=AUTHORITY_WRITE_GUARD_MARKER_FIRST_BODY:$m (no request sent)"
            return [pscustomobject]@{ Refused = $true; Reason = "AUTHORITY_WRITE_GUARD_MARKER_FIRST_BODY"; Output = $null; ExitCode = -1 }
        }
    }

    $oldEap = $ErrorActionPreference

    try {
        $ErrorActionPreference = "Continue"

        if ($ErrPath) {
            $out = gh @GhArgs 2>> $ErrPath
        }
        else {
            $out = gh @GhArgs 2>$null
        }

        $code = $LASTEXITCODE
    }
    finally {
        $ErrorActionPreference = $oldEap
    }

    return [pscustomobject]@{ Refused = $false; Reason = ""; Output = $out; ExitCode = $code }
}

# -------------------------------------------------
# Hold taxonomy (AGENT_HOST_AUDIT_PROTOCOL §5.1; ADR-0022 §3-§4)
#
# Exactly two classes stop a loop pass:
#   HUMAN_DECISION_REQUIRED : a product decision or a real external action that only the user may decide. Closed list below.
#   TECHNICAL_HOLD          : everything else. The Host recovers or retries by itself and never asks the user.
#
# The class is decided by the category, never by how often something failed. A hold reason is HUMAN_DECISION_REQUIRED
# only when the WHOLE reason is one of the forms the Host itself composes after it validated the category:
#   <CATEGORY>                                    a category by itself
#   NEXT_HOLD_<CATEGORY>                          the selector's stop
#   <WHO>_HUMAN_DECISION_REQUIRED_<CATEGORY>      an auditor's, a fixer's or an implementer's stop (<WHO> is [A-Z_]+)
#   PR_ON_OWNER_HOLD, GUARD_PR_ON_OWNER_HOLD      the owner's own hold file, which the orchestrator has just read
# OWNER_HOLD is never taken from the first three forms: only the two owner-hold reasons, which the orchestrator writes
# after it found the hold file, carry it. OWNER_HOLD, NEXT_HOLD_OWNER_HOLD or X_HUMAN_DECISION_REQUIRED_OWNER_HOLD from
# any other producer is technical.
# A category word inside any other reason decides nothing: NO_LIVE_ACTION, NEXT_HOLD_NOT_LIVE and LIVE_CHECK_FAILED are
# technical. The words "HUMAN_DECISION_REQUIRED" alone decide nothing either.
# -------------------------------------------------

$script:HumanDecisionCategories = @(
    "NEW_PRODUCT_FEATURE",          # A: a product feature the canonical requirements do not contain
    "PRODUCT_DIRECTION_UNDECIDED",  # B: a user-visible behaviour, UX or policy with several real product directions
    "BEYOND_USER_REQUIREMENT",      # C: a change that goes beyond what the user asked for
    "LIVE",                         # D: a LIVE provider mutation
    "PROVIDER_CALL",                # D: a real provider or marketplace call
    "CANARY",                       # D: a real canary
    "REAL_EXTERNAL_READ",           # D: a real supplier/provider read whose acceptance needs its own grant
    "RESIDUAL_RISK_APPROVAL",       # D: accepting a residual risk of a real external action
    "COST",                         # D: a payment or a cost
    "EXTERNAL_DATA_TRANSFER",       # D: sending real data to an external service
    "DESTRUCTIVE",                  # D: a destructive operation, a force-push, a branch deletion
    "OWNER_HOLD"                    # the owner's own hold file on a PR (state\merge-hold-pr-<N>.json)
)

function Get-HoldClass {
    param([AllowNull()][string]$Reason)

    $r = "$Reason"

    if ($r -cin @("PR_ON_OWNER_HOLD", "GUARD_PR_ON_OWNER_HOLD")) {
        return "HUMAN_DECISION_REQUIRED"
    }

    # the whole reason, case-sensitively: nothing before the form and nothing after the category
    $m = [regex]::Match($r, '^(?:NEXT_HOLD_|[A-Z][A-Z_]*_HUMAN_DECISION_REQUIRED_)?([A-Z_]+)$')

    if ($m.Success -and $m.Groups[1].Value -cne "OWNER_HOLD" -and $m.Groups[1].Value -cin $script:HumanDecisionCategories) {
        return "HUMAN_DECISION_REQUIRED"
    }

    return "TECHNICAL_HOLD"
}

# The closed category list as one prompt line, so every prompt and every parser uses the same words.
function Get-HumanDecisionCategoryList {
    return (@($script:HumanDecisionCategories | Where-Object { $_ -ne "OWNER_HOLD" }) -join "|")
}

# The category an auditor, a fixer or the selector named at the START of a text, when it is one of the closed list
# (the owner's hold file is not theirs to name). Anything else returns $null, and the stop is then technical.
function Get-HumanDecisionCategory {
    param([AllowNull()][string]$Text)

    $m = [regex]::Match("$Text".TrimStart(), '^\[?([A-Z_]+)\]?(?:[:\s\-]|$)')

    if ($m.Success -and $m.Groups[1].Value -cin @($script:HumanDecisionCategories | Where-Object { $_ -ne "OWNER_HOLD" })) {
        return $m.Groups[1].Value
    }

    return $null
}

# -------------------------------------------------
# Referenced evidence (AGENT_HOST_AUDIT_PROTOCOL §3, §4; ADR-0022 §5)
#
# A slice declares its evidence by citing it. The declaration is the PR body (and the host slice specification when one
# exists). Two deterministic reference forms are read from it, nothing is inferred:
#   an issue reference  "Issue #<n>"              -> that issue's comments are a scanned stream of this packet
#   a citation, in a code span, of a number of 9-12 digits. The form names the KIND of the source:
#       `<id>`                  -> the issue / PR conversation comment with that id
#       `review:<id>`           -> the PR review with that id
#       `review-comment:<id>`   -> the PR review comment with that id
# GitHub numbers the three kinds separately, so an id alone does not identify a source. A citation resolves only to a
# source of its own kind: a review that happens to carry the id of a cited comment never stands in for it.
#   a canonical document citation, in a code span:
#       `canon:<repository path>`  -> that file at the AUDITED BASE, as a git blob source
# It is how the declaration adds canonical text the slice is judged against to the packet. The base is what binds
# before the slice: what the slice itself changes in the canon is in its diff, so a slice can never rewrite the canon
# it is judged against. The path is a plain repository path: no "..", no leading "/", no space.
#
# The declaration only ADDS canon. The baseline below is carried by every packet whatever the declaration cites, so
# an agent-written declaration can never leave out the documents that decide what may be built and who decides it.
# A citation is a claim that the source is evidence. One that no scanned stream holds cannot be read, so it is a
# TECHNICAL_HOLD (CITED_SOURCE_UNRESOLVED): declared evidence never silently disappears from a packet. A number that is
# not in a code span (a CI run id, a line count) is not a citation.
# -------------------------------------------------

# The Host's baseline canon: the scope and authority documents, read at the audited base. Not configurable per
# runtime and not selectable by a declaration.
function Get-BaselineCanon {
    return @(
        "documents/roadmap/ROADMAP.md",
        "documents/roadmap/CURRENT-MILESTONE.md",
        "documents/rules/07-execution-safety.md",
        "documents/rules/14-operating-authority.md"
    )
}

function Get-EvidenceReferences {
    param([AllowNull()][string]$Text)

    $issues = New-Object "System.Collections.Generic.SortedSet[long]"
    $ids = New-Object "System.Collections.Generic.SortedSet[long]"
    $reviews = New-Object "System.Collections.Generic.SortedSet[long]"
    $reviewComments = New-Object "System.Collections.Generic.SortedSet[long]"

    if ($Text) {
        foreach ($m in [regex]::Matches($Text, '(?<![A-Za-z0-9])Issue #([1-9][0-9]{0,6})(?![0-9])')) {
            [void]$issues.Add([long]$m.Groups[1].Value)
        }

        foreach ($m in [regex]::Matches($Text, '(?<!`)`(review:|review-comment:)?([1-9][0-9]{8,11})`(?!`)')) {
            $n = [long]$m.Groups[2].Value

            switch -CaseSensitive ($m.Groups[1].Value) {
                "review:" { [void]$reviews.Add($n) }
                "review-comment:" { [void]$reviewComments.Add($n) }
                default { [void]$ids.Add($n) }
            }
        }
    }

    $canon = New-Object "System.Collections.Generic.SortedSet[string]" ([System.StringComparer]::Ordinal)

    if ($Text) {
        foreach ($m in [regex]::Matches($Text, '(?<!`)`canon:([A-Za-z0-9_][A-Za-z0-9_./-]*)`(?!`)')) {
            $p = $m.Groups[1].Value

            if ($p -notmatch '(^|/)\.\.?(/|$)' -and $p -notmatch '//' -and -not $p.EndsWith("/")) {
                [void]$canon.Add($p)
            }
        }
    }

    # Keys: "<source kind>:<id>", the kind being the locator kind of the stream item that may resolve the citation.
    # Labels: the citation as the declaration wrote it, for a hold reason.
    $keys = New-Object System.Collections.Generic.List[string]
    $labels = @{}

    foreach ($n in $ids) { $k = "github_issue_comment:$n"; $keys.Add($k); $labels[$k] = "$n" }
    foreach ($n in $reviews) { $k = "github_pr_review:$n"; $keys.Add($k); $labels[$k] = "review:$n" }
    foreach ($n in $reviewComments) { $k = "github_pr_review_comment:$n"; $keys.Add($k); $labels[$k] = "review-comment:$n" }

    return [pscustomobject]@{
        Issues = @($issues | ForEach-Object { [string]$_ })
        Ids = @($ids | ForEach-Object { [string]$_ })
        Reviews = @($reviews | ForEach-Object { [string]$_ })
        ReviewComments = @($reviewComments | ForEach-Object { [string]$_ })
        Keys = @($keys)
        Labels = $labels
        Canon = @($canon)
    }
}
