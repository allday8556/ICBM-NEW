param([string[]]$Scenarios = @("gpt-loop", "claude-loop", "big-pr", "scope-expansion", "max-cycles", "post-merge-remediation", "remediation-migration-hold", "remediation-open-pr-guard"), [string]$RootName = "fx", [string]$SrcHost = (Split-Path $PSScriptRoot -Parent))

# -SrcHost is the directory that holds the host scripts under test. The default is this repository's own
# automation\agent-host (the parent of tests\), which is where orchestrator-v1.3.ps1 and the others live; pass a
# runtime directory to test a deployed copy instead.
$sp = $PSScriptRoot
# Fixture output never lands in the repository: a fixture repository holds files that repository-wide scans would read.
$outDir = Join-Path ([System.IO.Path]::GetTempPath()) "icbm-agent-host-fx"
$root = Join-Path $outDir $RootName
New-Item -ItemType Directory -Force -Path $root | Out-Null

$jobs = foreach ($s in $Scenarios) {
    Start-Process powershell.exe -ArgumentList @("-NoProfile", "-ExecutionPolicy", "Bypass", "-File", "`"$sp\fx-harness.ps1`"", "-Scenario", $s, "-Root", "`"$root`"", "-SrcHost", "`"$SrcHost`"") `
        -RedirectStandardOutput (Join-Path $outDir "$RootName-$s.out.txt") -RedirectStandardError (Join-Path $outDir "$RootName-$s.err.txt") -NoNewWindow -PassThru
}

$jobs | Wait-Process -Timeout 1200

$failedScenarios = New-Object System.Collections.Generic.List[string]

foreach ($s in $Scenarios) {
    $rp = Join-Path $root "$s\result.json"
    if (Test-Path $rp) {
        $r = Get-Content $rp -Raw -Encoding utf8 | ConvertFrom-Json
        "{0,-28} status={1,-24} action={2,-48} pr={3} userUnchanged={4} automerge={5} forbidden={6} fixerPush={7} escape='{8}'" -f $s, $r.runtime_status, $r.runtime_action, $r.runtime_pr, $r.user_repo_unchanged, $r.runtime_auto_merge, (@($r.forbidden_calls).Count), (@($r.fixer_push_exits) -join ","), $r.fixer_escape_branch
        "    prs: $($r.prs)"
        "    repair: $(@($r.repair_states) -join ', ')  prompts: $(@($r.prompts) -join ',')"
        "    main before/after: $($r.origin_main_before.Substring(0,7)) / $($r.origin_main_after.Substring(0,7))  guardHook=$($r.guard_hook_fired)"
        "    merge_calls: $(@($r.merge_calls) -join ' | ')"
        "    merged: $(@($r.merged_prs) -join ', ')  full_audit_mains: $(@($r.full_audit_mains) -join ', ')"
        "    detail: $($r.runtime_detail)"
        "    audit_modes: $(@($r.audit_modes) -join ' | ')"
        "    baseline: $($r.baseline_file)"
        "    resume: $($r.resume_prompt_delta)"
        "    packet: $(@($r.packet_pointers) -join ' || ')"
        "    verdicts: $(@($r.verdict_files) -join ' || ')"
        "    holds: $(@($r.hold_files) -join ' || ')  legacy_named: $(@($r.legacy_named_verdicts) -join ',')"
        "    key: $(@($r.key_lines) -join ' | ')"
        "    checks: $(if ($r.checks) { ($r.checks.PSObject.Properties | ForEach-Object { "$($_.Name)=$(@($_.Value) -join ';')" }) -join ' | ' })"
        "    EXPECT {0} = {1}" -f $s, $(if ($r.checks -and $r.checks.expect) { $r.checks.expect } else { "NOT_PINNED" })
        # a scenario with no pinned ending proves nothing, so it fails the run like a failed pin
        if (-not ($r.checks -and $r.checks.expect -eq "PASS")) { $failedScenarios.Add($s) }
    }
    else {
        $failedScenarios.Add($s)
        "{0,-28} NO RESULT (see $outDir\$RootName-$s.out.txt / .err.txt)" -f $s
    }
}

# A scenario that did not end as pinned, has no pinned ending, or produced no result fails the run.
if ($failedScenarios.Count -gt 0) {
    "FX_RUN=FAIL ($($failedScenarios -join ', '))"
    exit 1
}

"FX_RUN=PASS ($($Scenarios.Count) scenarios)"
exit 0
