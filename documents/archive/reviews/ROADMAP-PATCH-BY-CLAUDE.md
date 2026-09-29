# ROADMAP.md Patch Proposal

Status: **PROPOSAL**
Author: Claude
Target file: `ROADMAP.md`
Basis: `docs/ARCHITECT_REVIEW_CLAUDE_ADDITIONS.md`, `docs/UI_SOURCE_OF_TRUTH.md`, `docs/ARCHITECTURE.md`
Place at: `docs/review/ROADMAP-PATCH-BY-CLAUDE.md`

---

## Why this patch exists

`ROADMAP.md` is #1 in the canonical read order (`CLAUDE.md` §13), but it has not
been updated since the architect review. Two contradictions currently exist
between it and other canonical documents:

1. §1.3 names **v27** as the UI Source of Truth. `docs/UI_SOURCE_OF_TRUTH.md`
   states that **v28** supersedes v27. A session that reads the roadmap first and
   stops there will build against the wrong prototype.
2. Fulfillment does not appear anywhere in `ROADMAP.md` (zero occurrences of
   fulfillment / 발주 / 송장), although A1 was accepted and M6.5 was added in the
   review.

A third gap is not a contradiction but blocks clean milestone closure:

3. Phase 0 / M0 has no usable acceptance criteria.

This patch fixes all three and nothing else. It does **not** introduce any
contract not already accepted in the architect review.

### Explicitly NOT patched

The review ruled that `FULFILL` is **not** a sixth top-level stage. The §0 spine
line therefore stays at five stages. My original A1 proposal to extend the line
is withdrawn.

---

## Patch 1 — status banner at the top of `ROADMAP.md`

**Insert** immediately below the existing `> Status: NEW CANONICAL PROJECT` block:

```markdown
> Superseding documents: where this roadmap conflicts with
> `docs/ARCHITECTURE.md`, `docs/UI_SOURCE_OF_TRUTH.md`, or
> `docs/ARCHITECT_REVIEW_CLAUDE_ADDITIONS.md`, those documents win.
> This roadmap states the product and phase plan; canonical contracts, the
> runtime stack, and the approved UI revision live in `docs/`.
```

Rationale: the roadmap will drift again. A standing precedence rule costs one
paragraph and removes the class of failure permanently.

---

## Patch 2 — §0 Product goal

**Current** (item 5 of the numbered list):

```
5. Track sales state, stock, orders, inquiries, claims, and marketplace read-back.
```

**Replace with:**

```
5. Track sales state, stock, orders, inquiries, claims, and marketplace read-back.
6. Fulfil the order: record the supplier order, capture tracking, write shipment
   state back to the marketplace, and reconcile settlement.
```

**Also append** to the paragraph beginning "Everything else — AI, OCR, learning…":

```markdown
Fulfillment is not a sixth top-level stage. It is the subflow of OPERATE that
runs after an order arrives. The spine remains
`CONNECT → COLLECT → PRODUCT DB → REGISTER → OPERATE`.
```

---

## Patch 3 — §1.3 UI Source of Truth

**Current:**

```
The canonical visual/product shell for ICBM-NEW is the standalone prototype
supplied by the user:

    icbm_redesign_test_v27_global_help_tooltips.html
```

**Replace with:**

```markdown
The canonical visual/product shell for ICBM-NEW is the approved standalone
prototype recorded in `docs/UI_SOURCE_OF_TRUTH.md`.

Current approved revision:

    ui/prototypes/icbm_redesign_test_v28_icbm_new_gaps.html

`icbm_redesign_test_v27_global_help_tooltips.html` is superseded and is not an
implementation source.

Do not hard-code a prototype filename anywhere else in this document. When the
user approves a new revision, only `docs/UI_SOURCE_OF_TRUTH.md` changes.
```

Rationale: the filename was pinned in two documents, which is how the drift
happened. After this patch there is exactly one place that names the file.

The remaining rules in §1.3 (no #86 wiring, contract-first direction, forbidden
transplant diagram) are unchanged and remain correct.

---

## Patch 4 — §3 Phase 0 acceptance

**Current:**

```
- standalone HTML visual structure is reproduced without importing legacy functional wiring
- clean install starts with an empty DB
- no dependency on ICBM-PROJECT runtime files
- no dependency on #86 functional adapters/owners
- no legacy DB migration required
- one deterministic health check passes
```

**Replace with:**

```markdown
### Scope boundary

M0 produces a running shell and foundation, not features.

- every screen renders, navigation works, empty states display
- all screen data comes from the new application contracts returning empty results
- **zero supplier or marketplace calls are made during M0**
- prototype JavaScript is referenced for interaction shape only; it never becomes
  a runtime owner

### Acceptance

No-legacy conditions:

- v28 visual structure reproduced without importing legacy functional wiring
- no dependency on ICBM-PROJECT runtime files
- no dependency on #86 functional adapters/owners
- no legacy DB migration required

Foundation conditions — each must be demonstrated, not asserted:

1. clean checkout → Alembic migration runs → application starts on an empty DB
2. health/readiness endpoint returns a deterministic pass
3. one job is enqueued, fails deliberately, retries on schedule, and lands in
   dead-letter at the attempt cap
4. the same `correlation_id` is retrievable across log output, the Job record,
   and an AuditEvent for one flow
5. one AuditEvent is written for a protected action
6. CI is green: lint, type check, tests, migration check
7. the shell renders all ten top-level screens in empty state
8. the whole sequence repeats after a full restart

Evidence for 1–8 is committed to `docs/acceptance/M0.md`.
```

