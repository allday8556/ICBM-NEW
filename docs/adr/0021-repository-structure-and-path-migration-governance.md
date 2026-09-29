# ADR-0021 — Repository structure and path migration governance

Status: **ACCEPTED** — decided by the repository owner (the user) in Issue #151, on canonical main
`a0643e4642a759c93acc4204b3ff138ad4580e9a`. This is a governance contract: docs only, runtime-zero.
- **Its authority becomes effective only after this exact PR passes the Agent Host V2 flow
  (`docs/AGENT_HOST_AUDIT_PROTOCOL.md` §1–§8) and is merged.**
- It amends no domain ADR. It records how the repository is restructured and how paths are migrated.
  It does not decide a new layout detail, a new rule or a new policy of its own.

Decision owner: the user (repository owner, product decisions and protected approvals — `CLAUDE.md`
§1). Recorded by Claude Code.

Sources (content-bound, SHA-256 of the API body, UTF-8, LF):

| source | kind | sha256 |
| --- | --- | --- |
| Issue #151 body — "ICBM-NEW Repository Restructure 승인" | `[OWNER-AMENDMENT]` | `4f197c27e92cbcd55d9dadd275f4a59b5d7f2bcc22b0e0448d327649a116fa71` |
| Issue #151 `5882336231` — PR #150 bootstrap conditional acceptance | `[OWNER-AMENDMENT]` | `4554d4e0825438109df12cddc49569f79d2ae235e872e3a9147cbdd150e7f45f` |

Where this record and those sources differ, the sources win.

---

## Context

