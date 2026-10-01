<!-- Moved verbatim from CLAUDE.md §1 (Roles) by the repository restructure (Issue #151, ADR-0021 §3). The section number is kept, so `CLAUDE.md §1` references resolve here (PATH_MIGRATION_MAP). -->

## 1. Roles

The single operational assignment of product decisions, implementation decisions and
safety/correctness verification is §14.1. This section defines only the durable exchange roles:

```text
GitHub       = single durable work hub / Source of Truth
Review agent = architecture/contracts audit, review and acceptance evidence
Implementing agent = implementation, tests, commits, PR updates and implementation drafts
```

An implementing agent follows approved architecture and does not redefine product decisions inside
code. Decision ownership and protected-action approval come only from §14.1 and §14.4.
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
- For who decides a question and how strongly it is verified, apply §14.1–§14.4. This exchange
  section adds no second ownership rule.
