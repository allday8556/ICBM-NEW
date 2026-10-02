# Agent Host — V3 (operating authority, ADR-0022)

Status: **CANONICAL SCRIPTS.** This directory holds the exact bytes of the Agent Host scripts that
implement `documents/rules/agent-host/AGENT_HOST_AUDIT_PROTOCOL.md` (V3) and
`documents/decisions/adr/0022-agent-operating-authority.md`. It changes no application code, schema
or product contract. A runtime is a copy of these files in a directory of its own, with its own
`state\`, `logs\` and `worktrees\`; one runtime directory runs one track.

The scripts are Windows PowerShell 5.1, UTF-8 with BOM and CRLF. `.gitattributes` marks this directory
`-text`, so each committed blob equals the runtime file byte for byte, and its sha256 is the pinned
identity.

## What the Host does

The scripts below are the HIGH_RISK strong path of rule §14.2. BASIC and PROVIDER_ZERO changes use
the normal PR/CI path; they are not sent through this packet/dual-audit machinery merely because
the Host is installed. This keeps V3 and narrows when it is invoked; it is not a V4 project.

- **Generated, immutable Audit Packet.**
  - Built over content-bound source identities: the exact HEAD, the audited base, the PR's complete
    diff, and the durable evidence the slice declaration cites, each by locator + canonical body
    SHA-256 (the API body, UTF-8, LF).
  - `updated_at` and the scan watermark are provenance only. Both are kept outside the canonical
    packet bytes.
- **Packet sources are cited, never classified** (protocol §3).
  - The declaration is the PR body, plus the Host's slice specification or remediation
    authorization when one exists. Citations are read from all of it.
  - Scanned streams: the PR's own four streams, the comments of every issue the declaration names
    as `Issue #<n>`, and any stream an optional `state\audit-sources-pr-<N>.json` designates.
  - The PR body itself is always a required source.
  - A citation is a source id of 9–12 digits in a code span, in the form of its kind: `` `<id>` ``
    a conversation comment, `` `review:<id>` `` a review, `` `review-comment:<id>` `` a review
    comment. It resolves only to a source of that kind, which is then a required source.
  - Canon is read at the audited base. Every packet carries the Host's baseline canon
    (`Get-BaselineCanon`: the roadmap, the current milestone, execution safety, operating
    authority) whatever the declaration cites. `` `canon:<path>` `` adds a further canonical
    document, bound by its git blob SHA. An auditor that needs canon the packet does not carry
    returns INSUFFICIENT.
  - A citation no scanned stream holds is a TECHNICAL_HOLD (`CITED_SOURCE_UNRESOLVED`): declared
    evidence never disappears from a packet silently.
- **Markers are provenance.** A source is marked only when its first non-empty line, trimmed, is
  exactly `[ARCHITECT-INSTRUCTION]`, `[EVIDENCE-PACKET]` or `[OWNER-AMENDMENT]`. A marked source
  never holds a packet, and no `scope: PR #<N>` classification record is read. Existing records
  stay in GitHub as history.
- **Completeness and reproducibility.** A hard completeness failure is a TECHNICAL_HOLD. Canonical
  serialization is deterministic, so the same inputs give byte-identical packets and the same
  `packet_digest`.
