<!-- Moved verbatim from CLAUDE.md §11 (Current milestone) by the repository restructure (Issue #151, ADR-0021 §3; milestone status is roadmap content, not a rule). The section number is kept, so `CLAUDE.md §11` references resolve here (PATH_MIGRATION_MAP). -->

## 11. Current milestone

```text
M0 — fresh UI shell + Phase 0 foundation   ACCEPTED 2026-09-13 (Issue #1, PR #2, documents/acceptance/milestones/M0.md)
M1 — KM통상 CONNECT only                   ACCEPTED 2026-09-13 (Issue #7, PR #9, ADR-0007, documents/acceptance/milestones/M1.md)
M2 — SmartStore CONNECT                    ACCEPTED 2026-09-15 (Issue #46, PR #51, documents/acceptance/milestones/M2.md)
M3 — KM통상 one-product COLLECT             ACCEPTED 2026-09-18 (Issue #52, ADR-0010, documents/acceptance/milestones/M3.md; bounded by its §2)
M4 — canonical Product DB                  ACCEPTED 2026-09-19 (Issue #80, ADR-0013, PR #81–#87, documents/acceptance/milestones/M4.md; bounded by its §2)
M5 — SmartStore REGISTER                   CURRENT (Issue #89; PR-A contract ADR-0014 → PR-B → PR-C → PR-D → PR-E → PR-F all merged, plus the #96 IMAGE UPLOAD amendment; each PR was separately authorized)
```

Every M5 implementation PR is merged (#90–#96) and main is green. **That is not acceptance.** `documents/acceptance/milestones/M5.md` stays `PENDING` and records no acceptance run; product CREATE and the duplicate-lookup search stay `NOT_ADOPTED` after the provider-evidence review closed `INSUFFICIENT` (Issue #89 `5768312853`, `5768347233`); `product_registration.write` stays `UNVERIFIED`; execution stays `DRY_RUN`; a real canary is `BLOCKED`. The owner and application-path gaps that still separate this from a runnable vertical are listed in `documents/roadmap/ROADMAP.md` §14 and `documents/acceptance/milestones/M5.md` §9; none of them may be closed without its own authorization.

**Standing authorization (ADR-0020).** That per-slice authorization is met by the ROADMAP standing authorization of `documents/decisions/adr/0020-roadmap-standing-authorization.md` when a slice satisfies all of its conditions: it is the next step in ROADMAP order read fresh from the exact main, the canonical documents already decide its scope, safety invariants and owner boundary, it is provider-zero, any endpoint adoption or schema change is already decided by a canonical contract, and it is its own PR that passes CI, the GPT exact-head audit and the independent Claude cross-audit. A real provider call, LIVE, a real canary, the residual-risk acceptance, a new architecture or policy decision, conflicting canon, an unclear scope, an undecided data model, a scope expansion and every approval of §7.2 still need the user's explicit decision.

The current approved visual source is the prototype recorded in `documents/contracts/ui/UI_SOURCE_OF_TRUTH.md`. Do not hard-code a prototype file name in this file or treat an older prototype as current.

The accepted milestone sequence is `documents/roadmap/ROADMAP.md` §12. Implement one milestone at a time, and do not begin horizontal supplier/marketplace expansion before the first vertical (former `CLAUDE.md` §12, now `documents/rules/12-first-vertical.md`) closes.
