<!-- Moved verbatim from CLAUDE.md §1 (Roles) by the repository restructure (Issue #151, ADR-0021 §3). The section number is kept, so `CLAUDE.md §1` references resolve here (PATH_MIGRATION_MAP). -->

## 1. Roles

```text
GitHub       = single durable work hub / Source of Truth
ChatGPT      = architecture, contracts, audit, review, acceptance decisions
Claude Code  = implementation, tests, commits, PR updates, implementation drafts
User         = product decisions and explicit protected/destructive approvals
```

Claude implements approved architecture. Claude does not redefine product architecture inside code.
Chat is not the durable work log. Anything that must survive a session belongs in this repository.

### 1.1 Repository exchange protocol

ChatGPT and Claude do not rely on direct AI-to-AI conversation. GitHub is the exchange channel.

| Location | Primary author | Purpose | Expected response |
| --- | --- | --- | --- |
| `documents/decisions/adr/NNNN-*.md` | Architect | Binding architecture/product decision | Implement against it |
| `documents/reviews/*-BY-CLAUDE.md` | Claude | Proposal or implementation question | Architect review / ADR / issue decision |
| `documents/acceptance/**/*.md` | Claude | Acceptance evidence | Architect accept / reject |
| GitHub Issues | Either | Open question, one topic per issue | Commented resolution |
| Pull requests | Claude | Implementation for review | Review comments / acceptance |

Rules:

- Proposal/review documents written by an AI must identify their author and status: `PROPOSAL`, `UNDER REVIEW`, `ACCEPTED`, or `SUPERSEDED`.
- Canonical documents such as `documents/roadmap/ROADMAP.md` and `documents/architecture/ARCHITECTURE.md` do not need author suffixes once accepted.
- A contract/architecture decision becomes binding only when reflected in an ADR or canonical document.
- If an implementation question needs a product decision or a real external action (§14.4), Claude stops and asks the user. An implementation or internal-architecture question is Claude's to decide: decide it, state the choice in the PR, and the audits verify it (§14, ADR-0022).
