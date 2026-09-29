# Agent Host — V2 PR-A candidate (Audit Packet foundation)

Status: **CANDIDATE — not activated.** This directory holds the exact bytes of the Agent Host scripts
proposed as PR-A of `docs/AGENT_HOST_AUDIT_PROTOCOL.md` §9. The protocol is canonical at main
`a0643e4642a759c93acc4204b3ff138ad4580e9a`. This PR changes no application code, schema, test,
workflow or product contract. The runtime copy that executes these scripts lives outside the
repository and is activated only after this PR's bootstrap audit, merge and POST_MERGE_VERIFY
(§0.1).

The scripts are Windows PowerShell 5.1, UTF-8 with BOM and CRLF. `.gitattributes` marks this directory
`-text`, so each committed blob equals the runtime file byte for byte, and its sha256 is the pinned
identity.

## Scope (protocol §9 PR-A)

- **Generated, immutable Audit Packet.**
  - Built over content-bound source identities: git objects by commit or path+blob SHA; mutable
    GitHub sources by kind + locator + canonical body SHA-256 (the API body, UTF-8, LF).
  - `updated_at` and the scan watermark are provenance only. Both are kept outside the canonical
    packet bytes.
- **Markers.** A source is marked only when its first non-empty line, trimmed, is exactly
  `[ARCHITECT-INSTRUCTION]`, `[EVIDENCE-PACKET]` or `[OWNER-AMENDMENT]` (§3 grammar).
- **Discovery.** Every packet generation re-scans every designated stream in full, all pages, at
  current bodies, including edits. An unreadable or truncated stream → HOLD.
- **Classification authority.**
  - Only a marked `[ARCHITECT-INSTRUCTION]` or `[OWNER-AMENDMENT]` record carrying an explicit
    `scope: PR #<N>` line and typed entries is authority. Typed entries are: `issue-comment`,
    `pr-review`, `pr-review-comment`, `issue-body`, `pr-body` with `sha256`, and path-bound
    `git-blob <ref>:<path> <sha>`.
  - The host manifest only designates streams.
  - Unclassified marker, changed digest, unparseable or untyped entry, ambiguous git source,
    conflict → HOLD. The Host never infers a mapping.
- **Completeness and reproducibility.** Hard completeness failure → HOLD. Canonical serialization is
  deterministic, so the same inputs give byte-identical packets and the same `packet_digest`.
- **Audit identity** = (exact HEAD, packet_digest).
  - `evidence_seen` coverage is checked by content-bound identity.
  - Only PASS is cached, under that exact identity. BLOCKER and HOLD are never reused, and digestless
    or legacy verdicts are ineligible.
- **Authority write guard.** Every Host GitHub write goes through `Invoke-GhWrite`
  (`agent-host-authority-v2.ps1`). A marker-first body is refused before any request is sent. A local
  draft file is allowed.
- **I2, duplicate slice PR.** Before creating a slice or remediation PR, all open PRs are compared by
  slice identity (branch, body `**Slice:**` or registry) and base. An open same-slice PR is always
  active; a label never clears it. Result: HOLD `DUPLICATE_ACTIVE_SLICE_PR:#N`.
- **I4, runtime chain.**
  - `orchestrator-v1.2.ps1` is a pinned display-only helper: its hash is checked, and it is skipped
    on mismatch.
  - Its output never feeds packet, cache, verdict or merge decisions.
  - No backup or legacy v1 script is referenced.

Out of scope, per the protocol's rollout order:
- PR-B: verdict and control-flow cleanup, fix-counter rules, repeated BLOCKER → HOLD.
- PR-C: canonical CI state, retry and duplicate handling, §7.1 as a merge-state machine, merge and
  post-merge.

Known PR-C defect, not represented as working: `Stop-DuplicateFullCi` in `orchestrator-v1.3.ps1`
fails on `@($cancelled)` in this PowerShell build.

## Runtime chain and pinned identity (sha256 of the committed bytes)

| file | role | sha256 |
| --- | --- | --- |
| `resume-orchestrator-v1.3.ps1` | entry: resume wrapper | `5f07194f107ac3d48a40dced8e5a028cec701dfade5b8aca9252b1f56c807e73` |
| `orchestrator-v1.3.ps1` | control loop, MERGE_GUARD | `34443ac2bb1e67f4f3bcfe084d62756f378aa5ff68f32783ec1c19773360263d` |
| `agent-host-authority-v2.ps1` | marker grammar, write guard | `e48a5cf7dfa51e19167f6ff11cc5f1ab4cf0345469185d1329ecceaf4c92c42c` |
| `run-audit-v1.1.ps1` | packet generator, GPT/Claude audit runner | `30d4ae5b67a7ebc939bf1dd018328323149f02bb81c6e3731aa893043e122547` |
| `run-repair-v1.1.ps1` | fixer, implementer, I2 guard | `589a689e68a6143a17cdb4c3a7e57c1bb1edb6dc3f4e5edc9eef5319772097b6` |
| `run-full-audit-v1.ps1` | post-merge main audit (legacy, PR-C) | `8f709f67482f6b610537ff5dcd3743191028566339db445acecc53135229e3f5` |
| `run-lookahead-main-v1.ps1` | next-slice selector | `b5df030a58ddb7d0de875ee7463603038124532062fd7c17010c9058932dcba7` |
| `run-lookahead-v1.ps1` | lookahead prep (non-authoritative) | `fcbba488f7e3b85501ef824197921234c607552ecbc775f6f5fda1c7d6b722e2` |
| `orchestrator-v1.2.ps1` | pinned display-only state helper | `1138fd4d21a49595b5bb862098ce04c96195a3af23fd0b506e715583d2ca299c` |
| `tests/fx-harness.ps1` | fixture harness (mocked gh/codex/claude) | `f6da1d7765cd6832cbf6eaec00d11921e8595157439cc848c255a0deb4218fca` |
| `tests/fx-run-all.ps1` | fixture runner | `315a8ac9ebe46ed351aae9f7b27789cbca0e21c255fbddd4fe12ff91daab580e` |

`V2-LEGACY-INVENTORY.md` classifies every host script and state family as ACTIVE, SUPERSEDED,
EVIDENCE_ONLY or HISTORICAL. It also records the classification-record grammar.

## Tests

The fixture harness builds a local bare origin and a scratch clone. It mocks `gh`, `codex` and
`claude`, so it makes no GitHub write, no provider call and no AI call:

```text
powershell -NoProfile -ExecutionPolicy Bypass -File tools\agent-host\tests\fx-run-all.ps1 -SrcHost <dir holding the scripts> -Scenarios <names>
```

Scenarios cover:
- the PR-A required discovery tests of protocol §9: old comment edited to a marker, new unclassified
  marker, unreadable or truncated stream, classified-body edit, grammar positive and negative,
  classification authority, write guard;
- B1–B3 of GPT pre-audit `5878815830`;
- I2, including the #142→#146 regression;
- cache and evidence rules;
- the legacy scenarios.
- `packet-many-files`: a PR whose changed-file manifest alone exceeds the 42K call limit. The full manifest is placed once in the canonical packet; every segmented call carries its count and sha256 plus its own FILES, keeps its body (the diff material the host limits) within 42,000 characters, and the host still reassembles every file (the pre-fix bytes fail it with `CALL_OVER_LIMIT` on every call). The scenario pins its expected values and reports `checks: ... expect=PASS` (or `expect=FAIL:<keys>`); run it with `fx-run-all.ps1 -Scenarios packet-many-files`.
