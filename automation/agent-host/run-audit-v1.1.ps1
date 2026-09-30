param(
    [Parameter(Mandatory=$true)]
    [int]$PrNumber,
    [switch]$PacketOnly,
    # audit-before-CI 정책: 감사는 CI 없이 exact HEAD 에서 먼저, FULL CI 는 DUAL PASS 후 1회 (orchestrator)
    [switch]$SkipCiGate
)

$ErrorActionPreference = "Stop"
# Native-process stdin/stdout must stay UTF-8.
# Windows PowerShell otherwise may replace Korean text with '?'.
$nativeUtf8 = New-Object System.Text.UTF8Encoding($false)
$script:OutputEncoding = $nativeUtf8
[Console]::OutputEncoding = $nativeUtf8


$hostRoot = $PSScriptRoot

# V2 §3 marker grammar + authority write guard (shared, identity-pinned in the runtime chain)
. (Join-Path $PSScriptRoot "agent-host-authority-v2.ps1")
$configPath = Join-Path $hostRoot "state\orchestrator-config.json"
$config = Get-Content $configPath -Raw -Encoding utf8 | ConvertFrom-Json

# 사용자 repo는 object DB / origin remote 로만 사용한다.
# 사용자 repo의 branch / HEAD / working files는 절대 바꾸지 않는다.
$repo = [string]$config.repo_path
$repoSlug = [string]$config.repository
$logDir = Join-Path $hostRoot "logs"
$stateDir = Join-Path $hostRoot "state"
$worktreeRoot = Join-Path $hostRoot "worktrees"

# 감사 방식이 바뀌면 이 값을 변경.
# 캐시는 같은 policy version에서만 재사용한다.
# v6: 사용자 checkout 대신 dedicated audit worktree + PR changed files 동적 segment.
# v7 (AGENT_HOST_PROTOCOL_V2 PR-A): generated immutable canonical Audit Packet + marked authoritative
#     sources + audit identity (HEAD, packet_digest) + evidence_seen. v6 이하 결과는 historical evidence 전용.
# v8 (canonical V2 at main a0643e4, PR-A alignment): content-bound identities, full edit-aware re-scan, marker grammar,
#     classification only from user/architect records, identity-level evidence_seen. v7 (draft) 결과는 재사용 불가.
# v9: ADR-0022 — no human classification. Packet sources are the durable evidence the slice declaration cites
#     (referenced-evidence discovery); a marker is provenance and never holds a packet. v8 이하 결과는 재사용 불가.
$auditPolicyVersion = "packet-v9-sol-high"

New-Item -ItemType Directory -Force -Path $logDir | Out-Null
New-Item -ItemType Directory -Force -Path $stateDir | Out-Null
New-Item -ItemType Directory -Force -Path $worktreeRoot | Out-Null

$ts = Get-Date -Format "yyyyMMdd-HHmmss"

$script:nativeErrPath = Join-Path $logDir "pr-$PrNumber-audit-native-$ts.stderr.txt"

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
Write-Host "AUDIT_POLICY=$auditPolicyVersion"

# -------------------------------------------------
# Native helpers
#
# PS5.1 + ErrorActionPreference=Stop 에서 git의 정상 stderr
# (CRLF 경고, worktree 진행 메시지)가 terminating error로 오인되지 않도록
# lookahead와 같은 방식(EAP=Continue + stderr 파일 분리)을 사용한다.
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

function Test-GitCommit {
    param([string]$Sha)

    Invoke-Git @("-C", $repo, "cat-file", "-e", "$Sha^{commit}") | Out-Null
    return ($LASTEXITCODE -eq 0)
}

# main / PR HEAD object를 host 전용 ref namespace로만 가져온다.
# refs/heads, refs/remotes, 사용자 checkout은 건드리지 않는다.
function Sync-HostRefs {
    param(
        [string]$Branch,
        [int]$Number
    )

    Invoke-Git @(
        "-C", $repo, "fetch", "--no-tags", "--quiet", "--refmap=", "origin",
        "+refs/heads/main:refs/icbm-agent-host/main",
        "+refs/heads/${Branch}:refs/icbm-agent-host/pr-$Number"
    ) | Out-Null

    if ($LASTEXITCODE -ne 0) {
        # fork PR 등 branch fetch 불가 시 pull ref 사용
        Invoke-Git @(
            "-C", $repo, "fetch", "--no-tags", "--quiet", "--refmap=", "origin",
            "+refs/heads/main:refs/icbm-agent-host/main",
            "+refs/pull/$Number/head:refs/icbm-agent-host/pr-$Number"
        ) | Out-Null
    }
}

# repair의 isolated/disposable worktree 원칙을 audit에도 재사용.
# exact HEAD가 다르면 재생성한다.
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
        Invoke-Git @("-C", $repo, "worktree", "remove", "--force", $Path) | Out-Null

        if (Test-Path $Path) {
            Remove-Item $Path -Recurse -Force
        }
    }

    Invoke-Git @("-C", $repo, "worktree", "add", "-f", "--detach", $Path, $Commit) | Out-Null

    if ($LASTEXITCODE -ne 0) {
        return $false
    }

    $wtHead = "$(Invoke-Git @('-C', $Path, 'rev-parse', 'HEAD'))".Trim()
    return ($wtHead -eq $Commit)
}

Write-Host ""
Write-Host "========================================"
Write-Host " ICBM AGENT HOST V1.1 - LOW USAGE"
Write-Host " PR #$PrNumber"
Write-Host "========================================"
Write-Host ""

# -------------------------------------------------
# 1. PR metadata
# -------------------------------------------------

$pr = gh pr view $PrNumber `
    --repo $repoSlug `
    --json number,title,state,headRefOid,headRefName,baseRefName |
    ConvertFrom-Json

if ($pr.state -ne "OPEN") {
    Write-Host "PR_STATE=$($pr.state)"
    Write-Output "AUDIT_BLOCKED=PR_NOT_OPEN"
    return
}

$prHead = [string]$pr.headRefOid
$prBranch = [string]$pr.headRefName

$mainHead = "$(gh api "repos/$repoSlug/commits/main" --jq .sha)".Trim()

Write-Host "PR       : #$PrNumber"
Write-Host "TITLE    : $($pr.title)"
Write-Host "BRANCH   : $prBranch"
Write-Host "PR HEAD  : $prHead"
Write-Host "MAIN     : $mainHead"
Write-Host ""

# -------------------------------------------------
# 2. CI gate - AI에게 판단시키지 않는다
# -------------------------------------------------

$checkJson = gh api `
    -H "Accept: application/vnd.github+json" `
    "repos/$repoSlug/commits/$prHead/check-runs?per_page=100"

$checks = $checkJson | ConvertFrom-Json
$checkRuns = @($checks.check_runs)

$incomplete = @(
    $checkRuns | Where-Object {
        $_.status -ne "completed"
    }
)

# Issue #143: "CI gate" job 이 있으면 scope 가 의도적으로 건너뛴(skipped) job 만 중립이고,
# gate 자체는 이 exact HEAD 에서 success 여야 한다. gate 가 없는 기존 workflow 는 종전대로 전부 success.
$gateRuns = @($checkRuns | Where-Object { $_.name -eq "CI gate" })
$scopedCi = ($gateRuns.Count -gt 0 -or @($checkRuns | Where-Object { $_.name -eq "CI scope" }).Count -gt 0)
$neutralConclusions = if ($scopedCi) { @("success", "skipped") } else { @("success") }
# draft(wip) run 의 skipped gate 는 중립. 병합 준비는 success gate 가 하나 이상 있어야 한다.
$gateSucceeded = @($gateRuns | Where-Object { $_.status -eq "completed" -and $_.conclusion -eq "success" })
$gateOpen = @($gateRuns | Where-Object { $_.status -ne "completed" })
if ($scopedCi -and $gateSucceeded.Count -eq 0 -and $gateOpen.Count -eq 0) {
    # gate job 은 모든 job 이 끝난 뒤 생성된다 → 아직 판정 전
    $incomplete = @($incomplete) + @([pscustomobject]@{ name = "CI gate"; status = "queued"; conclusion = $null })
}

$failed = @(
    $checkRuns | Where-Object {
        $_.status -eq "completed" -and
        ($_.conclusion -notin $neutralConclusions -or
         ($_.name -eq "CI gate" -and $_.conclusion -notin @("success", "skipped")))
    }
)

if ($PacketOnly) {
    # PacketOnly는 AI를 호출하지 않는 진단 모드 — CI 상태와 무관하게 packet만 만든다.
    Write-Host "CI         : PACKET_ONLY (total=$($checkRuns.Count) incomplete=$($incomplete.Count) failed=$($failed.Count))"
}
elseif ($SkipCiGate) {
    # 감사 우선: FULL CI 는 GPT PASS + Claude PASS 가 같은 HEAD 에 모인 뒤에만 돈다 (병합 조건은 MERGE_GUARD 가 강제)
    Write-Host "CI         : NOT_REQUIRED_BEFORE_AUDIT (audit-before-CI policy; total=$($checkRuns.Count))"
}
elseif ($checkRuns.Count -eq 0 -or $incomplete.Count -gt 0) {
    Write-Host ""

    foreach ($check in $incomplete) {
        Write-Host "RUNNING=$($check.name)"
    }

    Write-Output "AUDIT_BLOCKED=CI_RUNNING"
    return
}

if (-not $PacketOnly -and -not $SkipCiGate -and $failed.Count -gt 0) {
    Write-Host ""

    foreach ($check in $failed) {
        Write-Host "FAILED=$($check.name):$($check.conclusion)"
    }

    Write-Output "AUDIT_BLOCKED=CI_NOT_GREEN"
    return
}

Write-Host "CI         : $($checkRuns.Count)/$($checkRuns.Count) SUCCESS"
Write-Host ""

# -------------------------------------------------
# 3. Dedicated exact-head audit worktree
#
# 사용자 repo가 main이어도, dirty여도 감사 가능.
# 사용자 checkout은 읽지도 바꾸지도 않는다.
# -------------------------------------------------

Sync-HostRefs -Branch $prBranch -Number $PrNumber

if (-not (Test-GitCommit $mainHead)) {
    Write-Output "AUDIT_BLOCKED=MAIN_NOT_FETCHED"
    return
}

if (-not (Test-GitCommit $prHead)) {
    # GitHub PR head와 fetch 결과가 잠깐 어긋날 수 있다 → 재시도 대상.
    Write-Output "AUDIT_BLOCKED=HEAD_MOVED"
    return
}

$auditWorktree = Join-Path $worktreeRoot "audit-pr-$PrNumber"

if (-not (Initialize-HostWorktree -Path $auditWorktree -Commit $prHead)) {
    Write-Output "AUDIT_BLOCKED=AUDIT_WORKTREE_FAILED"
    return
}

Write-Host "AUDIT WORKTREE : $auditWorktree"
Write-Host "EXACT HEAD     : MATCH"
Write-Host "WORKTREE       : CLEAN (host-owned)"
Write-Host ""

# -------------------------------------------------
# 4. Changed files - GitHub가 canonical manifest
# -------------------------------------------------

$utf8 = New-Object System.Text.UTF8Encoding($false, $true)
$utf8Out = New-Object System.Text.UTF8Encoding($false)

# Canonical text (V2 §4.2): UTF-8 BOM 제거, CRLF/CR → LF. 그 외 공백은 바꾸지 않는다.
function ConvertTo-CanonicalText {
    param([string]$Text)

    if ($null -eq $Text) {
        return ""
    }

    if ($Text.Length -gt 0 -and $Text[0] -eq [char]0xFEFF) {
        $Text = $Text.Substring(1)
    }

    return $Text.Replace("`r`n", "`n").Replace("`r", "`n")
}

function Get-Sha256Hex {
    param([string]$Text)

    $sha = [System.Security.Cryptography.SHA256]::Create()

    try {
        $bytes = $utf8Out.GetBytes($Text)
        return (([System.BitConverter]::ToString($sha.ComputeHash($bytes))) -replace '-', '').ToLower()
    }
    finally {
        $sha.Dispose()
    }
}

$incompleteReasons = New-Object System.Collections.Generic.List[string]

# jq에 큰따옴표를 쓰지 않는다 (PS5.1 native 인자 quoting 문제).
$ghFileLines = @(
    gh api --paginate `
        "repos/$repoSlug/pulls/$PrNumber/files?per_page=100" `
        --jq '.[] | [.filename, (.previous_filename // .filename)] | @tsv'
) | Where-Object { $_ }

$ghChangedCount = [int]("$(gh api "repos/$repoSlug/pulls/$PrNumber" --jq .changed_files)".Trim())

$prFiles = @(
    foreach ($line in $ghFileLines) {
        $cols = "$line" -split "`t"
        $paths = @($cols | Where-Object { $_ } | Select-Object -Unique)

        [pscustomobject]@{
            File = $cols[0]
            Paths = $paths
        }
    }
)