Files are spread over locations that do not show their role. Work rules, designs, contracts,
evidence, acceptance records, automation and runtime owners sit side by side. As a result, owners are
repeatedly missed (Issue #151 §1).

Several of those locations are pinned by tests, CI, fingerprints, documents or contracts. Until now a
pinned path was treated as a reason to keep a file where it is. `docs/adr/README.md` also says that an
accepted ADR changes only through a superseding ADR. A restructure would therefore either never move a
pinned file, or need a superseding ADR for every path that a moved file changes inside an ADR.

The Agent Host V2 bootstrap ended on main `0919c2ae77f7d2f0162fbacdbd0d8274f34ea6f8`:
- PR #150 was merged;
- its POST_MERGE_VERIFY passed (Issue #89 `5882586622`);
- the owner's conditional bootstrap acceptance `5882336231` was satisfied (Issue #151 `5882591030`).

## Decision

### 1. Priority

The repository restructure goes ahead of new feature development (Issue #151, preamble).

PR #150 (PR-A) was completed first (Issue #151 §6). PR #146 waits until the restructure is complete
(Issue #151 §6).

### 2. Layout principle

The repository is physically reorganized by **large role → middle area → detailed purpose** (Issue #151
§1).

- A current path is **not** kept only because a test, CI job, fingerprint, document or contract pins
  it.
- When a new location fits the file's role, the file moves. Its tests, CI, imports, contracts and
  references migrate with it to the new canonical path.
- The migrated path is then the new canonical contract (Issue #151 §11).

The concrete tree is recorded by the migration PR in the permanent `PATH_MIGRATION_MAP` and repository
map (§6). This ADR does not fix it.

### 3. Rules ownership

- The root `CLAUDE.md` becomes the bootstrap and index that Claude Code loads automatically.
- The canonical entry point and the bodies of the operating, working and audit rules live under
  `documents/rules/`.
- `CLAUDE.md` must connect the canonical rules through a mechanism that actually loads them
  automatically. Text that only says "read X", where it is unclear whether X is loaded, is not allowed
  (Issue #151 §2).

Architecture and domain decisions — for example ADR-0013, ADR-0014 and ADR-0018 — stay owned by their
own ADR:
- `documents/rules/` does **not** copy ADR content;
- where needed, it holds only an index that references the ADR (Issue #151 §2).

### 4. Path-only migration is not a semantic change

During the repository restructure, the following **path-only** edits are not a semantic change of an
architecture or product decision (Issue #151 §3):

- a canonical file path;
- an import or module path;
- a current document link;
- the path inside a current or future command;
- a file locator;
- a CI, workflow or configuration path;
- a test target path;
- the reference that follows a canonical section locator when the file moves.

Such a path-only migration needs no superseding ADR. This includes path-only edits inside accepted ADRs.

A path-only migration never changes:
- the meaning of a decision;
- an invariant or a policy;
- an acceptance verdict;
- a domain contract itself.

### 5. Historical evidence is preserved

The following information, as recorded at the time in past acceptance and evidence records, is **not**
changed retroactively (Issue #151 §4):
- the commands run then;
- the repository paths then;
- exact SHAs;
- digests;
- evidence locators;
- the acceptance results then.

Within the same document, historical evidence and current or future instructions are kept apart:
- a historical section is kept verbatim;
- only current or future instructions migrate to the new canonical path.

### 6. Permanent PATH_MIGRATION_MAP

Every move is recorded permanently as an old → new mapping, the `PATH_MIGRATION_MAP` (Issue #151 §4,
§5).
- A past GitHub issue, PR, review or comment that points at an old path stays traceable to the current
  canonical location through that map.
- When a `CLAUDE.md` section moves, the map also records the old section → new canonical rule or
  section.

### 7. Structural migration and semantic reconciliation are separate

Correcting existing duplication, inconsistency or stale wording in document content is **separate**
from the structural migration (Issue #151 §10). The owner-conflict findings H1–H18 are an example.

- The structural migration PR preserves the existing meaning.
- The only exception is the path-only migration of §4: paths, locators and current execution locations
  change to the new canonical path.

The owner's current classification (Issue #151 §10):
- H2 is a confirmed reconciliation target;
- H3 is **NOT A CONFLICT**;
- H10 is a confirmed reconciliation target;
- the rest are handled in a separate content and owner reconciliation.

### 8. Migration scope

The structural migration is done in one atomic PR where possible. It never leaves a broken main with
paths only half moved. Separate logical commits inside that PR are allowed (Issue #151 §8).

The migration updates and verifies, completely, every item listed in Issue #151 §8:
- Python imports and module names;
- JS imports;
- runtime resource paths;
- tests, including the repository-rule tests;
- CI and workflow paths;
- `pyproject.toml`;
- the Alembic configuration;
- `.gitattributes`;
- document links;
- canonical references;
- fingerprints and their re-pins;
- the code digest;
- acceptance harness paths;
- repository-root depth assumptions;
- current and future commands.

After PR #150, the existing `tools/agent-host` path migrates to an `automation/agent-host/` structure
that fits its final role. Its README, protocol references, `.gitattributes`, tests, CI and every pinned
path follow it to the new canonical location (Issue #151 §7).

### 9. Fingerprints and visual acceptance

When a move changes an implementation fingerprint or the code digest, that change is a normal effect
of the migration (Issue #151 §9).

- A required fingerprint is re-pinned at its new canonical location.
- Whether a semantic revision changes, such as `EXTRACTOR_REVISION`, is decided separately from a
  pure path move.
- When the migration makes the existing Gate 3 visual acceptance stale, the visual acceptance is run
  again on the exact merged main after the migration, and the result is recorded.

### 10. Process after the bootstrap

The restructure uses the normal V2 packet flow of `docs/AGENT_HOST_AUDIT_PROTOCOL.md` §1–§8. It is
**not** added to the bootstrap exception (Issue #151 §6).

## What this ADR does not do

- It moves no file and changes no code, test, CI or schema.
- It amends, supersedes or relaxes no domain ADR, no invariant and no acceptance verdict.
- It resolves none of H1–H18.
- It does not replace the general rule of `docs/adr/README.md`. It only records that a path-only edit
  made under §4 needs no superseding ADR.
- It authorizes no provider call, no LIVE and no action that `CLAUDE.md` §7.2 reserves for the user.

## Consequences

- The migration PR may move pinned files and rewrite their path pins in tests, CI, fingerprints and
  documents without a superseding ADR, as long as each such edit is path-only (§4). Anything beyond a
  path is out of scope and needs its own decision.
- Past records stay readable through the `PATH_MIGRATION_MAP`, because their text is never rewritten
  (§5, §6).
- A moved fingerprint input is re-pinned. The visual acceptance goes stale and is recorded again on
  the merged main (§9).