---

## Patch 5 — §8 Phase 5, new subsections

**Insert** after the existing §8.2 (Stock / sold-out):

```markdown
## 8.2b Source drift

Recollection compares facts fingerprints against the current accepted revision.

    price changed      → new pricing proposal
    option removed     → REVIEW_REQUIRED
    option added       → proposed addition
    image changed      → image pipeline
    notice/detail      → REVIEW_REQUIRED when materially changed
    stock changed      → stock workflow

A live marketplace option is never silently removed because a supplier page
changed.
```

**Insert** after the existing §8.4 (Inquiry / claims):

```markdown
## 8.5 Fulfillment

Fulfillment closes the loop between a marketplace order and the supplier.

    Marketplace order
    → Product + atomic SKU mapping
    → SupplierOrder record
    → supplier purchase (manual in the first vertical, recorded canonically)
    → tracking captured
    → marketplace shipment/tracking write
    → delivery read-back
    → settlement

First vertical: **manual supplier ordering with canonical recording.** Supplier
cart/API automation is a later adapter capability. The canonical
`SupplierOrder` record exists from v1 regardless.

A fulfillment state that lives only in a spreadsheet does not satisfy this
section.
```

**Append** to the §8 Phase 5 acceptance block:

```
- an order maps to exactly one SupplierOrder
- a tracking number written back to the marketplace is read back and stored
```

---

## Patch 6 — §12 Development sequence

**Current:**

```
M6 OPERATE read-back / stock / orders
   ↓
FIRST VERTICAL ACCEPTED
```

**Replace with:**

```
M6 OPERATE read-back / stock / order ingest
   ↓
M6.5 fulfillment record + tracking path
   ↓
FIRST VERTICAL ACCEPTED
```

**Also append** a numbering map to §12, since the document uses Phase 0–7 and M0–M6.5 interchangeably:

```markdown
| Milestone | Phase |
| --------- | ----- |
| M0 | Phase 0 — Foundation |
| M1, M2 | Phase 1 — CONNECT |
| M3 | Phase 2 — COLLECT |
| M4 | Phase 3 — PRODUCT DB |
| M5 | Phase 4 — REGISTER |
| M6, M6.5 | Phase 5 — OPERATE (fulfillment included) |
| after first vertical | Phase 6 — Expand |
| later | Phase 7 — AI / Analytics / Insight |
```

---

## Patch 7 — §13 Definition of Done

**Current** first-production-vertical block:

```
K홀세일 CONNECT
→ real product COLLECT
→ canonical PRODUCT DB
→ SmartStore REGISTER
→ marketplace READ-BACK
→ OPERATE state sync

2 consecutive complete PASS
```

**Replace with:**

```
K홀세일 CONNECT
→ real product COLLECT → ProductFactsRevision
→ canonical PRODUCT DB
→ readiness + ComplianceGate PASS
→ idempotent SmartStore REGISTER
→ marketplace READ-BACK
→ OPERATE state sync
→ fulfillment record / tracking path when an order exists

2 consecutive complete PASS in fresh sessions
```

**Append** to §13:

```markdown
Acceptance evidence is committed to `docs/acceptance/`, never pasted in chat.
Each record contains correlation IDs, external identifiers returned, timestamps,
read-back payload, and confirmation of the fresh-session condition.
```

---

## Patch 8 — §11 UI mapping, one row

**Current:**

```
| Order Management | marketplace orders mapped to canonical products |
```

**Replace with:**

```
| Order Management | marketplace orders mapped to canonical products; supplier-order reference and tracking state live in order detail |
```

Per `docs/UI_BUILD_GAPS.md`, fulfillment gets no new top-level menu.

---

## Patch 9 — document hygiene

- Heading levels break at §2: the document uses `## 0`, `## 1`, then `# 2` onward.
  Normalize all top-level sections to `#` or all to `##`.
- Add a version/change-log table at the end of `ROADMAP.md`.
- The root file is committed as `ROADMAP-ADDITIONS-BY-CLAUDE.MD` (uppercase
  extension) while `CLAUDE.md` §13 references it as `.md`. Rename to lowercase;
  a case-sensitive CI filesystem will not resolve the reference as written.

---

## Not included in this patch, tracked separately

These were accepted in the review but have no milestone assigned yet. They do not
block M0. Suggested homes:

| Item | Suggested milestone |
| ---- | ------------------- |
| C2 crawl policy | before M1 (supplier CONNECT) |
| B7 rate/quota limiter | before M2 (marketplace CONNECT) |
| C3 evidence retention | before M3 (first collection) |
| A5 FX provider/staleness policy | before the first non-KRW supplier |
| C5 backup/restore drill | before first LIVE write |
| D3 `docs/GLOSSARY.md` | M0, alongside the first schema |
| D5 performance targets | M3, once one collection timing is known |

---

## Open item for the architect

`docs/adr/`, `docs/review/`, `docs/acceptance/` and `docs/GLOSSARY.md` are
referenced as binding in `CLAUDE.md` §13 but do not exist yet. Creating them with
a one-line README each makes the exchange protocol real before its first use.

A separate draft, `ADR-0001 runtime stack`, is proposed alongside this patch.
