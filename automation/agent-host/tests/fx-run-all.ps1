param([string[]]$Scenarios = @("gpt-loop", "claude-loop", "big-pr", "scope-expansion", "max-cycles", "post-merge-remediation", "remediation-migration-hold", "remediation-open-pr-guard"), [string]$RootName = "fx", [string]$SrcHost = "C:\Users\user\ICBM-Agent-Host")

$sp = $PSScriptRoot
$root = Join-Path $sp $RootName
New-Item -ItemType Directory -Force -Path $root | Out-Null

$jobs = foreach ($s in $Scenarios) {
    Start-Process powershell.exe -ArgumentList @("-NoProfile", "-ExecutionPolicy", "Bypass", "-File", "`"$sp\fx-harness.ps1`"", "-Scenario", $s, "-Root", "`"$root`"", "-SrcHost", "`"$SrcHost`"") `
        -RedirectStandardOutput (Join-Path $sp "$RootName-$s.out.txt") -RedirectStandardError (Join-Path $sp "$RootName-$s.err.txt") -NoNewWindow -PassThru
}

$jobs | Wait-Process -Timeout 1200

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
    }
    else {
        "{0,-28} NO RESULT (see fx-$s.out.txt / .err.txt)" -f $s
    }
}
