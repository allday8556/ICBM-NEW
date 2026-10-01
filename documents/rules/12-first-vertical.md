<!-- Moved verbatim from CLAUDE.md §12 (First vertical) by the repository restructure (Issue #151, ADR-0021 §3). The section number is kept, so `CLAUDE.md §12` references resolve here (PATH_MIGRATION_MAP). -->

## 12. First vertical

Do not horizontally expand before this closes:

```text
KM통상 CONNECT
→ 1 real product COLLECT
→ ProductFactsRevision
→ canonical Product DB
→ SmartStore readiness/compliance
→ idempotent SmartStore REGISTER
→ marketplace read-back
→ OPERATE sync
→ fulfillment record/tracking path when applicable
```

At final first-vertical closeout, the complete flow requires two consecutive passes in fresh
sessions on the final implementation line-up. This is one closeout requirement, not a requirement
for each slice, internal PR or provider-zero intermediate. Those changes use their §14.2 validation
tier and do not execute LIVE merely to keep this proof current.
