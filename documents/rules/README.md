# ICBM-NEW rules — canonical entry point

Operating rules for ICBM-NEW. Read these before doing any work in this repository.

These rules are the enforcement layer for `documents/roadmap/ROADMAP.md` and `documents/architecture/ARCHITECTURE.md`.
The roadmap says what to build; architecture defines the approved contracts and stack;
these rules define how implementation work is performed and recorded.

If documents conflict, stop and request architect resolution in GitHub instead of guessing.

The root `CLAUDE.md` is the bootstrap index that Claude Code loads automatically; it imports every
file below with `@` imports, so the rules load with it (ADR-0021 §3). The rule bodies live here.
Architecture and domain decisions stay owned by their ADRs under `documents/decisions/adr/`; these
files only reference them.

## Section map (former `CLAUDE.md` §N → canonical file)

Section numbers are kept, so an existing reference to `CLAUDE.md §N` resolves to the file below.

| former section | canonical file |
| --- | --- |
| §1 Roles (incl. §1.1) | `documents/rules/01-roles-and-exchange.md` |
| §2 Absolute no-legacy rule | `documents/rules/02-no-legacy.md` |
| §3 UI source rule | `documents/rules/03-ui-source.md` |
| §4 Pinned runtime stack | `documents/rules/04-runtime-stack.md` |
| §5 Architectural rules | `documents/rules/05-architectural-rules.md` |
| §6 Immutable domain rules | `documents/rules/06-immutable-domain-rules.md` |
| §7 Execution safety | `documents/rules/07-execution-safety.md` |
| §8 Git conventions | `documents/rules/08-git-conventions.md` |
| §9 Definition of Done | `documents/rules/09-definition-of-done.md` |
| §10 Working style expected of Claude | `documents/rules/10-working-style.md` |
| §11 Current milestone | `documents/roadmap/CURRENT-MILESTONE.md` (roadmap content, not a rule) |
| §12 First vertical | `documents/rules/12-first-vertical.md` |
| §13 Canonical file index and read order | this file, below |
| §14 Operating authority (ADR-0022; not a former section) | `documents/rules/14-operating-authority.md` |
| intro (enforcement layer) | this file, above |

---

## 13. Canonical file index and read order

Read in this order before coding (the order Issue #1 mandated for M0):

1. `CLAUDE.md` → `documents/rules/` — implementation/process rules (`CLAUDE.md` is the auto-loaded index; this directory holds the bodies)
2. `documents/roadmap/ROADMAP.md` — product/phase plan and milestone sequence (current milestone: `documents/roadmap/CURRENT-MILESTONE.md`)
3. `documents/architecture/ARCHITECTURE.md` — canonical contracts and stack
4. relevant `documents/decisions/adr/` decisions
5. `documents/contracts/ui/UI_SOURCE_OF_TRUTH.md` — which prototype is the approved visual shell, with its fingerprint
6. the current milestone's GitHub issue and its acceptance criteria
7. `documents/decisions/architect-reviews/ARCHITECT_REVIEW_CLAUDE_ADDITIONS.md` when context on reviewed proposals is needed

Additional locations:

| Path | Purpose |
| --- | --- |
| `documents/archive/proposals/ROADMAP-ADDITIONS-BY-CLAUDE.md` | Claude review proposal; not binding by itself |
| `documents/reviews/` | Claude drafts/proposals awaiting architecture review |
| `documents/acceptance/` | Durable acceptance evidence |
| `documents/architecture/GLOSSARY.md` | Canonical field and concept names (accepted in ARCHITECT_REVIEW D3); read it before naming an identifier or a state |
| `design/prototypes/` | Standalone UI prototypes; only `documents/contracts/ui/UI_SOURCE_OF_TRUTH.md` names the current one |
| `documents/rules/agent-host/AGENT_HOST_AUDIT_PROTOCOL.md` | Agent Host audit, CI, merge and post-merge protocol |
| `documents/reference/PATH_MIGRATION_MAP.md` | Permanent old → new path map of the repository restructure (ADR-0021 §6) |
| `documents/reference/REPOSITORY_MAP.md` | Where each role lives after the restructure |

If canonical documents conflict, stop implementation and request architect resolution in GitHub rather than guessing.