if ($prFiles.Count -eq 0) {
    $incompleteReasons.Add("NO_CHANGED_FILES")
}

if ($prFiles.Count -ne $ghChangedCount) {
    # GitHub files API 페이지 누락/3000개 제한 등
    $incompleteReasons.Add("GITHUB_FILE_COUNT_MISMATCH:$($prFiles.Count)/$ghChangedCount")
}

# GitHub manifest와 exact main...HEAD local diff가 같은 파일 집합인지 확인
$ghPathSet = New-Object "System.Collections.Generic.HashSet[string]" ([System.StringComparer]::Ordinal)

foreach ($f in $prFiles) {
    foreach ($p in $f.Paths) {
        [void]$ghPathSet.Add($p)
    }
}

$localNames = @(
    Invoke-Git @(
        "-C", $auditWorktree, "-c", "core.quotepath=false",
        "diff", "--no-renames", "--name-only", "$mainHead...$prHead"
    )
) | Where-Object { $_ }

if ($LASTEXITCODE -ne 0) {
    Write-Output "AUDIT_BLOCKED=GIT_NAME_DIFF_FAILED"
    return
}

$localPathSet = New-Object "System.Collections.Generic.HashSet[string]" ([System.StringComparer]::Ordinal)

foreach ($n in $localNames) {
    [void]$localPathSet.Add("$n")
}

if (-not $ghPathSet.SetEquals($localPathSet)) {
    $onlyGh = @($ghPathSet | Where-Object { -not $localPathSet.Contains($_) })
    $onlyLocal = @($localPathSet | Where-Object { -not $ghPathSet.Contains($_) })
    $incompleteReasons.Add(
        "MANIFEST_MISMATCH:github_only=$($onlyGh -join ',');local_only=$($onlyLocal -join ',')"
    )
}

# -------------------------------------------------
# 5. Per-file exact diff (git native --output → UTF-8 보존)
# -------------------------------------------------

$tmpDiffPath = Join-Path $logDir "pr-$PrNumber-filediff-tmp.utf8.txt"

$fileDiffs = New-Object System.Collections.Generic.List[object]

foreach ($f in $prFiles) {
    Remove-Item $tmpDiffPath -Force -ErrorAction SilentlyContinue

    $gitArgs = @(
        "-C", $auditWorktree,
        "-c", "core.quotepath=false",
        "diff",
        "--no-color",
        "--unified=2",
        "--output=$tmpDiffPath",
        "$mainHead...$prHead",
        "--"
    ) + $f.Paths

    Invoke-Git $gitArgs | Out-Null

    if ($LASTEXITCODE -ne 0) {
        Write-Output "AUDIT_BLOCKED=GIT_DIFF_FAILED"
        return
    }

    try {
        $text = [System.IO.File]::ReadAllText($tmpDiffPath, $utf8)
    }
    catch {
        Write-Output "AUDIT_BLOCKED=UTF8_DECODE_FAILED"
        return
    }

    # canonical packet 은 LF only — diff 도 읽는 즉시 정규화 (이후 completeness proof 도 정규화된 원문 기준)
    $text = ConvertTo-CanonicalText $text

    if (-not $text) {
        $incompleteReasons.Add("EMPTY_FILE_DIFF:$($f.File)")
        $text = ""
    }

    $fileDiffs.Add([pscustomobject]@{
        File = $f.File
        Text = $text
    })
}

Remove-Item $tmpDiffPath -Force -ErrorAction SilentlyContinue

$diffText = ($fileDiffs | ForEach-Object { $_.Text }) -join ""

$changedFilesText = ($prFiles | ForEach-Object { $_.File }) -join "`r`n"

$packetDiffText = (
    $fileDiffs | ForEach-Object {
        $_.Text + "`r`n--- END OF FILE DIFF: $($_.File) (complete) ---`r`n"
    }
) -join ""

# host가 repo owner architect ruling으로 만든 remediation PR이면,
# 그 ruling을 승인 범위 증거로 packet에 포함한다 (범위 밖 변경은 auditor가 scope violation으로 판정).
$authorizationBlock = ""

# V2 §4 packet input identities: slice spec path + blob SHA, scope allow-list source, remediation authorization comment
# (content-bound: locator + canonical body digest). host state 의 slice spec 은 pre-V2 host registry (authority 아님) 로 표시한다.
$sliceSpecRel = "NONE"
$sliceSpecSha = "NONE"
$sliceSpecOrigin = "NONE"
$remediationAuthIdentity = "NONE"

# git blob SHA-1 (git hash-object 와 같은 값): sha1("blob <len>\0" + bytes)
function Get-GitBlobSha {
    param([byte[]]$Bytes)

    $hdr = [System.Text.Encoding]::ASCII.GetBytes("blob $($Bytes.Length)`0")
    $all = New-Object byte[] ($hdr.Length + $Bytes.Length)
    [Array]::Copy($hdr, 0, $all, 0, $hdr.Length)
    [Array]::Copy($Bytes, 0, $all, $hdr.Length, $Bytes.Length)
    $sha1 = [System.Security.Cryptography.SHA1]::Create()

    try {
        return (([System.BitConverter]::ToString($sha1.ComputeHash($all))) -replace '-', '').ToLower()
    }
    finally {
        $sha1.Dispose()
    }
}

# GitHub body 의 canonical 형태 (V2 §4): API 가 준 body 그대로, UTF-8, 줄끝만 LF 로. 그 외는 바꾸지 않는다.
function ConvertTo-LfText {
    param([AllowNull()][string]$Text)

    if ($null -eq $Text) {
        return ""
    }

    return $Text.Replace("`r`n", "`n").Replace("`r", "`n")
}
$scopeAllowlist = "NONE"
$scopeAllowlistSha = "NONE"
$remediationAuthCommentId = "NONE"

