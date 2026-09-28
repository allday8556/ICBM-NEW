# AGENT_HOST_PROTOCOL_V2 (main a0643e4) §3 — shared authority helpers for every Host script.
#
#   Get-AuthorityMarker   : the deterministic marker grammar (§3 "Recognition grammar").
#   Invoke-GhWrite        : the single Host GitHub write path with the authority write guard (§3 "Authority write guard").
#
# Dot-sourced by orchestrator-v1.3.ps1, run-audit-v1.1.ps1 and run-repair-v1.1.ps1.
# This file has no state and makes no network call of its own.

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
