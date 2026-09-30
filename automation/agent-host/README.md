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

- **Generated, immutable Audit Packet.**
  - Built over content-bound source identities: the exact HEAD, the audited base, the PR's complete
    diff, and the durable evidence the slice declaration cites, each by locator + canonical body
    SHA-256 (the API body, UTF-8, LF).
  - `updated_at` and the scan watermark are provenance only. Both are kept outside the canonical
    packet bytes.
- **Packet sources are cited, never classified** (protocol §3).
  - The declaration is the PR body, plus the Host's slice specification when one exists.
  - Scanned streams: the PR's own four streams, the comments of every issue the declaration names
    as `Issue #<n>`, and any stream an optional `state\audit-sources-pr-<N>.json` designates.
  - A source is a packet source when the declaration cites its id (a bare number of 9–12 digits).
    Every packet source is required.
  - A cited id that no scanned stream holds is provenance, not a hold.
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
- **Two hold classes** (protocol §5.1), decided by category in `agent-host-authority-v2.ps1`:
  - `HUMAN_DECISION_REQUIRED`: a closed list — a new product feature, an undecided product
    direction, a change beyond the user's requirement, a real external action (LIVE, provider call,
    canary, real external read, residual risk, cost, real data transfer, destructive operation),
    and the owner's own hold file on a PR. The run waits for the user.
  - `TECHNICAL_HOLD`: everything else. The supervisor in `orchestrator-v1.3.ps1` retries the same
    state with a doubling wait, and ends `TECHNICAL_HOLD_EXHAUSTED` after
    `technical_hold.max_same_state_retries`. That is a cost circuit breaker, not a question.
- **Repair.** A BLOCKER is repaired and re-audited. After `repair_loop.max_cycles` attempts every
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
| `orchestrator-v1.3.ps1` | control loop, hold classes, supervisor, MERGE_GUARD, merge, POST_MERGE_VERIFY | `6c315ec0b8291672e3ea04bd099e2bc46c9380ece42e94d7f0bef6059baa22a7` |
| `agent-host-authority-v2.ps1` | marker grammar, write guard, hold taxonomy, citation grammar | `564ac0506e605c95813096f1472710eb959c43f75c2573a47da0872667e008f5` |
| `run-audit-v1.1.ps1` | packet generator, GPT/Claude audit runner | `23997f7b40d8d674555b2f4c9ce1fd5b474b6ed43d5bc1defeded1107e990ae1` |
| `run-repair-v1.1.ps1` | fixer, implementer, I2 guard | `3fff97a009945661f3e36d36d70a99f4e3c04b75177118e1f4e8b9b948812097` |
| `run-full-audit-v1.ps1` | post-merge main audit | `7c6c9cfecfd4d810dbf346e1a988ea4008a989c21e3fe68a49f82027d6738c37` |
| `run-lookahead-main-v1.ps1` | next-slice selector | `6e07b517ea2d425609db4767421f2e3f6d575272a1ac0b8da6c9350aa41329c9` |
| `run-lookahead-v1.ps1` | lookahead prep (non-authoritative) | `fcbba488f7e3b85501ef824197921234c607552ecbc775f6f5fda1c7d6b722e2` |
| `orchestrator-v1.2.ps1` | pinned display-only state helper | `1138fd4d21a49595b5bb862098ce04c96195a3af23fd0b506e715583d2ca299c` |
| `tests/fx-harness.ps1` | fixture harness (mocked gh/codex/claude) | `9aed1aeea5668dedff623a478646333dbd535365b019b02a73cc468857b6069e` |
| `tests/fx-run-all.ps1` | fixture runner | `0cadb93ff78c6e788bcf5d29104ca0a015126c55ef03a9038fe1761a5f7f29ad` |
| `tests/fx-config.json` | fixture host configuration | `d6bdac1a09b04cdf078d351f99ac92a693f6843b32f3d409b1a11cecc2d7180c` |

`V2-LEGACY-INVENTORY.md` is the V2 inventory of host scripts and state families. Its §7 records what
V3 supersedes.

## Tests

The fixture harness builds a local bare origin and a scratch clone. It mocks `gh`, `codex` and
`claude`, so it makes no GitHub write, no provider call and no AI call. Its host configuration is
`tests\fx-config.json`, and its output goes to the temporary directory, never into the repository.

```text
powershell -NoProfile -ExecutionPolicy Bypass -Command "& 'automation\agent-host\tests\fx-run-all.ps1' -Scenarios <names>"
```

Every scenario that pins its ending prints `FX_EXPECT=PASS` or `FX_EXPECT=FAIL:<keys>`.
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
