# Agent Host — Legacy Rule Inventory + V2 supersession check (PR-A)

Date: 2026-09-28. Protocol: AGENT_HOST_PROTOCOL_V2 (`documents/rules/agent-host/AGENT_HOST_AUDIT_PROTOCOL.md`, PR #147, ACCEPTED).
Scope of this pass: PR-A (Audit Packet foundation). PR-B / PR-C items are listed as open conflicts only; they were not changed.

Principle applied: only rules explicitly adopted by V2 are runtime-authoritative. Legacy audit/cache/HOLD/CI/retry/READY/MERGE_GUARD
rules are not inherited automatically. Legacy results are historical evidence only. V2 audit/cache/state is authoritative only when V2 produced it.

Classes: **ACTIVE** (runs and decides), **ACTIVE-LEGACY** (still runs, not yet re-specified by V2; scheduled for PR-B/PR-C),
**SUPERSEDED** (replaced by a V2 rule; must not decide), **EVIDENCE_ONLY** (kept as a record; never read as a decision input),
**HISTORICAL** (old/unused; not referenced by any active script).

## 1. Host scripts

| Script | Class | Reason |
|---|---|---|
| orchestrator-v1.3.ps1 | ACTIVE | Main loop. PR-A: DUAL PASS cache and MERGE_GUARD require GPT+Claude PASS on the same (HEAD, packet_digest), evidence_seen ⊇ required, and a fresh packet rebuild with the same digest. CI/READY/retry/merge/post-merge parts are ACTIVE-LEGACY (PR-C). |
| run-audit-v1.1.ps1 | ACTIVE | V2 packet generator (policy `packet-v7-sol-high`): source manifest, marker discovery after watermark, hard completeness → HOLD, canonical packet + digest, immutable write, evidence_seen, PASS-only cache. |
| run-repair-v1.1.ps1 | ACTIVE (+ACTIVE-LEGACY parts) | FIXER / remediation / implement-next. PR-A: reads the BLOCKER only from the V2 result file of the current (HEAD, digest). Fix counter `repair-pr-N.json` counts every BLOCKER cycle → ACTIVE-LEGACY (PR-B: code/contract-BLOCKER-only counter, repeated BLOCKER → HOLD). |
| resume-orchestrator-v1.3.ps1 | ACTIVE | Launcher only (reads runtime current_pr); no audit rules. |
| orchestrator-v1.2.ps1 | ACTIVE (display only) / its audit-state derivation SUPERSEDED | Called as state refresh; writes `orchestrator-state.json` (NEXT slots, display state). Its `Test-AuditPass` reads non-digest names `<policy>-pr-N-main-M-head-H-{gpt,claude}.txt`, which V2 never writes, so it can only show AUDIT_REQUIRED. Not a decision input for any V2 gate. |
| run-full-audit-v1.ps1 | ACTIVE-LEGACY | Post-merge FULL/DELTA main audit. Conflicts with V2 §8 (no automatic second audit after merge; POST_MERGE_VERIFY = merged tree == audited HEAD tree) → PR-C. Not a PR-audit path; its `full-audit-*` verdicts never feed PR DUAL PASS / MERGE_GUARD. |
| run-lookahead-main-v1.ps1 | ACTIVE | Auto-next slice selection (GPT select + Claude cross-check). Produces the slice spec, which V2 records as a packet input (path + content sha256 + allow-list sha256). |
| run-lookahead-v1.ps1 | ACTIVE (non-authoritative) | NEXT-1..3 preparation; never a merge/audit gate. |
| run-audit-v1.ps1 | HISTORICAL | Not referenced by any active script; writes legacy non-digest caches. Must not be run. |
| run-repair-v1.ps1 | HISTORICAL | Not referenced; reads legacy non-digest caches. Must not be run. |
| *.ps1.*.bak (root, incl. `*.pre-packet-a.bak`) | HISTORICAL | Backups only. |

## 2. State file families (`state\`)

| Family | Class | Reason |
|---|---|---|
| `audit-sources-pr-<N>.json` | ACTIVE | V2 authoritative-source manifest (sources/required/sha256 pins, excluded, discovery streams + watermark). #146 created in PR-A. |
| `packets\pr-<N>-head-<12>-<d12>.packet.txt` / `.manifest.json` | ACTIVE | V2 generated immutable canonical packet + its manifest (byte-identical on rebuild or PACKET_IMMUTABILITY_VIOLATION). |
| `packets\pr-<N>-head-<12>.current` | ACTIVE | Mutable pointer to the packet last issued for this HEAD (HEAD, MAIN, policy, digest, manifest sha). Removed on any packet HOLD. |
| `packet-v7-sol-high-…-pkt-<d12>-{gpt,claude}.txt` | ACTIVE | V2 PASS-only cache (written only for VERDICT=PASS with evidence_seen ⊇ required). |
| `packet-v7-sol-high-…-pkt-<d12>-{gpt,claude}.result.txt` | ACTIVE (current run only) | Result of the current run (PASS/BLOCKER/INSUFFICIENT/HOLD); deleted at the start of every audit run; never a cache. |
| `packet-v7-sol-high-…-pkt-<d12>-{gpt,claude}-call<i>.txt` | EVIDENCE_ONLY | Raw auditor output per call. |
| `packet-v7-sol-high-…-packet-hold.result.txt` | EVIDENCE_ONLY | Host HOLD record for a hard-completeness failure (VERDICT=HOLD, PACKET_DIGEST=NONE). |
| `packet-v1-*`, `packet-v3-*`, `packet-v5-*`, `packet-v6-sol-high-*` verdicts and `-call<i>` files | HISTORICAL / EVIDENCE_ONLY | No packet digest, older policy; not read by any V2 path. Includes the v6 GPT 7/7 PASS on #146 e75daab: historical evidence only — the V2 audit of e75daab must be fresh. |
| `repair-pr-<N>.json` | ACTIVE-LEGACY | Fix counter (max 3) — PR-B. |
| `repair-pr-<N>-<12>.patch` | EVIDENCE_ONLY | Fixer diffs. |
| `ci-rerun-<12>.json` | ACTIVE-LEGACY | One failed-job rerun per HEAD — PR-C (V2 §6.1 requires environment evidence before a retry). |
| `ci-duplicate-cancelled-<12>.json` (none present) | ACTIVE-LEGACY | Duplicate FULL CI exclusion — PR-C. |
| `merge-hold-pr-146.json` | ACTIVE | Owner hold; blocks loop and MERGE_GUARD (consistent with V2 §7.6 "no HOLD active"). Left in place. |
| `merge-hold-pr-146.released.json` | HISTORICAL | Released hold record. |
| `next-main-<12>.json` | ACTIVE | Auto-next registry PR → slice spec. |
| `next-slice-main-<12>.json` | ACTIVE | Slice spec (V2 packet input by path + sha256; allowed_paths = scope allow-list identity). |
| `next-scope-amendment-<12>.json` | ACTIVE | Owner amendment; for #146 a V2 required source (AMEND-scope-65d640356845, sha256 pinned). |
| `next-scope-amendment-….pre-f1f6.bak` | HISTORICAL | Backup. |
| `next-select-main-<12>-{gpt,claude}.txt`, `next-selection.json` | EVIDENCE_ONLY | Selection audit records. |
| `full-audit-state.json`, `full-audit-baseline.json`, `full-audit-deferred.json`, `full-audit-v1-*` | ACTIVE-LEGACY | Post-merge audit machinery — PR-C conflict (V2 §8). |
| `remediation-main-<12>.json`, `remediation-authorization.json` | ACTIVE | Remediation PR authorization; included in the V2 packet (authorization block + REMEDIATION_AUTHORIZATION_COMMENT id). |
| `orchestrator-config.json` | ACTIVE (settings) | Its prose `ci_order` / `repair_loop` / `full_audit_policy` rules are legacy descriptions, not V2 authority. |
| `orchestrator-config.json.*.bak` | HISTORICAL | Backups. |
| `orchestrator-runtime.json` | ACTIVE | Runtime status / HOLD reason record. |
| `orchestrator-state.json` | ACTIVE (display only) | Written by v1.2; non-authoritative. |
| `lookahead\` | EVIDENCE_ONLY | Lookahead outputs. |
| `repair-no-push-hooks\pre-push` | ACTIVE | Fixer sandbox no-push guard. |
| `logs\`, `worktrees\` | EVIDENCE_ONLY / ACTIVE (host-owned disposable worktrees) | Logs and exact-head worktrees. |

## 3. Verification — no legacy decision path runs concurrently with V2 for PR audits

1. run-audit reads only `…-pkt-<digest12>-{gpt,claude}.txt` as cache, and re-checks AUDIT_HEAD, PACKET_DIGEST, VERDICT=PASS and evidence_seen ⊇ required; any mismatch deletes the file and audits fresh.
2. The audit policy moved from `packet-v6-sol-high` to `packet-v7-sol-high`, so legacy names differ in both policy and the missing `-pkt-` segment.
3. The orchestrator (`Get-AuditResult`) finds verdicts only through `packets\pr-N-head-H.current`, which must match HEAD, MAIN, policy and the current source-manifest sha256. It requires PACKET_DIGEST in the verdict equal to that digest. `-CacheOnly` (DUAL PASS cache, MERGE_GUARD) reads only the PASS cache file.
4. run-repair reads the BLOCKER only from the V2 result file of the `.current` digest (`NO_EXACT_HEAD_*_AUDIT` / `STALE_AUDIT` otherwise).
5. orchestrator-v1.2 still reads legacy names, but only for display; no gate reads `orchestrator-state.json`.
6. run-audit-v1.ps1 and run-repair-v1.ps1 are not referenced by any active script.
7. Fixture `packet-legacy-cache`: v6 non-digest PASS files, v7 non-digest PASS files and a PASS under a foreign digest (`-pkt-000000000000-`) were all present for the exact HEAD. The host still ran a fresh GPT and Claude audit (2 prompts), then merged on the V2 PASS only.

Fix applied within PR-A (boundary only): the legacy non-digest verdicts are ineligible (items 1–4). Nothing else was changed.

## 4. Open conflicts (out of PR-A scope; not changed)

- PR-B: auditors can still return INSUFFICIENT (V2 has PASS/BLOCKER/HOLD). The fix counter counts every BLOCKER cycle. There is no "repeated identical BLOCKER → HOLD" rule and no separate session identity.
- PR-C: CI rerun rule (`ci-rerun-*`, any failure gets one rerun) vs V2 §6.1 (retry only with environment evidence). Post-merge FULL/DELTA audit (`run-full-audit-v1.ps1`) vs V2 §8 POST_MERGE_VERIFY tree equality. MERGE_GUARD still lacks the tree-equality post-merge check.
- Marker discovery without a manifest scans the PR's own issue comments, reviews and review comments from watermark 0. Other streams (for example Issue #89) are scanned only when a manifest declares them.

---

## 5. Canonical V2 alignment (main a0643e4) — PR-A update 2026-09-29 (not activated)

Supersedes the draft-based statements above where they differ:
- Audit policy is `packet-v8-sol-high`. Draft-V2 `packet-v7-*` results, including the `-pkt-` ones, are now HISTORICAL and never reused, like v6 and earlier.
- `state\audit-sources-pr-<N>.json` only **designates streams**. A host manifest with sources, excluded, required or classifications is HOLD. Classifications come only from marked user/architect classification records in a designated stream (§3).
- Watermark and `updated_at` are scan provenance in `state\packets\pr-<N>-head-<12>.scan.json`. They are outside the canonical packet bytes.
- The host slice spec (`next-slice-main-*.json`) is recorded as `SLICE_SPEC_ORIGIN=host_registry_pre_v2_not_authority`, with its path and git blob SHA. It is scope evidence, not authority.
- New `agent-host-authority-v2.ps1` (ACTIVE) holds the §3 marker grammar and the authority write guard `Invoke-GhWrite`. Every Host GitHub write site goes through it. `git push` of fixer/implementer commits is not a GitHub body and is outside the guard.

### I4 — actual runtime chain

```
resume-orchestrator-v1.3.ps1   5f07194f107ac3d48a40dced8e5a028cec701dfade5b8aca9252b1f56c807e73  launcher only (& orchestrator)
└─ orchestrator-v1.3.ps1       34443ac2bb1e67f4f3bcfe084d62756f378aa5ff68f32783ec1c19773360263d  control plane
   ├─ . agent-host-authority-v2.ps1  e48a5cf7dfa51e19167f6ff11cc5f1ab4cf0345469185d1329ecceaf4c92c42c  grammar + write guard
   ├─ & orchestrator-v1.2.ps1  1138fd4d21a49595b5bb862098ce04c96195a3af23fd0b506e715583d2ca299c  DISPLAY/STATE HELPER ONLY (pinned)
   ├─ Invoke-HostScript run-audit-v1.1.ps1  2f75793fd520ccf4d743c7439de203dce72c32c8e7f2f90cdeeff29f22020598  packet + audits
   │  └─ . agent-host-authority-v2.ps1
   ├─ Invoke-HostScript run-repair-v1.1.ps1  589a689e68a6143a17cdb4c3a7e57c1bb1edb6dc3f4e5edc9eef5319772097b6  fixer / auto-next / remediation
   │  └─ . agent-host-authority-v2.ps1  (reads run-audit-v1.1.ps1 text only for the policy string)
   ├─ Invoke-HostScript run-full-audit-v1.ps1  8f709f67482f6b610537ff5dcd3743191028566339db445acecc53135229e3f5  post-merge (ACTIVE-LEGACY, PR-C)
   │  └─ & run-full-audit-v1.ps1 (self, -ForceFull escalation)
   ├─ Invoke-HostScript run-lookahead-main-v1.ps1  b5df030a58ddb7d0de875ee7463603038124532062fd7c17010c9058932dcba7  next-slice selection
   └─ & run-lookahead-v1.ps1   fcbba488f7e3b85501ef824197921234c607552ecbc775f6f5fda1c7d6b722e2  NEXT-1..3 prep, non-blocking
```

**Why v1.3 calls v1.2.** `Invoke-StateRefresh` runs orchestrator-v1.2.ps1 to refresh `state\orchestrator-state.json` and print it. That file holds the NEXT-1..3 slots, which run-lookahead-v1.ps1 reads, plus a display state.
- Its `Test-AuditPass` still reads digestless legacy names. So its state is display only: now always AUDIT_REQUIRED.
- No script reads its output or `current.state` for any decision.
- **Pinned:** the orchestrator checks its SHA-256 (1138fd4d…) before each call. On a mismatch it does not run it (non-blocking). Its output goes to Out-Host only, so it cannot leak into a function return value.
- **Verdict:** a helper with no V2 authority. Classification: ACTIVE (display only).

**Backups and legacy scripts:**
- No `*.bak` file is referenced by any runtime script.
- `run-audit-v1.ps1` and `run-repair-v1.ps1` are not referenced by any runtime script.
- The V2 packet/cache/verdict/merge path reads only v8 `-pkt-<digest12>` files through the `.current` pointer. Pointer checks: HEAD, MAIN, policy and source-manifest SHA.

Result: I4 PASS.
---

## 6. B1-B3 (GPT pre-audit 5878815830), 2026-09-29, not activated

**Classification record grammar** (run-audit-v1.1.ps1 `Read-ClassificationRecord`). The Host applies only this exact grammar and never infers or reinterprets.
- **Record.** A record is a body whose first non-empty line is exactly `[ARCHITECT-INSTRUCTION]` or `[OWNER-AMENDMENT]`. It must sit in a designated stream and have at least one section line: `required:`, `evidence-only:` or `excluded:` (case-sensitive, trimmed).
- **Scope** (B2). Exactly one line in the body, trimmed, starts with `scope:`. It comes before the first section line and equals exactly `scope: PR #<N>`: case-sensitive, no leading zero, nothing else on the line. The record applies only when <N> is the audited PR. Otherwise (no scope line, two scope lines, another PR, extra text, other case, or a scope line after the sections) the record does not apply, and its own marker stays unclassified, so HOLD.
- **Entries** (B1/B2). One per line, prefixed `- `, with an optional ` — <reason>` suffix (required under `excluded:`):
  - `issue-comment <id> sha256 <64hex>`
  - `pr-review <pr>/<id> sha256 <64hex>`
  - `pr-review-comment <id> sha256 <64hex>`
  - `issue-body <n> sha256 <64hex>`
  - `pr-body <n> sha256 <64hex>`
  - `git-blob <ref>:<path> <40hex blob sha>`, where <ref> is `HEAD`, `base` (both bound to the audit identity) or a 40-hex commit SHA. The declared path is resolved at that ref, and its blob SHA must equal the declared SHA. No other place in the tree is searched.
  - `<section>: none` declares an empty section.
- **HOLD reasons:**
  - any other `- git...` form, such as `git blobs at base: CLAUDE.md <sha>`, `git blob HEAD:…` or a `..` path, gives `CLASSIFICATION_RECORD_AMBIGUOUS_GIT_SOURCE:<record id>`;
  - a canonical kind with other syntax, such as a `(#143)` annotation, gives `CLASSIFICATION_RECORD_UNPARSEABLE:<record id>`;
  - a type-less `<id> <hex>` entry or any non-canonical kind, such as `review findings` or `review`, gives `CLASSIFICATION_RECORD_UNTYPED_ENTRY:<record id>`;
  - a wrong path or a blob found only elsewhere in the tree gives `SOURCE_INCOMPLETE` / `CLASSIFIED_SOURCE_DIGEST_CHANGED`.
- **Existing terse records are not reinterpreted.** The #147 bootstrap record 5876525801 does not satisfy this grammar: it has no scope line, uses `(#143)` annotations and `git blobs at base` shorthand, and has type-less entries. A replacement or amended record is needed before it could apply to any V2 packet.
- **Designated stream kinds:** `issue_comments`, `issue_body`, `pr_body`, `pr_reviews`, `pr_review_comments`. With no host manifest, the defaults are the PR's own comments, body (`pr_body`), reviews and review comments. The #146 manifest now designates `pr_body` 146.

**I2 duplicate guard** (B3, run-repair-v1.1.ps1 `Assert-NoDuplicateSlicePr`):
- A merged or closed PR is inactive, because only open PRs are listed.
- An open same-slice PR is ACTIVE. No label or other metadata can deactivate it; V2 has not adopted any supersession authority yet.
- A `superseded` label is logged as advisory only (`SAME_SLICE_OPEN_PR_ADVISORY`). The result stays `DUPLICATE_ACTIVE_SLICE_PR:#N` (base main) or `SAME_SLICE_PR_OTHER_BASE:#N`.