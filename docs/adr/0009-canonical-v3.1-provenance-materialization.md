# ADR-0009 — Canonical v3.1 provenance materialization

Status: **ACCEPTED** 2026-09-14. This ADR records the provenance resolution approved in PR #36, architect review `5198359602`.
Relates to: ADR-0008 (not superseded)
Recorded by: Claude Code, at the user's instruction after PR #36 merged. The number was confirmed free in `docs/adr/` immediately before writing.
Date: 2026-09-14

---

## Context

ADR-0008 (Issue #25, Stage 0, PR #35) quotes Canonical v3.1 §11.3. When it was written, the frozen v3.1 document was not in this repository. ADR-0008 says so in its "Provenance gap" paragraph and in confirmation point 4.

The PR #35 audit (`5197614709`) asked for the frozen document to be materialized in the repository as a separate documentation change. PR #36 did that.

## Decision

- PR #36 materialized the frozen Canonical v3.1 at **`docs/architecture/CANONICAL-V3.1.md`** in two commits:
  - a byte-identical copy of the approved document: SHA-256 `d548887e34f49f8371cda092d9aaf632b4c926a3a9734f65b2f0bbaa21ab0f22`, 46,046 bytes, 1,426 lines, git blob `dc9c414dcc689529d70b7b6a59959f8ecd95cbb7`, PR commit `56cd2fd`;
  - a change to exactly one line, its placement line (PR commit `fd6a974`).

  It was squash-merged as `9d56725`. `main` therefore holds the placement-edited file. The byte-identical commit remains on GitHub under `refs/pull/36/head`.
- ADR-0008's statements that the frozen document is not in the repository are **kept as the record of the facts at Stage 0**. They are not edited.
- **The provenance gap is resolved** by PR #36 and architect review `5198359602`. The canonical path of Canonical v3.1 is `docs/architecture/CANONICAL-V3.1.md`.
- **Neither the Canonical v3.1 text nor ADR-0008 is modified.** ADR-0008's taxonomy decision stands unchanged.

## Consequences

- The reference chain is explicit:

  ```text
  ERRORS.md → ADR-0008 → ADR-0009 → docs/architecture/CANONICAL-V3.1.md
  ```

- `docs/ARCHITECTURE.md` §8 names that path for §11.3.
- ADR-0008 Stage 1 consistency checks read §11.3 from that path, never from an untracked file or chat history.