- **Audit identity** = (exact HEAD, packet_digest).
  - `evidence_seen` coverage is checked by content-bound identity.
  - Only PASS is cached, under that exact identity. A verdict under another policy version or
    without a digest is ineligible.
  - A cached PASS is reused only when it carries this Host's own stamp: the sha256 of the running
    `run-audit-v1.1.ps1`, the auditor and the audit policy. A result made outside the Host is never
    promoted to a Host PASS (Issue #185).
- **Where the auditors run** (Issue #185). The per-PR GPT audit runs in the Host's exact-HEAD audit
  worktree (`worktrees\audit-pr-<n>`, prepared by `Initialize-HostWorktree`), as the full audit runs
  in its own audit worktree. The runtime root need not be a git repository, and no check-skipping
  option is used. Before each call the worktree must exist, be clean and sit at exactly the PR HEAD
  the packet names; otherwise nothing is audited and the run holds
  (`GPT_AUDIT_WORKTREE_MISSING`, `GPT_AUDIT_WORKTREE_HEAD_MISMATCH`, `GPT_AUDIT_WORKTREE_DIRTY`,
  `GPT_PACKET_HEAD_MISMATCH`).
- **Two hold classes** (protocol §5.1), decided by category in `agent-host-authority-v2.ps1`:
  - The class word alone decides nothing; only a category of the closed list makes a stop the
    user's, and only when the whole reason is a form the Host composes (`NO_LIVE_ACTION` is
    technical). An auditor's `HUMAN_DECISION_REQUIRED` without such a category is audited again.
  - `HUMAN_DECISION_REQUIRED`: the Host encoding of rule §14.4 and §7.2 — an undecided or
    out-of-scope product decision, a protected execution action, or the user's own hold. Routine
    read-only provider operations are not in the list. The run waits for the user.
  - `TECHNICAL_HOLD`: everything else. The supervisor in `orchestrator-v1.3.ps1` retries the same
    state with a doubling wait, and ends `TECHNICAL_HOLD_EXHAUSTED` after
    `technical_hold.max_same_state_retries` consecutive retries of it; a different state in
    between starts the count again. That is a cost circuit breaker, not a question.
- **Repair.** Only the six material classes in ADR-0022 §4.1 are BLOCKERs, including a core
  safety-gate bypass; everything else is a NOTE or a technical evidence hold. A BLOCKER is repaired
  and re-audited. After `repair_loop.max_cycles` attempts every
  later attempt is an independent re-analysis; a count never stops the loop. A fix that needs a
  file outside the PR, a migration or a removed file is reported (`SCOPE_WIDENED`,
  `SCHEMA_OR_MIGRATION`, `REMOVED_FILE`) for the auditors, not held.
- **MERGE_GUARD** (protocol §7): same HEAD, same packet digest, `evidence_seen` coverage, the
  packet regenerated at merge time, the current base contained in the HEAD (`behind_by == 0`),
  FULL CI GREEN, mergeable, no owner hold. The merge uses `expected_head_sha`; nothing is forced.
- **Base freshness.** A PR HEAD behind main is brought up to date with GitHub's update-branch and
  audited from scratch.
- **POST_MERGE_VERIFY.** The merge commit's tree must equal the merged HEAD's tree. It is read from
  GitHub on every pass that finds the PR merged; a mismatch never lets the next slice start.
- **Auto-next.** After a verified merge and the post-merge audit, the selector reads the canonical
  documents on the fresh main and the implementer starts the next canonical slice of this runtime's
  track (`track.name` / `track.scope` in the configuration).
- **Authority write guard.** Every Host GitHub write goes through `Invoke-GhWrite`. A marker-first
  body is refused before any request is sent. A local draft file is allowed.
- **I2, duplicate slice PR.** Before creating a slice or remediation PR, all open PRs are compared by
  slice identity (branch, body `**Slice:**` or registry) and base. An open same-slice PR is always
  active; a label never clears it.

## Configuration (`state\orchestrator-config.json`, per runtime)

| key | meaning |
| --- | --- |
| `repository`, `repo_path` | the GitHub repository and the local clone used as object database |
| `auto_merge`, `auto_merge_method` | merge after MERGE_GUARD; default on |
| `auto_next.enabled` | start the next canonical slice after a verified merge; default on |
| `technical_hold.max_same_state_retries`, `technical_hold.max_backoff_seconds` | the retry ceiling and wait bound of a TECHNICAL_HOLD |
| `repair_loop.max_cycles` | repair attempts before independent re-analysis |
| `track.name`, `track.scope` | optional: the one track this runtime runs |

## Runtime chain and pinned identity (sha256 of the committed bytes)

| file | role | sha256 |
| --- | --- | --- |
| `resume-orchestrator-v1.3.ps1` | entry: resume wrapper | `5f07194f107ac3d48a40dced8e5a028cec701dfade5b8aca9252b1f56c807e73` |
| `orchestrator-v1.3.ps1` | control loop, hold classes, supervisor, MERGE_GUARD, merge, POST_MERGE_VERIFY | `59fdaf52c6cdcf3610202b8bbae865a4979ea3addee7a62a6a9a672a785d1c8f` |
| `agent-host-authority-v2.ps1` | marker grammar, write guard, hold taxonomy, citation grammar | `f56d9065edbefff894e9d4f84fed441740adda47709849b4501af2eb1aac1162` |
| `run-audit-v1.1.ps1` | packet generator, GPT/Claude audit runner | `cf9ce3498bbb3454911032a1c8c8328188fe9c7c82aa9a56b5331fc8f3e38262` |
| `run-repair-v1.1.ps1` | fixer, implementer, I2 guard | `c06283c96ec98be585e49f333e4fba6407ff35454c16b39481323f5e9fda79ba` |
| `run-full-audit-v1.ps1` | strong/final-main audit | `992b2a5f2bdf727374ebb7b57871187a396c9923bc6307ef8cafcd980efa8896` |
| `run-lookahead-main-v1.ps1` | next-slice selector | `2091ce4ff46d2827470bae26cc449baf474e3efb2736114ef32a9d5917fc4495` |
| `run-lookahead-v1.ps1` | lookahead prep (non-authoritative) | `fcbba488f7e3b85501ef824197921234c607552ecbc775f6f5fda1c7d6b722e2` |
| `orchestrator-v1.2.ps1` | pinned display-only state helper | `1138fd4d21a49595b5bb862098ce04c96195a3af23fd0b506e715583d2ca299c` |
| `tests/fx-harness.ps1` | fixture harness (mocked gh/codex/claude) | `9adba88f023ea654eace86077570d5cbda05600a7753388f382bbda6233ea83d` |
| `tests/fx-run-all.ps1` | fixture runner | `ab0e599049bdbaac9bd87642aaf523e8526a65baafa219b7dd0c14efa27da1eb` |
| `tests/fx-config.json` | fixture host configuration | `6dc395d640ec1d600d60fc34745147f8a78383133328c511ff490f2f6c2e1af9` |

The table is checked mechanically, so a reader does not recompute it:
`tests/contracts/test_repository_rules.py::test_the_agent_host_readme_pins_the_committed_script_bytes`
fails when a hash differs from the committed file or a script is missing from the table.

`V2-LEGACY-INVENTORY.md` is the V2 inventory of host scripts and state families. Its §7 records what
V3 supersedes.

## Tests

The fixture harness builds a local bare origin and a scratch clone. It mocks `gh`, `codex` and
`claude`, so it makes no GitHub write, no provider call and no AI call. Its host configuration is
`tests\fx-config.json`, and its output goes to the temporary directory, never into the repository.

```text
powershell -NoProfile -ExecutionPolicy Bypass -Command "& 'automation\agent-host\tests\fx-run-all.ps1' -Scenarios <names>"
```

Every scenario that pins its ending prints `FX_EXPECT=PASS` or `FX_EXPECT=FAIL:<keys>`. The runner
ends `FX_RUN=FAIL` with exit code 1 when a pinned scenario fails or produces no result.
`tests/harness/agent_host/test_agent_host_fixtures.py` runs the scenarios protocol §9 requires and
fails on any other ending. They cover:
- a complete packet with no owner amendment and no classification record; legacy records and
  uncited markers as provenance; an unreadable or truncated stream as a technical hold;
- marker grammar, the authority write guard, the hold taxonomy and the citation grammar;
- BLOCKER → repair → new HEAD → new packet → re-audit; re-analysis after repeated BLOCKERs;
- DUAL PASS → FULL CI → MERGE_GUARD → `expected_head_sha` merge → POST_MERGE_VERIFY → next slice;
- a moved HEAD, a moved main, a HEAD behind the base, a cited source or a declaration edited just
  before the merge: no stale PASS is reused and nothing unaudited is merged;
- a new product feature, a LIVE step, an auditor's or a fixer's product-decision stop and the
  owner's hold file: `HUMAN_DECISION_REQUIRED`, and nothing else ever is.
- `packet-many-files`: a PR whose changed-file manifest alone exceeds the 42K call limit.
- `gpt-audit-cwd` (Issue #185): the per-PR GPT audit runs in the exact-HEAD audit worktree
  under a non-git Host root; a wrong-HEAD, missing or dirty worktree or a packet naming another
  HEAD is refused with no GPT call, and a result without the Host's own stamp is never reused.