function Get-HostRelativePath {
    param([string]$Path)

    $full = [System.IO.Path]::GetFullPath($Path)
    $rootFull = [System.IO.Path]::GetFullPath($hostRoot).TrimEnd('\') + '\'

    if ($full.StartsWith($rootFull, [System.StringComparison]::OrdinalIgnoreCase)) {
        return $full.Substring($rootFull.Length).Replace('\', '/')
    }

    return $full.Replace('\', '/')
}

# auto-next PR이면 host가 canonical 문서에서 선택한 slice 범위를 packet에 포함한다.
foreach ($nf in @(Get-ChildItem $stateDir -Filter "next-main-*.json" -ErrorAction SilentlyContinue)) {
    $nreg = $null

    try {
        $nreg = Get-Content $nf.FullName -Raw -Encoding utf8 | ConvertFrom-Json
    }
    catch {
        $nreg = $null
    }

    if ($nreg -and "$($nreg.pr)" -eq "$PrNumber" -and $nreg.slice_spec -and (Test-Path $nreg.slice_spec)) {
        $sl = Get-Content $nreg.slice_spec -Raw -Encoding utf8 | ConvertFrom-Json
        $sliceSpecRel = Get-HostRelativePath $nreg.slice_spec
        $sliceSpecSha = Get-GitBlobSha ([System.IO.File]::ReadAllBytes($nreg.slice_spec))
        $sliceSpecOrigin = "host_registry_pre_v2_not_authority"
        $scopeAllowlist = ConvertTo-Json -Compress -InputObject @($sl.allowed_paths | ForEach-Object { [string]$_ })
        $scopeAllowlistSha = Get-Sha256Hex $scopeAllowlist
        $authorizationBlock = (
            "APPROVED SLICE SCOPE (host-selected from the canonical documents at main $($sl.main); scope evidence only):`r`n" +
            "SLICE_ID=$($sl.slice_id)`r`nSLICE_TITLE=$($sl.slice_title)`r`nCANONICAL_SOURCES=$($sl.canonical_sources)`r`n" +
            "ALLOWED_PATHS=$(@($sl.allowed_paths) -join ', ')`r`nSCHEMA_CHANGE=$($sl.schema_change)`r`n" +
            "ACCEPTANCE=$($sl.acceptance)`r`nFORBIDDEN=$($sl.forbidden)`r`nSPEC:`r`n$($sl.spec)`r`n" +
            "--- APPROVED SLICE SCOPE END ---`r`n"
        )
    }
}

foreach ($rf in @(Get-ChildItem $stateDir -Filter "remediation-main-*.json" -ErrorAction SilentlyContinue)) {
    $reg = $null

    try {
        $reg = Get-Content $rf.FullName -Raw -Encoding utf8 | ConvertFrom-Json
    }
    catch {
        $reg = $null
    }

    if ($reg -and "$($reg.pr)" -eq "$PrNumber" -and $reg.authorization_comment_id) {
        $authComment = gh api "repos/$repoSlug/issues/comments/$($reg.authorization_comment_id)" | ConvertFrom-Json

        if ($authComment -and [string]$authComment.user.login -eq ($repoSlug -split "/")[0]) {
            $remediationAuthCommentId = [string]$reg.authorization_comment_id
            $remediationAuthIdentity = "github_issue_comment:$remediationAuthCommentId@$(Get-Sha256Hex (ConvertTo-LfText ([string]$authComment.body)))"
            $authorizationBlock = (
                "AUTHORIZATION EVIDENCE (repository owner comment $($authComment.html_url)):`r`n" +
                "$($authComment.body)`r`n" +
                "--- AUTHORIZATION EVIDENCE END ---`r`n"
            )
        }
    }
}

# -------------------------------------------------
# 5b. Packet sources (AGENT_HOST_AUDIT_PROTOCOL §3, §4, §4.1; ADR-0022 §5)
#
# No human classifies a source. The slice declares its evidence by citing it, and the Host reads exactly that:
#   declaration  = this PR's body, plus the host slice specification / remediation authorization when one exists
#   streams      = this PR's own four streams (conversation comments, body, reviews, review comments)
#                  + the comments of every issue the declaration names as "Issue #<n>"
#                  + any stream state\audit-sources-pr-<N>.json designates (optional; designation only)
#   sources      = the PR body itself (the declaration is audit input);
#                  every source the declaration cites, by its kind (agent-host-authority-v2.ps1 Get-EvidenceReferences):
#                    `<id>` a conversation comment, `review:<id>` a review, `review-comment:<id>` a review comment,
#                    each <locator>@<sha256 of the current body>;
#                  the Host's baseline canon and every `canon:<path>` the declaration cites, at the audited base,
#                    each git_blob:base:<path>@<blob SHA>.
#                  All are required. A citation that cannot be read is a TECHNICAL_HOLD; a baseline document absent at
#                  the base is named in the packet header.
# 매 생성마다 모든 stream 을 전 페이지, 현재 body 로 다시 읽는다 (edit-aware). watermark 는 scan provenance 일 뿐
#   packet bytes 밖(state\packets\*.scan.json)에만 기록되고 어떤 source 도 건너뛰지 않는다.
# marker = body 의 첫 non-empty line 이 정확히 token (agent-host-authority-v2.ps1 Get-AuthorityMarker). It is provenance only:
#   a marked source is a packet source when the declaration cites it, like any other source, and is otherwise history.
#   A marked source never holds a packet, and no "scope: PR #<N>" record is read (ADR-0022 §5; legacy records stay as history).
# Edits still bind: a cited source's body digest is in the packet, so an edited body changes the packet digest.
# TECHNICAL_HOLD only (the Host retries by itself, never the user): an unreadable or truncated stream, an unreadable
#   declaration, a citation no scanned stream holds, an invalid host manifest, a packet derivation failure.
# -------------------------------------------------

$packetHoldReasons = New-Object System.Collections.Generic.List[string]
$packetHoldDetails = New-Object System.Collections.Generic.List[string]
$sourceManifestPath = Join-Path $stateDir "audit-sources-pr-$PrNumber.json"
$sourceManifestRel = "NONE"
$sourceManifestSha = "NONE"
$designatedStreams = New-Object System.Collections.Generic.List[object]
$streamItems = New-Object System.Collections.Generic.List[object]
$scanProvenance = New-Object System.Collections.Generic.List[object]
$packetSources = New-Object System.Collections.Generic.List[object]
$scanStartedAt = (Get-Date).ToUniversalTime().ToString("o")

function Add-PacketHold {
    param([string]$Reason, [string]$Detail = "")

    if (-not $packetHoldReasons.Contains($Reason)) {
        $packetHoldReasons.Add($Reason)
        $packetHoldDetails.Add("$Reason $Detail".Trim())
    }
}

function Invoke-GhJson {
    param([string]$ApiPath)

    $oldEap = $ErrorActionPreference

    try {
        $ErrorActionPreference = "Continue"
        $raw = gh api $ApiPath 2>> $script:nativeErrPath
        $code = $LASTEXITCODE
    }
    finally {
        $ErrorActionPreference = $oldEap
    }

    if ($code -ne 0 -or $null -eq $raw) {
        return [pscustomobject]@{ Ok = $false; Data = $null }
    }

    try {
        $data = (@($raw) -join "`n") | ConvertFrom-Json
    }
    catch {
        return [pscustomobject]@{ Ok = $false; Data = $null }
    }

    return [pscustomobject]@{ Ok = $true; Data = $data }
}

# 전 페이지 listing. 실패한 page / 읽을 수 없는 응답 / 상한 초과 → $null (partial scan 금지)
function Get-GhFullList {
    param([string]$ApiPath)

    $items = New-Object System.Collections.Generic.List[object]

    for ($page = 1; $page -le 100; $page++) {
        $r = Invoke-GhJson "$ApiPath`?per_page=100&page=$page"

        if (-not $r.Ok) {
            return $null
        }

        $arr = @($r.Data | ForEach-Object { $_ })

        foreach ($x in $arr) {
            if ($null -eq $x -or "$($x.id)" -notmatch '^\d+$') {
                return $null
            }

            $items.Add($x)
        }

        if ($arr.Count -lt 100) {
            return ,$items.ToArray()
        }
    }

    return $null
}

function Sort-Ordinal {
    param([object[]]$Items, [scriptblock]$Key)

    $arr = @($Items)

    if ($arr.Count -lt 2) {
        return ,$arr
    }

    $keys = [string[]]@($arr | ForEach-Object { & $Key $_ })
    $vals = [object[]]$arr
    [Array]::Sort([Array]$keys, [Array]$vals, [System.Collections.IComparer][System.StringComparer]::Ordinal)
    return ,$vals
}

function New-StreamItem {
    param([string]$Stream, [string]$Locator, [string]$Id, [string]$Number, $Body, [string]$UpdatedAt)

    $text = ConvertTo-LfText ([string]$Body)

    return [pscustomobject]@{
        Stream = $Stream
        Locator = $Locator
        Id = $Id
        Number = $Number
        Text = $text
        Digest = Get-Sha256Hex $text
        Marker = Get-AuthorityMarker $text
        UpdatedAt = $UpdatedAt
    }
}

# --- host manifest: designation only ---
$manifestDoc = $null

if (Test-Path -LiteralPath $sourceManifestPath) {
    $sourceManifestRel = Get-HostRelativePath $sourceManifestPath
    $manifestBytes = [System.IO.File]::ReadAllBytes($sourceManifestPath)
    $sha = [System.Security.Cryptography.SHA256]::Create()
    $sourceManifestSha = (([System.BitConverter]::ToString($sha.ComputeHash($manifestBytes))) -replace '-', '').ToLower()
    $sha.Dispose()

    try {
        $manifestDoc = (ConvertTo-CanonicalText ($utf8.GetString($manifestBytes))) | ConvertFrom-Json
    }
    catch {
        $manifestDoc = $null
        Add-PacketHold "SOURCE_MANIFEST_INVALID:UNREADABLE_JSON"
    }

    if ($manifestDoc) {
        foreach ($k in @($manifestDoc.PSObject.Properties | ForEach-Object { $_.Name })) {
            if ($k -notin @("pr", "note", "designated_streams")) {
                Add-PacketHold "SOURCE_MANIFEST_INVALID:UNKNOWN_KEY:$k"
            }
        }

        if ("$($manifestDoc.pr)" -ne "$PrNumber") {
            Add-PacketHold "SOURCE_MANIFEST_INVALID:PR_MISMATCH" "manifest_pr=$($manifestDoc.pr)"
        }

        $declared = @($manifestDoc.designated_streams | Where-Object { $null -ne $_ })

        if ($declared.Count -eq 0) {
            Add-PacketHold "SOURCE_MANIFEST_INVALID:NO_DESIGNATED_STREAMS"
        }

        foreach ($d in $declared) {
            $type = [string]$d.type
            $num = if ($type -in @("issue_comments", "issue_body")) { "$($d.issue)" } else { "$($d.pr)" }

            if ($type -notin @("issue_comments", "issue_body", "pr_body", "pr_reviews", "pr_review_comments") -or $num -notmatch '^\d+$') {
                Add-PacketHold "SOURCE_MANIFEST_INVALID:BAD_DESIGNATED_STREAM:${type}:$num"
                continue
            }

            # watermark 등 다른 필드는 무시 (scan provenance 는 host 가 스스로 기록한다)
            $designatedStreams.Add([pscustomobject]@{ Type = $type; Number = [int64]$num })
        }
    }
}

# a duplicate inside the host manifest is a manifest error; the streams the Host adds below are de-duplicated
$manifestStreamKeys = New-Object "System.Collections.Generic.HashSet[string]" ([System.StringComparer]::Ordinal)

foreach ($ds in $designatedStreams) {
    if (-not $manifestStreamKeys.Add("$($ds.Type):$($ds.Number)")) {
        Add-PacketHold "SOURCE_MANIFEST_INVALID:DUPLICATE_STREAM:$($ds.Type):$($ds.Number)"
    }
}

function Add-ScannedStream {
    param([string]$Type, [int64]$Number)

    if ($manifestStreamKeys.Add("${Type}:$Number")) {
        $designatedStreams.Add([pscustomobject]@{ Type = $Type; Number = $Number })
    }
}

# this PR's own four streams are always scanned
foreach ($type in @("issue_comments", "pr_body", "pr_reviews", "pr_review_comments")) {
    Add-ScannedStream -Type $type -Number ([int64]$PrNumber)
}

# the slice declaration: the PR body (read ONCE, here; the pr_body stream below reuses this read) + the host slice
# spec / remediation authorization
$declarationText = ""
$prBodyResp = Invoke-GhJson "repos/$repoSlug/pulls/$PrNumber"

if (-not $prBodyResp.Ok -or -not $prBodyResp.Data -or "$($prBodyResp.Data.number)" -ne "$PrNumber") {
    Add-PacketHold "DECLARATION_UNREADABLE:pr_body:$PrNumber"
}
else {
    $declarationText = (ConvertTo-LfText ([string]$prBodyResp.Data.body)) + "`n" + (ConvertTo-LfText $authorizationBlock)
}

$evidenceRefs = Get-EvidenceReferences $declarationText

foreach ($n in $evidenceRefs.Issues) {
    Add-ScannedStream -Type "issue_comments" -Number ([int64]$n)
}

$designatedStreams = Sort-Ordinal -Items $designatedStreams.ToArray() -Key { "$($args[0].Type)`t$("{0:D20}" -f $args[0].Number)" }

# --- full, edit-aware re-scan of every designated stream (all pages, current bodies) ---
if ($packetHoldReasons.Count -eq 0) {
    foreach ($ds in $designatedStreams) {
        $sk = "$($ds.Type):$($ds.Number)"
        $count = 0
        $maxId = "0"

        switch ($ds.Type) {
            "pr_body" {
                # This PR's body was read once, above, as the declaration. The packet source is that same read:
                # the citations and the body the auditors see can never come from two different versions.
                $r = if ("$($ds.Number)" -eq "$PrNumber" -and $prBodyResp.Ok) { $prBodyResp } else { Invoke-GhJson "repos/$repoSlug/pulls/$($ds.Number)" }

                if (-not $r.Ok -or -not $r.Data -or "$($r.Data.number)" -ne "$($ds.Number)") {
                    Add-PacketHold "STREAM_UNREADABLE:$sk"
                    break
                }

                $streamItems.Add((New-StreamItem -Stream $sk -Locator "github_pr_body:$($ds.Number)" -Id "$($ds.Number)" -Number "$($ds.Number)" -Body $r.Data.body -UpdatedAt ([string]$r.Data.updated_at)))
                $count = 1
            }
            "issue_body" {
                $r = Invoke-GhJson "repos/$repoSlug/issues/$($ds.Number)"

                if (-not $r.Ok -or -not $r.Data -or "$($r.Data.number)" -ne "$($ds.Number)") {
                    Add-PacketHold "STREAM_UNREADABLE:$sk"
                    break
                }

                $streamItems.Add((New-StreamItem -Stream $sk -Locator "github_issue_body:$($ds.Number)" -Id "$($ds.Number)" -Number "$($ds.Number)" -Body $r.Data.body -UpdatedAt ([string]$r.Data.updated_at)))
                $count = 1
            }
            "issue_comments" {
                $meta = Invoke-GhJson "repos/$repoSlug/issues/$($ds.Number)"
                $list = Get-GhFullList "repos/$repoSlug/issues/$($ds.Number)/comments"

                if (-not $meta.Ok -or $null -eq $list) {
                    Add-PacketHold "STREAM_UNREADABLE:$sk"
                    break
                }

                if ("$($meta.Data.comments)" -notmatch '^\d+$' -or [int]$meta.Data.comments -ne @($list).Count) {
                    Add-PacketHold "STREAM_TRUNCATED:$sk" "listed=$(@($list).Count) expected=$($meta.Data.comments)"
                    break
                }

                foreach ($it in $list) {
                    $streamItems.Add((New-StreamItem -Stream $sk -Locator "github_issue_comment:$($it.id)" -Id "$($it.id)" -Number "$($ds.Number)" -Body $it.body -UpdatedAt ([string]$it.updated_at)))
                }

                $count = @($list).Count
            }
            "pr_review_comments" {
                $meta = Invoke-GhJson "repos/$repoSlug/pulls/$($ds.Number)"
                $list = Get-GhFullList "repos/$repoSlug/pulls/$($ds.Number)/comments"

                if (-not $meta.Ok -or $null -eq $list) {
                    Add-PacketHold "STREAM_UNREADABLE:$sk"
                    break
                }

                if ("$($meta.Data.review_comments)" -notmatch '^\d+$' -or [int]$meta.Data.review_comments -ne @($list).Count) {
                    Add-PacketHold "STREAM_TRUNCATED:$sk" "listed=$(@($list).Count) expected=$($meta.Data.review_comments)"
                    break
                }

                foreach ($it in $list) {
                    $streamItems.Add((New-StreamItem -Stream $sk -Locator "github_pr_review_comment:$($it.id)" -Id "$($it.id)" -Number "$($ds.Number)" -Body $it.body -UpdatedAt ([string]$it.updated_at)))
                }

                $count = @($list).Count
            }
            "pr_reviews" {
                $list = Get-GhFullList "repos/$repoSlug/pulls/$($ds.Number)/reviews"

                if ($null -eq $list) {
                    Add-PacketHold "STREAM_UNREADABLE:$sk"
                    break
                }

                foreach ($it in $list) {
                    $streamItems.Add((New-StreamItem -Stream $sk -Locator "github_pr_review:$($ds.Number)/$($it.id)" -Id "$($it.id)" -Number "$($ds.Number)" -Body $it.body -UpdatedAt ([string]$it.submitted_at)))
                }

                $count = @($list).Count
            }
        }

        foreach ($it in @($streamItems | Where-Object { $_.Stream -eq $sk })) {
            if ([int64]$it.Id -gt [int64]$maxId) { $maxId = $it.Id }
        }

        $scanProvenance.Add([ordered]@{ stream = $sk; items = $count; watermark_max_id = $maxId })
    }
}

# --- referenced-evidence discovery (AGENT_HOST_AUDIT_PROTOCOL §3, §4; ADR-0022 §5) ---
#
# A scanned source is a packet source when, and only when, the slice declaration cites its id. The Host applies that rule
# mechanically: it never judges relevance, never reads a "scope: PR #<N>" record, and never asks anyone to classify.
# Every packet source is required: both auditors must report it in EVIDENCE_SEEN by its content-bound identity.
# A citation names its kind (`<id>` a conversation comment, `review:<id>`, `review-comment:<id>`), and resolves only to a
# source of that kind: the key is "<locator kind>:<id>", never the id alone.
$citedKeys = New-Object "System.Collections.Generic.HashSet[string]" ([System.StringComparer]::Ordinal)

foreach ($k in $evidenceRefs.Keys) {
    [void]$citedKeys.Add($k)
}

$resolvedKeys = New-Object "System.Collections.Generic.HashSet[string]" ([System.StringComparer]::Ordinal)
$seenLocators = New-Object "System.Collections.Generic.HashSet[string]" ([System.StringComparer]::Ordinal)

# The declaration is audit input itself: what the slice says it does, does not do and relies on. The PR body is always a
# required source, so both auditors read it and an edited body is a new audit identity.
foreach ($it in @($streamItems | Where-Object { $_.Locator -eq "github_pr_body:$PrNumber" })) {
    [void]$seenLocators.Add($it.Locator)
    $packetSources.Add([pscustomobject]@{
        Identity = "$($it.Locator)@$($it.Digest)"
        Locator = $it.Locator
        Digest = $it.Digest
        Kind = $(if ($it.Marker) { $it.Marker } else { "UNMARKED" })
        Class = "required"
        Required = $true
        Origin = "declaration"
        Record = "pr-body"
        Text = $it.Text
        TextSha = $it.Digest
        Bytes = $utf8Out.GetByteCount($it.Text)
        UpdatedAt = $it.UpdatedAt
    })
}

foreach ($it in $streamItems) {
    $citeKey = "$(($it.Locator -split ":", 2)[0]):$($it.Id)"

    if (-not $citedKeys.Contains($citeKey)) {
        continue
    }

    [void]$resolvedKeys.Add($citeKey)

    # the same source can sit in two scanned streams (a manifest stream and an issue the declaration names)
    if (-not $seenLocators.Add($it.Locator)) {
        continue
    }

    $packetSources.Add([pscustomobject]@{
        Identity = "$($it.Locator)@$($it.Digest)"
        Locator = $it.Locator
        Digest = $it.Digest
        Kind = $(if ($it.Marker) { $it.Marker } else { "UNMARKED" })
        Class = "required"
        Required = $true
        Origin = "referenced"
        Record = "pr-body"
        Text = $it.Text
        TextSha = $it.Digest
        Bytes = $utf8Out.GetByteCount($it.Text)
        UpdatedAt = $it.UpdatedAt
    })
}

# Canonical documents, read at the AUDITED BASE ($mainHead): the canon that binds before this slice. What the slice
# changes in it is in the diff, so a slice never rewrites the canon it is judged against.
#   baseline   : Get-BaselineCanon, carried by every packet whatever the declaration cites. One that is not at the base
#                is recorded as absent in the packet header; it is never silently skipped.
#   referenced : `canon:<path>` citations of the declaration. One that is not at the base is a TECHNICAL_HOLD.
# Identity is content-bound by the git blob SHA, so a changed document is a new packet.
$unresolvedCanon = New-Object System.Collections.Generic.List[string]
$baselineAbsent = New-Object System.Collections.Generic.List[string]
$canonWanted = New-Object System.Collections.Generic.List[object]

foreach ($cp in (Get-BaselineCanon)) {
    $canonWanted.Add([pscustomobject]@{ Path = $cp; Origin = "baseline" })
}

foreach ($cp in $evidenceRefs.Canon) {
    $canonWanted.Add([pscustomobject]@{ Path = $cp; Origin = "referenced" })
}

foreach ($cw in $canonWanted) {
    $cp = $cw.Path
    $locator = "git_blob:base:$cp"

    if ($seenLocators.Contains($locator)) {
        continue
    }

    $blobSha = "$(Invoke-Git @('-C', $repo, 'rev-parse', '--verify', '--quiet', "${mainHead}:$cp"))".Trim()
    $kind = if ($blobSha -match '^[0-9a-f]{40}$') { "$(Invoke-Git @('-C', $repo, 'cat-file', '-t', $blobSha))".Trim() } else { "" }

    if ($kind -ne "blob") {
        if ($cw.Origin -eq "baseline") { $baselineAbsent.Add($cp) } else { $unresolvedCanon.Add("canon:$cp") }
        continue
    }

    $canonText = ConvertTo-LfText ((@(Invoke-Git @('-C', $repo, 'cat-file', 'blob', $blobSha)) -join "`n") + "`n")
    [void]$seenLocators.Add($locator)

    $packetSources.Add([pscustomobject]@{
        Identity = "$locator@$blobSha"
        Locator = $locator
        Digest = $blobSha
        Kind = "CANON"
        Class = "required"
        Required = $true
        Origin = $cw.Origin
        Record = $(if ($cw.Origin -eq "baseline") { "host-baseline" } else { "pr-body" })
        Text = $canonText
        TextSha = Get-Sha256Hex $canonText
        Bytes = $utf8Out.GetByteCount($canonText)
        UpdatedAt = ""
    })
}

# A citation is declared evidence. One that no scanned stream holds (a wrong id, an issue the declaration does not
# name, a source deleted since) cannot be read: hard completeness fails, as a TECHNICAL_HOLD. It is never dropped.
$unresolvedRefs = @(@($evidenceRefs.Keys | Where-Object { -not $resolvedKeys.Contains($_) } | ForEach-Object { $evidenceRefs.Labels[$_] }) + @($unresolvedCanon))

if ($packetHoldReasons.Count -eq 0) {
    foreach ($label in $unresolvedRefs) {
        Add-PacketHold "CITED_SOURCE_UNRESOLVED:$label" "the declaration cites $label and it cannot be read: no scanned stream holds a source of that kind with that id, or no such file is at the audited base"
    }
}

# provenance only (scan.json, outside the packet bytes): what is marked and not cited
$markedUncited = @($streamItems | Where-Object { $_.Marker -and -not $seenLocators.Contains($_.Locator) })

foreach ($ps in $packetSources) {
    if ($ps.Text.Contains("[/SOURCE identity=")) {
        Add-PacketHold "SOURCE_MARKER_COLLISION:$($ps.Locator)" "body contains the packet source terminator"
    }
}

$packetSources = Sort-Ordinal -Items $packetSources.ToArray() -Key { $args[0].Identity }
$requiredSourceIds = @($packetSources | Where-Object { $_.Required } | ForEach-Object { $_.Identity })

function Get-SourceSection {
    param($Source)

    return "[SOURCE identity=$($Source.Identity) kind=$($Source.Kind) class=$($Source.Class)]`n$($Source.Text)`n[/SOURCE identity=$($Source.Identity)]"
}

$packetDir = Join-Path $stateDir "packets"
New-Item -ItemType Directory -Force -Path $packetDir | Out-Null
$currentPtrPath = Join-Path $packetDir "pr-$PrNumber-head-$($prHead.Substring(0,12)).current"

# scan provenance (watermark, updated_at): packet bytes 밖, 매 생성마다 덮어쓴다
$scanPath = Join-Path $packetDir "pr-$PrNumber-head-$($prHead.Substring(0,12)).scan.json"
$scanDoc = [ordered]@{
    note = "scan provenance only (AGENT_HOST_AUDIT_PROTOCOL §3/§4): never a skip boundary, never part of the canonical packet bytes"
    scanned_at = $scanStartedAt
    streams = @($scanProvenance | ForEach-Object { $_ })
    marked_sources = @($streamItems | Where-Object { $_.Marker } | ForEach-Object { [ordered]@{ locator = $_.Locator; marker = $_.Marker; digest = $_.Digest; updated_at = $_.UpdatedAt } })
    sources = @($packetSources | ForEach-Object { [ordered]@{ identity = $_.Identity; updated_at = $_.UpdatedAt } })
    cited_issues = @($evidenceRefs.Issues)
    unresolved_references = @($unresolvedRefs)
    marked_not_cited = @($markedUncited | ForEach-Object { [ordered]@{ locator = $_.Locator; marker = $_.Marker; digest = $_.Digest } })
    hold = @($packetHoldReasons)
}
[System.IO.File]::WriteAllText($scanPath, ($scanDoc | ConvertTo-Json -Depth 6), $utf8Out)

# hard completeness 실패 → TECHNICAL_HOLD (§4.1, §5.1): the Host retries by itself; nobody is asked to do anything.
# 캐시가 아닌 host 결과만 남기고, 이 HEAD 의 current packet 포인터를 무효화한다.
function Stop-PacketHold {
    $holdPath = Join-Path $stateDir "$auditPolicyVersion-pr-$PrNumber-main-$($mainHead.Substring(0,12))-head-$($prHead.Substring(0,12))-packet-hold.result.txt"

    $lines = @(
        "AUDIT_HEAD=$prHead"
        "VERDICT=HOLD"
        "SUMMARY=PACKET_HOLD: $($packetHoldReasons -join '; ')"
        "PACKET_DIGEST=NONE"
        "HOLD_CLASS=TECHNICAL_HOLD"
    )

    foreach ($d in $packetHoldDetails) {
        $lines += "HOLD_REASON=$d"
    }

    [System.IO.File]::WriteAllText($holdPath, ($lines -join "`r`n") + "`r`n", $utf8Out)
    Add-ToolStamp -Path $holdPath -Auditor "HOST" -Policy $auditPolicyVersion
    Remove-Item -LiteralPath $currentPtrPath -Force -ErrorAction SilentlyContinue

    foreach ($d in $packetHoldDetails) {
        Write-Host "PACKET_HOLD_DETAIL=$d"
    }

    Write-Output "PACKET_COMPLETE=False"

    foreach ($r in $packetHoldReasons) {
        Write-Output "PACKET_HOLD_REASON=$r"
    }

    Write-Output "PACKET_HOLD_RESULT=$holdPath"
    Write-Output "HOLD_CLASS=TECHNICAL_HOLD"
    Write-Output "AUDIT_HOLD=$($packetHoldReasons[0])"
    Write-Output "AUDIT_BLOCKED=$($packetHoldReasons[0])"
}

Write-Host "SOURCE_MANIFEST : $sourceManifestRel (optional stream designation; no classification)"
Write-Host "SOURCES         : $(@($packetSources).Count) cited by the slice declaration (all required)"
Write-Host "MARKED_NOT_CITED: $(@($markedUncited).Count) (provenance only; never a hold)"
Write-Host "UNRESOLVED_REFS : $(@($unresolvedRefs).Count) (citations no scanned stream holds; each is a TECHNICAL_HOLD)"
Write-Host "SCAN_PROVENANCE : $scanPath"

foreach ($sp in $scanProvenance) {
    Write-Host "SCANNED_STREAM  : $($sp.stream) items=$($sp.items) watermark_max_id=$($sp.watermark_max_id)"
}

if ($packetHoldReasons.Count -gt 0) {
    Stop-PacketHold
    return
}

# -------------------------------------------------
# 6. Packet 구성
#
# 32K 이하: 단일 packet.
# 32K 초과: 파일 경계 segment(≤20K, 큰 파일은 PART로 분할 — 절대 자르지 않음)
#           → segment들을 AI call(≤42K) 단위로 묶는다.
# 모든 changed file이 packet에 포함되지 않으면 PASS 금지 → INSUFFICIENT.
# -------------------------------------------------

$maxPacketDiffChars = 32000
$segmentSoftLimit = 20000
$auditCallCharLimit = 42000
# 한 파일의 PART 들은 같은 call 에 둔다 (파일이 여러 call 에 흩어지면 감사자가 전체를 판단할 수 없음 → INSUFFICIENT).
# 그 경우에만 call 한도를 이 상한까지 넘길 수 있다.
$splitFileCallHardLimit = 150000

$useSegmentedAudit = ($diffText.Length -gt $maxPacketDiffChars)

function Split-DiffText {
    param(
        [string]$Text,
        [int]$Limit
    )

    $chunks = New-Object System.Collections.Generic.List[string]
    $sb = New-Object System.Text.StringBuilder

    # 줄 단위로 나누되 줄바꿈을 보존한다 (재조립 시 원문과 동일해야 함)
    $lines = [regex]::Split($Text, "(?<=`n)")

    foreach ($line in $lines) {
        if ($line.Length -eq 0) {
            continue
        }

        if ($sb.Length -gt 0 -and ($sb.Length + $line.Length) -gt $Limit) {
            $chunks.Add($sb.ToString())
            [void]$sb.Clear()
        }

        $rest = $line

        while ($rest.Length -gt $Limit) {
            $chunks.Add($rest.Substring(0, $Limit))
            $rest = $rest.Substring($Limit)
        }

        [void]$sb.Append($rest)
    }

    if ($sb.Length -gt 0) {
        $chunks.Add($sb.ToString())
    }

    return ,$chunks
}

# piece = (file, part k/n, text)
$pieces = New-Object System.Collections.Generic.List[object]

foreach ($fd in $fileDiffs) {
    $chunks = Split-DiffText -Text $fd.Text -Limit ($segmentSoftLimit - 2000)

    if ($chunks.Count -eq 0) {
        $chunks = @("")
    }

    for ($i = 0; $i -lt $chunks.Count; $i++) {
        $pieces.Add([pscustomobject]@{
            File = $fd.File
            Part = $i + 1
            Parts = $chunks.Count
            Text = $chunks[$i]
        })
    }
}

# pieces → segments (≤ segmentSoftLimit)
$segments = New-Object System.Collections.Generic.List[object]
$current = New-Object System.Collections.Generic.List[object]
$currentChars = 0

foreach ($p in $pieces) {
    if ($current.Count -gt 0 -and ($currentChars + $p.Text.Length) -gt ($segmentSoftLimit - 2000)) {
        $segments.Add(@($current.ToArray()))
        $current = New-Object System.Collections.Generic.List[object]
        $currentChars = 0
    }

    $current.Add($p)
    $currentChars += $p.Text.Length
}

if ($current.Count -gt 0) {
    $segments.Add(@($current.ToArray()))
}

function New-SegmentText {
    param(
        [object[]]$SegPieces,
        [int]$Index,
        [int]$Total
    )

    $fileLines = foreach ($p in $SegPieces) {
        if ($p.Parts -gt 1) {
            "$($p.File) [PART $($p.Part)/$($p.Parts)]"
        }
        else {
            $p.File
        }
    }

    $body = foreach ($p in $SegPieces) {
        $text = if ($p.Parts -gt 1) {
            "--- FILE PART $($p.Part)/$($p.Parts): $($p.File) (continues in next part; not truncated) ---`r`n" + $p.Text
        }
        else {
            $p.Text
        }

        # 파일 diff의 끝을 명시 → 빈 문맥 줄로 끝나는 hunk를 잘림으로 오판하지 않게 한다.
        if ($p.Part -eq $p.Parts) {
            $text = $text + "`r`n--- END OF FILE DIFF: $($p.File) (complete) ---"
        }

        $text
    }

    return (@(
        "ICBM SEGMENT AUDIT PACKET"
        "POLICY_VERSION=$auditPolicyVersion"
        "SEGMENT=SEG$Index/$Total"
        "PR=$PrNumber"
        "MAIN=$mainHead"
        "HEAD=$prHead"
        ""
        "FILES:"
        ($fileLines -join "`r`n")
        ""
        "DIFF:"
        ($body -join "`r`n")
    ) -join "`r`n")
}

# The full changed-file manifest is placed ONCE in the canonical packet (section 6b). A segmented audit call
# carries only its content-bound reference (count + sha256 of the LF-normalized list) and lists its own files
# under FILES: in each segment. Repeating the full list in every call let the common part alone exceed
# $auditCallCharLimit on a large PR, so every call failed CALL_OVER_LIMIT although each segment was within limits.
# Coverage stays host-verified: section 7 reassembles every changed file from the calls, byte for byte.
$changedFilesCanonical = ConvertTo-CanonicalText $changedFilesText
$changedFilesDigest = Get-Sha256Hex $changedFilesCanonical
$changedFilesSection = "=== CHANGED FILE MANIFEST ($($prFiles.Count) files, sha256=$changedFilesDigest) ===`n$changedFilesCanonical`n=== END CHANGED FILE MANIFEST ==="
$callManifestBlock = $authorizationBlock + @"
CHANGED FILE MANIFEST (GitHub PR files API, $($prFiles.Count) files, sha256=$changedFilesDigest of the LF-normalized list): the full list is in the canonical audit packet, and the host has verified that the audit calls together contain every file completely. The files of this call are listed under FILES: in its segments.
"@

# audit calls: 각 call = packet text + 포함 piece 목록
$auditCalls = New-Object System.Collections.Generic.List[object]

if (-not $useSegmentedAudit) {
    # canonical packet 에 들어가므로 HEAD 와 무관하게 바뀌는 값(TITLE, BRANCH, 시점별 CI 집계)은 넣지 않는다 (V2 §4.2)
    $packet = @"
ICBM EXACT-HEAD AUDIT PACKET
POLICY_VERSION=$auditPolicyVersion

PR=$PrNumber

CANONICAL_MAIN=$mainHead
EXACT_PR_HEAD=$prHead

$authorizationBlock
AUDIT_WORKTREE_HEAD_MATCH=YES
AUDIT_WORKTREE=CLEAN


CHANGED_FILES:
$changedFilesText


--- EXACT MAIN...HEAD DIFF START ---

$packetDiffText

--- EXACT MAIN...HEAD DIFF END ---
"@

    $auditCalls.Add([pscustomobject]@{
        Index = 1
        Body = (ConvertTo-CanonicalText $packet)
        Text = $null
        Pieces = @($pieces.ToArray())
    })
}
else {
    $segTexts = New-Object System.Collections.Generic.List[object]

    for ($s = 0; $s -lt $segments.Count; $s++) {
        $segTexts.Add([pscustomobject]@{
            Text = New-SegmentText -SegPieces $segments[$s] -Index ($s + 1) -Total $segments.Count
            Pieces = $segments[$s]
        })
    }

    $separator = (
        "`r`n`r`n" +
        "============================================================`r`n" +
        "NEXT AUDIT SEGMENT`r`n" +
        "============================================================`r`n`r`n"
    )

    $groups = New-Object System.Collections.Generic.List[object]
    $group = New-Object System.Collections.Generic.List[object]
    $groupChars = $callManifestBlock.Length

    foreach ($st in $segTexts) {
        $nextChars = $groupChars + $separator.Length + $st.Text.Length
        # 이 segment 가 앞 segment 에서 시작된 파일의 다음 PART 로 시작하면, 상한 안에서는 같은 call 에 붙인다
        $firstPiece = @($st.Pieces)[0]
        $continuesFile = ($firstPiece -and $firstPiece.Part -gt 1)
        $keepTogether = ($continuesFile -and $nextChars -le $splitFileCallHardLimit)

        if ($group.Count -gt 0 -and $nextChars -gt $auditCallCharLimit -and -not $keepTogether) {
            $groups.Add(@($group.ToArray()))
            $group = New-Object System.Collections.Generic.List[object]
            $groupChars = $callManifestBlock.Length
        }

        $group.Add($st)
        $groupChars += $separator.Length + $st.Text.Length
    }

    if ($group.Count -gt 0) {
        $groups.Add(@($group.ToArray()))
    }

    for ($g = 0; $g -lt $groups.Count; $g++) {
        # AUDIT_CALL=i/n 은 call preamble (packet digest 와 함께) 에 둔다
        $callText = $callManifestBlock + $separator + (($groups[$g] | ForEach-Object { $_.Text }) -join $separator)

        $callPieces = @(
            foreach ($st in $groups[$g]) {
                foreach ($p in $st.Pieces) {
                    $p
                }
            }
        )

        $auditCalls.Add([pscustomobject]@{
            Index = $g + 1
            Body = (ConvertTo-CanonicalText $callText)
            Text = $null
            Pieces = $callPieces
        })
    }
}

# -------------------------------------------------
# 6b. Canonical Audit Packet + packet_digest (V2 §4, §4.2)
#
# UTF-8 (BOM 없음), LF only, timestamp/임시 경로/비결정적 순서 없음.
# packet_digest = sha256(canonical bytes). 각 call text = call preamble + 공통 source block + call body,
# source block 과 call body 는 canonical packet 의 byte-identical 부분 문자열이다.
# -------------------------------------------------

$sourceLines = New-Object System.Collections.Generic.List[string]
$sourceLines.Add("AUTHORITATIVE SOURCE MANIFEST (host-generated; AGENT_HOST_AUDIT_PROTOCOL sections 3-4; sources are the durable evidence the slice declaration cites; content-bound identity = <locator>@<body digest>):")
$sourceLines.Add("SOURCE_MANIFEST=$sourceManifestRel")

foreach ($ds in $designatedStreams) {
    $sourceLines.Add("DESIGNATED_STREAM=$($ds.Type):$($ds.Number)")
}

foreach ($ps in $packetSources) {
    $sourceLines.Add("SOURCE=$($ps.Identity) kind=$($ps.Kind) class=$($ps.Class) required=$($ps.Required.ToString().ToLower()) origin=$($ps.Origin) record=$($ps.Record) bytes=$($ps.Bytes)")
}

$sourceLines.Add("BASELINE_CANON=$((Get-BaselineCanon) -join ',')")
$sourceLines.Add("BASELINE_CANON_ABSENT_AT_BASE=$(if ($baselineAbsent.Count -gt 0) { $baselineAbsent -join ',' } else { 'NONE' })")
$sourceLines.Add("REQUIRED_SOURCES=$(if (@($requiredSourceIds).Count -gt 0) { $requiredSourceIds -join ',' } else { 'NONE' })")
$sourceLines.Add("--- AUTHORITATIVE SOURCES START ($(@($packetSources).Count)) ---")

$sourceBlock = ($sourceLines -join "`n")

foreach ($ps in $packetSources) {
    $sourceBlock += "`n" + (Get-SourceSection $ps)
}

$sourceBlock += "`n--- AUTHORITATIVE SOURCES END ---"

$packetHeader = (@(
    "PACKET_FORMAT=icbm-audit-packet-v3"
    "POLICY_VERSION=$auditPolicyVersion"
    "PR=$PrNumber"
    "EXACT_PR_HEAD=$prHead"
    "AUDITED_BASE_SHA=$mainHead"
    "SLICE_SPEC_PATH=$sliceSpecRel"
    "SLICE_SPEC_BLOB=$sliceSpecSha"
    "SLICE_SPEC_ORIGIN=$sliceSpecOrigin"
    "SCOPE_ALLOWLIST=$scopeAllowlist"
    "SCOPE_ALLOWLIST_SHA256=$scopeAllowlistSha"
    "REMEDIATION_AUTHORIZATION=$remediationAuthIdentity"
    "AUDIT_MODE=$(if ($useSegmentedAudit) { 'SEGMENTED' } else { 'SINGLE_PACKET' })"
    "AUDIT_CALLS=$($auditCalls.Count)"
) -join "`n")

$canonicalParts = New-Object System.Collections.Generic.List[string]
$canonicalParts.Add("ICBM CANONICAL AUDIT PACKET`n$packetHeader`n`n$sourceBlock`n")

if ($useSegmentedAudit) {
    $canonicalParts.Add("`n$changedFilesSection`n")
}

foreach ($call in $auditCalls) {
    $canonicalParts.Add("`n=== AUDIT CALL BODY $($call.Index)/$($auditCalls.Count) ===`n$($call.Body)`n")
}

$canonicalParts.Add("`n--- END OF CANONICAL AUDIT PACKET ---`n")
$canonicalPacket = ConvertTo-CanonicalText ($canonicalParts -join "")
$packetDigest = Get-Sha256Hex $canonicalPacket
$packetDigest12 = $packetDigest.Substring(0,12)

foreach ($call in $auditCalls) {
    $call.Text = (
        "ICBM EXACT-HEAD AUDIT CALL`n" +
        "AUDIT_CALL=$($call.Index)/$($auditCalls.Count)`n" +
        "PACKET_DIGEST=$packetDigest`n" +
        "$packetHeader`n`n" +
        "$sourceBlock`n`n" +
        $call.Body
    )
}

# --- source completeness (host, fail-closed, before AI): 모든 call 에 모든 source 가 정확히 1회, 내용 sha 일치 ---
if (-not $canonicalPacket.Contains($sourceBlock)) {
    Add-PacketHold "PACKET_DERIVATION_FAILED:SOURCE_BLOCK"
}

if ($useSegmentedAudit -and -not $canonicalPacket.Contains("`n$changedFilesSection`n")) {
    Add-PacketHold "PACKET_DERIVATION_FAILED:CHANGED_FILE_MANIFEST"
}

foreach ($call in $auditCalls) {
    if (-not $canonicalPacket.Contains($call.Body)) {
        Add-PacketHold "PACKET_DERIVATION_FAILED:CALL$($call.Index)"
    }

    foreach ($ps in $packetSources) {
        $open = "[SOURCE identity=$($ps.Identity) kind=$($ps.Kind) class=$($ps.Class)]`n"
        $close = "`n[/SOURCE identity=$($ps.Identity)]"
        $first = $call.Text.IndexOf($open, [System.StringComparison]::Ordinal)
        $ok = ($first -ge 0 -and $first -eq $call.Text.LastIndexOf($open, [System.StringComparison]::Ordinal))
        $ok = $ok -and ($call.Text.IndexOf($close, [System.StringComparison]::Ordinal) -eq $call.Text.LastIndexOf($close, [System.StringComparison]::Ordinal))

        if ($ok) {
            $start = $first + $open.Length
            $end = $call.Text.IndexOf($close, $start, [System.StringComparison]::Ordinal)
            $ok = ($end -ge $start -and (Get-Sha256Hex $call.Text.Substring($start, $end - $start)) -eq $ps.TextSha)
        }

        if (-not $ok) {
            Add-PacketHold "SOURCE_INCOMPLETE:$($ps.Locator)" "call=$($call.Index) source section missing, duplicated or content mismatch"
        }
    }
}

if ($packetHoldReasons.Count -gt 0) {
    Stop-PacketHold
    return
}

# --- immutable write: 같은 이름이 이미 있으면 byte-identical 이어야 한다 ---
$packetBase = "pr-$PrNumber-head-$($prHead.Substring(0,12))-$packetDigest12"
$packetFile = Join-Path $packetDir "$packetBase.packet.txt"
$packetManifestFile = Join-Path $packetDir "$packetBase.manifest.json"

# manifest.json 은 canonical packet 에서 결정적으로 파생된 값만 담는다 (updated_at / watermark / 시각 없음 → scan.json)
$packetManifestObj = [ordered]@{
    packet_format = "icbm-audit-packet-v3"
    policy_version = $auditPolicyVersion
    pr = $PrNumber
    exact_pr_head = $prHead
    audited_base_sha = $mainHead
    packet_digest = $packetDigest
    slice_spec = [ordered]@{ path = $sliceSpecRel; blob = $sliceSpecSha; origin = $sliceSpecOrigin }
    scope_allowlist = [ordered]@{ serialized = $scopeAllowlist; sha256 = $scopeAllowlistSha }
    remediation_authorization = $remediationAuthIdentity
    source_manifest = $sourceManifestRel
    designated_streams = @($designatedStreams | ForEach-Object { "$($_.Type):$($_.Number)" })
    sources = @($packetSources | ForEach-Object {
        [ordered]@{ identity = $_.Identity; kind = $_.Kind; class = $_.Class; required = $_.Required; origin = $_.Origin; record = $_.Record; bytes = $_.Bytes }
    })
    required = @($requiredSourceIds)
    audit_calls = $auditCalls.Count
}

$packetManifestText = (ConvertTo-CanonicalText ($packetManifestObj | ConvertTo-Json -Depth 8)) + "`n"

function Write-ImmutableFile {
    param([string]$Path, [string]$Text)

    $bytes = $utf8Out.GetBytes($Text)

    if (Test-Path -LiteralPath $Path) {
        $existing = [System.IO.File]::ReadAllBytes($Path)

        if ($existing.Length -ne $bytes.Length) {
            return $false
        }

        for ($i = 0; $i -lt $bytes.Length; $i++) {
            if ($existing[$i] -ne $bytes[$i]) {
                return $false
            }
        }

        return $true
    }

    $tmp = "$Path.tmp-$PID"
    [System.IO.File]::WriteAllBytes($tmp, $bytes)
    Move-Item -LiteralPath $tmp -Destination $Path
    return $true
}

$packetExisted = Test-Path -LiteralPath $packetFile

if (-not (Write-ImmutableFile -Path $packetFile -Text $canonicalPacket) -or
    -not (Write-ImmutableFile -Path $packetManifestFile -Text $packetManifestText)) {
    Remove-Item -LiteralPath $currentPtrPath -Force -ErrorAction SilentlyContinue
    Write-Output "PACKET_DIGEST=$packetDigest"
    Write-Output "PACKET_COMPLETE=False"
    Write-Output "AUDIT_HOLD=PACKET_IMMUTABILITY_VIOLATION"
    Write-Output "AUDIT_BLOCKED=PACKET_IMMUTABILITY_VIOLATION"
    return
}

# 이 HEAD 의 현재 packet 포인터 (orchestrator / repair 가 audit identity 를 찾는 곳). 불변 아님.
$currentPtrText = (@(
    "PR=$PrNumber"
    "HEAD=$prHead"
    "MAIN=$mainHead"
    "POLICY_VERSION=$auditPolicyVersion"
    "PACKET_DIGEST=$packetDigest"
    "SOURCE_MANIFEST_SHA256=$sourceManifestSha"
    "REQUIRED_SOURCES=$($requiredSourceIds -join ',')"
    "PACKET_FILE=$(Get-HostRelativePath $packetFile)"
    "PACKET_MANIFEST=$(Get-HostRelativePath $packetManifestFile)"
) -join "`n") + "`n"

[System.IO.File]::WriteAllText("$currentPtrPath.tmp-$PID", $currentPtrText, $utf8Out)
Move-Item -LiteralPath "$currentPtrPath.tmp-$PID" -Destination $currentPtrPath -Force

Write-Host "PACKET_FILE     : $packetFile ($(if ($packetExisted) { 'EXISTING_BYTE_IDENTICAL' } else { 'WRITTEN' }))"
Write-Output "PACKET_DIGEST=$packetDigest"
Write-Output "PACKET_WRITE=$(if ($packetExisted) { 'EXISTING_BYTE_IDENTICAL' } else { 'WRITTEN' })"
Write-Output "PACKET_SOURCES_REQUIRED=$(if (@($requiredSourceIds).Count -gt 0) { $requiredSourceIds -join ',' } else { 'NONE' })"

foreach ($ps in $packetSources) {
    Write-Output "PACKET_SOURCE=$($ps.Identity) kind=$($ps.Kind) class=$($ps.Class) origin=$($ps.Origin)"
}

# -------------------------------------------------
# 7. Completeness proof - 누락/잘림이 있으면 PASS 금지
# -------------------------------------------------

$reassembled = @{}

foreach ($call in $auditCalls) {
    foreach ($p in $call.Pieces) {
        if (-not $call.Text.Contains($p.Text)) {
            $incompleteReasons.Add("PIECE_NOT_IN_PACKET:$($p.File)#$($p.Part)")
        }

        if (-not $reassembled.ContainsKey($p.File)) {
            $reassembled[$p.File] = New-Object System.Text.StringBuilder
        }

        [void]$reassembled[$p.File].Append($p.Text)
    }

    if ($call.Text.Contains([char]0xFFFD)) {
        $incompleteReasons.Add("UTF8_REPLACEMENT_CHAR:CALL$($call.Index)")
    }

    # 여러 PART 로 나뉜 파일을 한 call 에 모은 경우만 상한(splitFileCallHardLimit)까지 허용
    $holdsSplitFile = @($call.Pieces | Where-Object { $_.Parts -gt 1 }).Count -gt 0
    $callLimit = if ($holdsSplitFile) { $splitFileCallHardLimit } else { $auditCallCharLimit }

    # 한도는 diff material(call body) 기준. 모든 call 에 공통으로 붙는 marked source block 은 제외한다.
    if ($useSegmentedAudit -and $call.Body.Length -gt ($callLimit + 4000)) {
        $incompleteReasons.Add("CALL_OVER_LIMIT:CALL$($call.Index)")
    }
}

$includedFiles = 0

if ($useSegmentedAudit) {
    $callFileSet = New-Object "System.Collections.Generic.HashSet[string]" ([System.StringComparer]::Ordinal)
    foreach ($call in $auditCalls) { foreach ($p in $call.Pieces) { [void]$callFileSet.Add($p.File) } }
    foreach ($f in $prFiles) {
        if (-not $callFileSet.Contains($f.File)) {
            $incompleteReasons.Add("MANIFEST_FILE_NOT_IN_ANY_CALL:$($f.File)")
        }
    }
}

foreach ($fd in $fileDiffs) {
    if (-not $reassembled.ContainsKey($fd.File)) {
        $incompleteReasons.Add("FILE_MISSING_FROM_PACKET:$($fd.File)")
        continue
    }

    if ($reassembled[$fd.File].ToString() -cne $fd.Text) {
        $incompleteReasons.Add("FILE_TRUNCATED_IN_PACKET:$($fd.File)")
        continue
    }

    $includedFiles++
}

$packetComplete = ($incompleteReasons.Count -eq 0)

$manifestPath = Join-Path $logDir "pr-$PrNumber-packet-manifest-$ts.txt"

$manifestLines = @(
    "PR=$PrNumber"
    "MAIN=$mainHead"
    "HEAD=$prHead"
    "POLICY_VERSION=$auditPolicyVersion"
    "AUDIT_MODE=$(if ($useSegmentedAudit) { 'SEGMENTED' } else { 'SINGLE_PACKET' })"
    "DIFF_CHARS=$($diffText.Length)"
    "FILES_TOTAL=$($prFiles.Count)"
    "FILES_INCLUDED=$includedFiles"
    "SEGMENTS=$($segments.Count)"
    "AUDIT_CALLS=$($auditCalls.Count)"
    "PACKET_COMPLETE=$packetComplete"
    "PACKET_DIGEST=$packetDigest"
    "PACKET_FILE=$packetFile"
    "REQUIRED_SOURCES=$($requiredSourceIds -join ',')"
    "AUDIT_TOOL=$script:auditToolName"
    "AUDIT_TOOL_SHA256=$script:auditToolSha"
    "CODEX_CLI=$script:codexCli"
    "CLAUDE_CLI=$script:claudeCli"
)

foreach ($r in $incompleteReasons) {
    $manifestLines += "INCOMPLETE_REASON=$r"
}

foreach ($call in $auditCalls) {
    $callPath = Join-Path $logDir "pr-$PrNumber-packet-call$($call.Index)-$ts.txt"
    [System.IO.File]::WriteAllText($callPath, $call.Text, $utf8Out)
    $manifestLines += "CALL$($call.Index)_CHARS=$($call.Text.Length)"
    $manifestLines += "CALL$($call.Index)_PACKET=$callPath"

    foreach ($p in $call.Pieces) {
        $manifestLines += "CALL$($call.Index)_FILE=$($p.File)#$($p.Part)/$($p.Parts)"
    }
}

[System.IO.File]::WriteAllText($manifestPath, ($manifestLines -join "`r`n"), $utf8Out)

$packetPath = Join-Path $logDir "pr-$PrNumber-packet-call1-$ts.txt"

Write-Host "DIFF CHARS     : $($diffText.Length)"
Write-Host "AUDIT_MODE     : $(if ($useSegmentedAudit) { 'SEGMENTED' } else { 'SINGLE_PACKET' })"
Write-Host "SEGMENTS       : $($segments.Count)"
Write-Host "AUDIT_CALLS    : $($auditCalls.Count)"
Write-Host "PACKET_MANIFEST: $manifestPath"
Write-Output "PACKET_FILES_TOTAL=$($prFiles.Count)"
Write-Output "PACKET_FILES_INCLUDED=$includedFiles"
Write-Output "PACKET_COMPLETE=$packetComplete"

foreach ($r in $incompleteReasons) {
    Write-Output "PACKET_INCOMPLETE_REASON=$r"
}

Write-Host ""

if ($PacketOnly) {
    Write-Output "PACKET_ONLY=TRUE"
    Write-Host "GPT_AUDIT=SKIPPED"
    Write-Host "CLAUDE_AUDIT=SKIPPED"
    return
}

# -------------------------------------------------
# 8. Audit identity + cache key (AGENT_HOST_PROTOCOL_V2 §2, §5.2)
#
# Audit identity = (exact HEAD, packet_digest). main(=audited base) + policy 도 key 에 묶는다.
#   <key>-gpt.txt / <key>-claude.txt           : PASS 캐시 — VERDICT=PASS 이고 evidence_seen ⊇ required 일 때만 host 가 쓴다.
#   <key>-gpt.result.txt / <key>-claude.result.txt : 이번 실행의 결과 (PASS/BLOCKER/INSUFFICIENT/HOLD). 캐시로 읽지 않는다.
# BLOCKER/INSUFFICIENT/HOLD 는 캐시 이름으로 절대 쓰이지 않고, 캐시 판정은 PASS + HEAD + digest + evidence 를 모두 다시 확인한다.
# 과거 packet digest 없는 v6 이하 파일(...-head-<12>-gpt.txt)은 이 key 와 이름이 달라 V2 에서 재사용되지 않는다 (historical evidence 전용).
# -------------------------------------------------

$mainShort = $mainHead.Substring(0,12)
$headShort = $prHead.Substring(0,12)

$cacheKey = "$auditPolicyVersion-pr-$PrNumber-main-$mainShort-head-$headShort-pkt-$packetDigest12"

$gptCache = Join-Path $stateDir "$cacheKey-gpt.txt"
$claudeCache = Join-Path $stateDir "$cacheKey-claude.txt"
$gptResultPath = Join-Path $stateDir "$cacheKey-gpt.result.txt"
$claudeResultPath = Join-Path $stateDir "$cacheKey-claude.result.txt"

# 이번 실행 이전의 non-PASS 결과는 남기지 않는다 (fresh audit; orchestrator 는 이번 실행 결과만 읽는다)
Remove-Item -LiteralPath $gptResultPath, $claudeResultPath -Force -ErrorAction SilentlyContinue

function Write-VerdictFile {
    param(
        [string]$Path,
        [string]$Verdict,
        [string]$Summary,
        [string[]]$Detail = @()
    )

    $lines = @(
        "AUDIT_HEAD=$prHead"
        "VERDICT=$Verdict"
        "SUMMARY=$Summary"
        "PACKET_DIGEST=$packetDigest"
    ) + $Detail

    [System.IO.File]::WriteAllText($Path, ($lines -join "`r`n") + "`r`n", $utf8Out)
}

if (-not $packetComplete) {
    # 누락/잘림 → AI 호출 없이 host가 INSUFFICIENT 확정. PASS 불가. (결과 파일만, 캐시 아님)
    Write-VerdictFile `
        -Path $gptResultPath `
        -Verdict "INSUFFICIENT" `
        -Summary "HOST_PACKET_INCOMPLETE: $($incompleteReasons -join '; ')"

    Add-ToolStamp -Path $gptResultPath -Auditor "HOST" -Policy $auditPolicyVersion

    Remove-Item $gptCache, $claudeCache -Force -ErrorAction SilentlyContinue

    Write-Output "GPT_VERDICT=INSUFFICIENT"
    Write-Output "PIPELINE_STOP=HOST_PACKET_INCOMPLETE"
    Write-Output "AUDIT_RESULT=HOLD"
    return
}

function Read-EvidenceSeen {
    param([string]$Text)

    $m = [regex]::Match($Text, '(?m)^EVIDENCE_SEEN=(.*?)\s*$')

    if (-not $m.Success) {
        return @()
    }

    # content-bound identity 만 인정한다 (V2 §5): <locator>@<body digest | git object SHA>. ID 만 적은 항목은 무시 (cover 하지 않음)
    return @(
        $m.Groups[1].Value -split ',' |
            ForEach-Object { $_.Trim() } |
            Where-Object { $_ -cmatch '^(github_issue_comment:\d+|github_pr_review:\d+/\d+|github_pr_review_comment:\d+|github_issue_body:\d+|github_pr_body:\d+|git_blob:(HEAD|base|[0-9a-f]{40}):[^@\s,]+)@([0-9a-f]{40}|[0-9a-f]{64})$' }
    )
}

function Get-MissingEvidence {
    param([string[]]$Seen)

    $set = New-Object "System.Collections.Generic.HashSet[string]" ([System.StringComparer]::Ordinal)

    foreach ($s in @($Seen)) {
        [void]$set.Add($s)
    }

    return @($requiredSourceIds | Where-Object { -not $set.Contains($_) })
}

function Read-VerdictText {
    param([string]$Text)

    $verdictMatch = [regex]::Match(
        $Text,
        "(?m)^VERDICT=(PASS|BLOCKER|INSUFFICIENT|HOLD|HUMAN_DECISION_REQUIRED)\s*$"
    )

    $summaryMatch = [regex]::Match(
        $Text,
        "(?m)^SUMMARY=(.+?)\s*$"
    )

    $seen = @(Read-EvidenceSeen $Text)

    [pscustomobject]@{
        Valid = ($Text -match "AUDIT_HEAD=$prHead" -and $verdictMatch.Success)
        Verdict = if ($verdictMatch.Success) { $verdictMatch.Groups[1].Value } else { "" }
        Summary = if ($summaryMatch.Success) { $summaryMatch.Groups[1].Value.Trim() } else { "" }
        EvidenceSeen = $seen
        Missing = @(Get-MissingEvidence $seen)
    }
}

# 한 call 의 auditor 출력 → evidence coverage 적용 (V2 §5).
# 모든 call 이 모든 source 를 받으므로 각 call 의 EVIDENCE_SEEN 은 manifest.required 를 모두 포함해야 한다.
# 빠진 id 가 있으면 그 call 은 PASS 가 아니다 → HOLD (EVIDENCE_NOT_SEEN). BLOCKER 도 근거를 읽지 않았으면 HOLD.
function Read-CallVerdict {
    param([string]$Text, [int]$Index)

    $r = Read-VerdictText -Text $Text
    $r | Add-Member -NotePropertyName Index -NotePropertyValue $Index

    if ($r.Valid -and @($r.Missing).Count -gt 0) {
        $r.Summary = "EVIDENCE_NOT_SEEN:$(@($r.Missing) -join ',') (auditor verdict was $($r.Verdict): $($r.Summary))"
        $r.Verdict = "HOLD"
    }
    elseif ($r.Valid -and $r.Verdict -eq "HUMAN_DECISION_REQUIRED" -and -not (Get-HumanDecisionCategory $r.Summary)) {
        # §5.1: only a category of the closed list is the user's. An auditor that names none has not said what the
        # user should decide, so this is a technical hold and the audit is run again.
        $r.Summary = "HUMAN_DECISION_WITHOUT_CATEGORY (auditor summary: $($r.Summary))"
        $r.Verdict = "HOLD"
    }

    return $r
}

# 여러 call 결과는 fail-closed로 합친다.
# HUMAN_DECISION_REQUIRED 우선, HOLD(근거 미확인 = TECHNICAL_HOLD), 그 다음 BLOCKER, 형식 오류 → INSUFFICIENT,
# 모든 call PASS일 때만 PASS. HOLD 와 INSUFFICIENT 는 TECHNICAL_HOLD 이다 (§5.1): the Host re-audits, nobody is asked.
# EVIDENCE_SEEN(aggregate) = 모든 call 이 공통으로 확인한 id (교집합).
function Merge-CallVerdicts {
    param(
        [object[]]$Results,
        [string]$Path
    )

    $invalid = @($Results | Where-Object { -not $_.Valid })
    $human = @($Results | Where-Object { $_.Valid -and $_.Verdict -eq "HUMAN_DECISION_REQUIRED" })
    $holds = @($Results | Where-Object { $_.Valid -and $_.Verdict -eq "HOLD" })
    $blockers = @($Results | Where-Object { $_.Valid -and $_.Verdict -eq "BLOCKER" })
    $insufficient = @($Results | Where-Object { $_.Valid -and $_.Verdict -eq "INSUFFICIENT" })

    if ($human.Count -gt 0) {
        $verdict = "HUMAN_DECISION_REQUIRED"
        $summary = ($human | ForEach-Object { "[CALL$($_.Index)] $($_.Summary)" }) -join " | "
    }
    elseif ($holds.Count -gt 0) {
        $verdict = "HOLD"
        $summary = ($holds | ForEach-Object { "[CALL$($_.Index)] $($_.Summary)" }) -join " | "
    }
    elseif ($blockers.Count -gt 0) {
        $verdict = "BLOCKER"
        $summary = ($blockers | ForEach-Object { "[CALL$($_.Index)] $($_.Summary)" }) -join " | "
    }
    elseif ($invalid.Count -gt 0) {
        $verdict = "INSUFFICIENT"
        $summary = "HOST_AGGREGATE: invalid auditor output in call(s) " + (($invalid | ForEach-Object { $_.Index }) -join ",")
    }
    elseif ($insufficient.Count -gt 0) {
        $verdict = "INSUFFICIENT"
        $summary = ($insufficient | ForEach-Object { "[CALL$($_.Index)] $($_.Summary)" }) -join " | "
    }
    elseif ($Results.Count -eq 1) {
        $verdict = "PASS"
        $summary = $Results[0].Summary
    }
    else {
        $verdict = "PASS"
        $summary = "All $($Results.Count) audit calls PASS: " + (($Results | ForEach-Object { $_.Summary }) -join " | ")
    }

    if ($Results.Count -eq 1 -and $verdict -ne "PASS" -and $Results[0].Valid) {
        $summary = $Results[0].Summary
    }

    # aggregate evidence_seen = 교집합 (canonical source 순서)
    $common = @(
        foreach ($ps in $packetSources) {
            $id = $ps.Identity
            $inAll = $true

            foreach ($r in $Results) {
                if (@($r.EvidenceSeen) -cnotcontains $id) {
                    $inAll = $false
                }
            }

            if ($inAll) {
                $id
            }
        }
    )

    $covered = (@(Get-MissingEvidence $common).Count -eq 0)

    $detail = @(
        "EVIDENCE_REQUIRED=$(if (@($requiredSourceIds).Count -gt 0) { $requiredSourceIds -join ',' } else { 'NONE' })"
        "EVIDENCE_SEEN=$(if ($common.Count -gt 0) { $common -join ',' } else { 'NONE' })"
        "EVIDENCE_COVERED=$(if ($covered) { 'YES' } else { 'NO' })"
    )

    foreach ($r in $Results) {
        $detail += "CALL$($r.Index)_VERDICT=$($r.Verdict)"
        $detail += "CALL$($r.Index)_EVIDENCE_SEEN=$(if (@($r.EvidenceSeen).Count -gt 0) { @($r.EvidenceSeen) -join ',' } else { 'NONE' })"
    }

    Write-VerdictFile -Path $Path -Verdict $verdict -Summary $summary -Detail $detail
}

# PASS 캐시 판정: 파일이 있고, 같은 HEAD, 같은 packet digest, VERDICT=PASS, EVIDENCE_SEEN ⊇ required.
# 하나라도 어긋나면 캐시가 아니다 (그 파일은 지운다 — 다음 결과는 fresh audit).
function Test-PassCache {
    param([string]$Path)

    if (-not (Test-Path -LiteralPath $Path)) {
        return $false
    }

    $t = Get-Content -Raw -Encoding utf8 -LiteralPath $Path

    $ok = (
        $t -match "(?m)^AUDIT_HEAD=$prHead\s*$" -and
        $t -match "(?m)^PACKET_DIGEST=$packetDigest\s*$" -and
        $t -match '(?m)^VERDICT=PASS\s*$' -and
        @(Get-MissingEvidence @(Read-EvidenceSeen $t)).Count -eq 0
    )

    if (-not $ok) {
        Remove-Item -LiteralPath $Path -Force -ErrorAction SilentlyContinue
    }

    return $ok
}

# 이번 실행 결과를 결과 파일에 기록하고, PASS + evidence coverage 일 때만 캐시 파일로 복사한다.
function Publish-AuditResult {
    param([string]$ResultPath, [string]$CachePath, [string]$Auditor)

    Add-ToolStamp -Path $ResultPath -Auditor $Auditor -Policy $auditPolicyVersion

    $t = Get-Content -Raw -Encoding utf8 -LiteralPath $ResultPath
    $pass = (
        $t -match '(?m)^VERDICT=PASS\s*$' -and
        $t -match "(?m)^PACKET_DIGEST=$packetDigest\s*$" -and
        @(Get-MissingEvidence @(Read-EvidenceSeen $t)).Count -eq 0
    )

    if ($pass) {
        Copy-Item -LiteralPath $ResultPath -Destination $CachePath -Force
    }
    else {
        Remove-Item -LiteralPath $CachePath -Force -ErrorAction SilentlyContinue
    }
}

$multiCallNote = @"
- The CHANGED FILE MANIFEST identifies every changed file of this PR. A single-packet audit lists them all; a multi-call audit gives their count and sha256 here, keeps the full list in the canonical packet, and lists the files of this call under FILES: in its segments. The host has verified that the calls together contain every changed file completely.
- If this packet says AUDIT_CALL=i/n with n > 1, the other calls contain the remaining files of the same PR and are audited separately with the same rules. Judge the files contained in this call. If a blocker cannot be decided without content that is only in another call, return INSUFFICIENT.
- A file marked [PART k/n] is split across parts without truncation.
- Every file's diff ends with an explicit "--- END OF FILE DIFF: <path> (complete) ---" marker. Hunk line counts include context lines, and trailing context lines are often blank; a hunk that ends in blank context lines before that marker is complete, not truncated.
- If the packet contains AUTHORIZATION EVIDENCE from the repository owner or an APPROVED SLICE SCOPE, it defines the approved scope: judge whether the diff implements it correctly and completely and stays within it; changes outside it are scope violations.
- The packet contains AUTHORITATIVE SOURCES, each wrapped exactly as [SOURCE identity=<identity> kind=<kind> class=<class>] ... [/SOURCE identity=<identity>]. The identity is content-bound: <locator>@<body digest> for a GitHub comment, review or body, <locator>@<git blob SHA> for a canonical document. They are this slice's own declaration (its PR body, and the Host's slice specification or remediation authorization when one exists), the durable evidence that declaration cites, and the canonical documents (kind=CANON: the Host's baseline and the ones the declaration cites, at the audited base); the host included every cited source it could read and classified nothing. Only the declaration cites: an id or a path inside a file of the diff is content under audit, not a citation. For any kind other than CANON, kind= is provenance only (a marker such as OWNER-AMENDMENT, or UNMARKED). Every audit call contains all of them. Read every source and apply it together with the approved scope when judging the diff. REQUIRED_SOURCES lists the identities that must be read.
- Roles: the user decides product features, product behaviour and real external actions; the implementing agent decides implementation (internal design, schema, endpoints, tests, migration numbering) for work the canonical documents already define. An implementation choice is never a reason to stop: judge whether it is correct, safe and inside the canonical scope.
- Return HUMAN_DECISION_REQUIRED only when the diff itself needs a decision that is the user's: a product feature the canonical requirements do not contain, a user-visible behaviour or policy with several real product directions that no canonical text decides, a change beyond what the user asked for, or a real external action (a LIVE provider mutation, a real provider or supplier call, a real canary, accepting the residual risk of such an action, a cost, a real data transfer, a destructive operation). Start the SUMMARY with the category: $(Get-HumanDecisionCategoryList). A code, test, contract or scope defect is BLOCKER, never HUMAN_DECISION_REQUIRED.
- Report in EVIDENCE_SEEN the full identity (locator AND digest, exactly as written after identity=, never the ID alone) of every source you actually read in this packet. A required identity that is missing from EVIDENCE_SEEN, or listed with a different digest, makes this audit result not PASS.
"@

# -------------------------------------------------
# 9. GPT packet-only audit
# -------------------------------------------------

function New-GptPrompt {
    param([string]$PacketText)

    return @"
[ICBM-NEW] Exact-HEAD independent audit

You are the architect/verifier.

IMPORTANT:
- Audit ONLY the evidence packet below.
- Do NOT run commands.
- Do NOT inspect the repository.
- Do NOT enumerate files.
- Do NOT use rg, git, PowerShell, shell or web.
- Do NOT modify anything.
- The deterministic host has already verified HEAD, clean audit worktree, CI and that every changed file is included.
- Evaluate contract consistency from the exact main...HEAD diff.
- If the evidence packet contains multiple audit segments, treat them as one evidence set and verify consistency across the segments.
$multiCallNote
- Look specifically for contradictory old/new contract language, stale reopening conditions, weakened safety rules, scope violations and missing/incorrect contract-test pins.
- Do not trust commit messages as proof.
- The canon this slice is judged against is the kind=CANON sources: files as they are at the AUDITED BASE, which is what binds before this slice. origin=baseline are the Host's own (the roadmap, the current milestone, execution safety, operating authority) and are in every packet; origin=referenced are the ones the declaration adds. What the slice changes in the canon is in the diff: judge that change, do not judge by it. If deciding needs a canonical document the packet does not carry, return INSUFFICIENT and name its path in SUMMARY; never assume what an unseen document says.
- If the supplied packet is not enough to decide safely, return INSUFFICIENT.
- PASS only if this packet contains enough evidence and no blocker is visible.

Return EXACTLY four lines:

AUDIT_HEAD=$prHead
VERDICT=<PASS|BLOCKER|INSUFFICIENT|HUMAN_DECISION_REQUIRED>
SUMMARY=<one concise line>
EVIDENCE_SEEN=<comma-separated identities (<locator>@<digest>, exactly as written after identity=) of the sources you actually read; NONE if the packet has no sources>

EVIDENCE PACKET:

$PacketText
"@
}

function Invoke-GptCall {
    param(
        [string]$Prompt,
        [string]$OutPath,
        [string]$ErrPath
    )

    Remove-Item $OutPath -Force -ErrorAction SilentlyContinue

    $oldEap = $ErrorActionPreference

    try {
        # codex CLI 정상 배너/진행 출력이 stderr로 나와도 terminating error로 오인하지 않는다.
        $ErrorActionPreference = "Continue"

        $Prompt | codex exec `
            -C $hostRoot `
            -s read-only `
            --ephemeral `
            --model gpt-5.6-sol `
            --config 'model_reasoning_effort="high"' `
            -o $OutPath `
            - `
            2> $ErrPath |
            Out-Host

        $code = $LASTEXITCODE
    }
    finally {
        $ErrorActionPreference = $oldEap
    }

    return ($code -eq 0 -and (Test-Path $OutPath))
}

Write-Host "[1/2] GPT packet-only audit... calls=$($auditCalls.Count)"

$useGptCache = Test-PassCache -Path $gptCache

if ($useGptCache) {
    Write-Host "GPT_CACHE=HIT (PASS, same HEAD + packet digest, evidence covered)"
}
else {
    Write-Host "GPT_CACHE=MISS"

    $gptResults = New-Object System.Collections.Generic.List[object]

    foreach ($call in $auditCalls) {
        # auditor 원 출력 (call 별). 캐시가 아니다.
        $outPath = Join-Path $stateDir "$cacheKey-gpt-call$($call.Index).txt"

        $errPath = Join-Path $logDir "pr-$PrNumber-gpt-call$($call.Index)-$ts.stderr.txt"

        $ok = Invoke-GptCall `
            -Prompt (New-GptPrompt -PacketText $call.Text) `
            -OutPath $outPath `
            -ErrPath $errPath

        if (-not $ok) {
            Write-Host ""
            Write-Output "GPT_AUDIT=ERROR"
            Write-Host "GPT_CALL=$($call.Index)"
            Write-Host "CLAUDE_AUDIT=SKIPPED"
            Remove-Item $gptCache, $gptResultPath -Force -ErrorAction SilentlyContinue
            Write-Output "AUDIT_BLOCKED=GPT_EXEC_FAILED"
            return
        }

        $gptResults.Add((Read-CallVerdict -Text (Get-Content -Raw -Encoding utf8 $outPath) -Index $call.Index))
        Add-ToolStamp -Path $outPath -Auditor "GPT" -Policy $auditPolicyVersion
    }

    Merge-CallVerdicts -Results @($gptResults.ToArray()) -Path $gptResultPath
    Publish-AuditResult -ResultPath $gptResultPath -CachePath $gptCache -Auditor "GPT"
}

$gptResult = Get-Content -Raw -Encoding utf8 $(if ($useGptCache) { $gptCache } else { $gptResultPath })

Write-Host ""
Write-Host "GPT AUDIT:"
Write-Host $gptResult.Trim()
Write-Host ""

$gptParsed = Read-VerdictText -Text $gptResult

if (-not $gptParsed.Valid) {
    Write-Output "GPT_AUDIT=INVALID_FORMAT"
    Write-Host "CLAUDE_AUDIT=SKIPPED"
    return
}

$gptVerdict = $gptParsed.Verdict

Write-Output "GPT_VERDICT=$gptVerdict"

# 중요:
# GPT가 PASS가 아니면 Claude 사용량을 쓰지 않는다.
if ($gptVerdict -ne "PASS") {
    Write-Output "PIPELINE_STOP=GPT_$gptVerdict"
    Write-Host "CLAUDE_AUDIT=SKIPPED"
    Write-Host ""
    Write-Host "PACKET_MANIFEST=$manifestPath"
    return
}

# -------------------------------------------------
# 10. Claude independent packet-only cross-audit
#
# GPT 판정은 Claude에게 제공하지 않는다.
# stdin을 사용해 --print prompt 오류를 피한다.
# -------------------------------------------------

function New-ClaudePrompt {
    param([string]$PacketText)

    return @"
[ICBM-NEW] Independent exact-HEAD cross-audit

Perform an INDEPENDENT audit.

IMPORTANT:
- Do NOT assume another auditor's result.
- Audit ONLY the evidence packet below.
- Do NOT use tools.
- Do NOT inspect the repository.
- Do NOT modify anything.
- Evaluate contract consistency from the exact main...HEAD diff.
- If the evidence packet contains multiple audit segments, treat them as one evidence set and verify consistency across the segments.
$multiCallNote
- Look specifically for contradictory old/new contract language, stale reopening conditions, weakened safety rules, scope violations and missing/incorrect contract-test pins.
- The canon this slice is judged against is the kind=CANON sources: files as they are at the AUDITED BASE, which is what binds before this slice. origin=baseline are the Host's own (the roadmap, the current milestone, execution safety, operating authority) and are in every packet; origin=referenced are the ones the declaration adds. What the slice changes in the canon is in the diff: judge that change, do not judge by it. If deciding needs a canonical document the packet does not carry, return INSUFFICIENT and name its path in SUMMARY; never assume what an unseen document says.
- If the supplied packet is not sufficient to decide safely, return INSUFFICIENT.
- PASS only when this packet provides sufficient evidence and no blocker is visible.

Return EXACTLY four lines:

AUDIT_HEAD=$prHead
VERDICT=<PASS|BLOCKER|INSUFFICIENT|HUMAN_DECISION_REQUIRED>
SUMMARY=<one concise line>
EVIDENCE_SEEN=<comma-separated identities (<locator>@<digest>, exactly as written after identity=) of the sources you actually read; NONE if the packet has no sources>

EVIDENCE PACKET:

$PacketText
"@
}

function Invoke-ClaudeCall {
    param(
        [string]$Prompt,
        [string]$OutPath,
        [string]$ErrPath
    )

    Remove-Item $OutPath -Force -ErrorAction SilentlyContinue

    $oldEap = $ErrorActionPreference

    try {
        $ErrorActionPreference = "Continue"

        $out = $Prompt | claude -p `
            --no-session-persistence `
            --restricted `
            --permission-prompts none `
            2> $ErrPath

        $code = $LASTEXITCODE
    }
    finally {
        $ErrorActionPreference = $oldEap
    }

    if ($code -ne 0 -or -not $out) {
        return $false
    }

    [System.IO.File]::WriteAllText($OutPath, (@($out) -join "`r`n") + "`r`n", $utf8Out)
    return $true
}

Write-Host "[2/2] Claude packet-only independent cross-audit... calls=$($auditCalls.Count)"

$useClaudeCache = Test-PassCache -Path $claudeCache

if ($useClaudeCache) {
    Write-Host "CLAUDE_CACHE=HIT (PASS, same HEAD + packet digest, evidence covered)"
}
else {
    Write-Host "CLAUDE_CACHE=MISS"

    $claudeResults = New-Object System.Collections.Generic.List[object]

    foreach ($call in $auditCalls) {
        # auditor 원 출력 (call 별). 캐시가 아니다.
        $outPath = Join-Path $stateDir "$cacheKey-claude-call$($call.Index).txt"

        $errPath = Join-Path $logDir "pr-$PrNumber-claude-call$($call.Index)-$ts.stderr.txt"

        $ok = Invoke-ClaudeCall `
            -Prompt (New-ClaudePrompt -PacketText $call.Text) `
            -OutPath $outPath `
            -ErrPath $errPath

        if (-not $ok) {
            Write-Host ""
            Write-Output "CLAUDE_AUDIT=ERROR"
            Write-Host "CLAUDE_CALL=$($call.Index)"
            Remove-Item $claudeCache, $claudeResultPath -Force -ErrorAction SilentlyContinue
            Write-Output "AUDIT_BLOCKED=CLAUDE_EXEC_FAILED"
            return
        }

        $claudeResults.Add((Read-CallVerdict -Text (Get-Content -Raw -Encoding utf8 $outPath) -Index $call.Index))
        Add-ToolStamp -Path $outPath -Auditor "CLAUDE" -Policy $auditPolicyVersion
    }

    Merge-CallVerdicts -Results @($claudeResults.ToArray()) -Path $claudeResultPath
    Publish-AuditResult -ResultPath $claudeResultPath -CachePath $claudeCache -Auditor "CLAUDE"
}

$claudeResult = Get-Content -Raw -Encoding utf8 $(if ($useClaudeCache) { $claudeCache } else { $claudeResultPath })

Write-Host ""
Write-Host "CLAUDE AUDIT:"
Write-Host $claudeResult.Trim()
Write-Host ""

$claudeParsed = Read-VerdictText -Text $claudeResult

if (-not $claudeParsed.Valid) {
    Write-Output "CLAUDE_AUDIT=INVALID_FORMAT"
    return
}

$claudeVerdict = $claudeParsed.Verdict

# -------------------------------------------------
# 11. Final freshness check
#
# PR HEAD뿐 아니라 canonical main도 감사 시작 때와 같아야 한다.
# 기준은 사용자 checkout이 아니라 host audit worktree.
# -------------------------------------------------

$currentRemoteHead = gh pr view $PrNumber `
    --repo $repoSlug `
    --json headRefOid `
    --jq .headRefOid

$currentRemoteHead = "$currentRemoteHead".Trim()

$currentMainHead = gh api `
    "repos/$repoSlug/commits/main" `
    --jq .sha

$currentMainHead = "$currentMainHead".Trim()

$currentWorktreeHead = "$(Invoke-Git @('-C', $auditWorktree, 'rev-parse', 'HEAD'))".Trim()
$currentDirty = Invoke-Git @("-C", $auditWorktree, "status", "--porcelain")

Write-Host "========================================"
Write-Host " FINAL AUDIT RESULT"
Write-Host "========================================"

Write-Host "START MAIN : $mainHead"
Write-Host "MAIN NOW   : $currentMainHead"
Write-Host "START HEAD : $prHead"
Write-Host "REMOTE NOW : $currentRemoteHead"
Write-Host "AUDIT WT   : $currentWorktreeHead"
Write-Host ""

if (
    $mainHead -eq $currentMainHead -and
    $prHead -eq $currentRemoteHead -and
    $prHead -eq $currentWorktreeHead -and
    -not $currentDirty
) {
    Write-Output "HEAD_STATUS=STABLE"
}
else {
    Write-Output "HEAD_STATUS=STALE"
    Write-Output "ALL_AUDIT_RESULTS=DISCARD"
    return
}

Write-Output "CLAUDE_VERDICT=$claudeVerdict"
Write-Output "AUDIT_IDENTITY=HEAD:$prHead PACKET_DIGEST:$packetDigest"
Write-Output "GPT_EVIDENCE_SEEN=$(@($gptParsed.EvidenceSeen) -join ',')"
Write-Output "CLAUDE_EVIDENCE_SEEN=$(@($claudeParsed.EvidenceSeen) -join ',')"

# V2 §5: GPT.evidence_seen ⊇ required AND Claude.evidence_seen ⊇ required, 같은 (HEAD, packet_digest)
if (
    $gptVerdict -eq "PASS" -and
    $claudeVerdict -eq "PASS" -and
    @($gptParsed.Missing).Count -eq 0 -and
    @($claudeParsed.Missing).Count -eq 0
) {
    Write-Output "AUDIT_RESULT=DUAL_PASS"
}
else {
    Write-Output "AUDIT_RESULT=HOLD"
}

Write-Host ""
Write-Host "PACKET_MANIFEST=$manifestPath"
Write-Host "GPT_CACHE=$gptCache"
Write-Host "CLAUDE_CACHE=$claudeCache"
Write-Host "PACKET_FILE=$packetFile"
Write-Host ""
