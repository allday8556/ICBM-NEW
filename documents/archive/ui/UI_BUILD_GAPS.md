# ICBM-NEW UI Build Gaps

Basis: `icbm_redesign_test_v28_icbm_new_gaps.html`

Status: **P0 UI GAPS CLOSED BY v28 PROTOTYPE** — historical record. The current approved prototype is the one recorded in `docs/UI_SOURCE_OF_TRUTH.md` (v29 keeps every v28 surface listed here).

The v28 prototype now contains the visual/interaction surfaces that were previously missing for the clean implementation.

## Closed in v28

1. **First-run / empty states** — CLOSED
   - Dashboard
   - Collection Management
   - Integrated DB
   - Registration Management
   - Order Management
   - Inquiry Management
   - Sold-out Confirmation
   - Analytics
   - AI Shopping Insight

2. **수집관리 > 공급처 관리** — CLOSED
   - supplier cards
   - auth/profile/last-verified states
   - supplier add flow
   - credential modal
   - connection/analyze/rules actions

3. **통합DB > 상품 상세/편집** — CLOSED
   - product editor modal
   - source/evidence-oriented sections
   - option/SKU surface
   - image/detail surface
   - pricing/readiness surfaces

4. **등록관리 > 등록 Preflight/Preview** — CLOSED
   - readiness checklist
   - PASS / 확인필요 / BLOCKED representation
   - SmartStore marketplace preview
   - edit/recommend/register actions

5. **등록상품 > SmartStore read-back 상세** — CLOSED
   - registered-product detail/read-back surface
   - marketplace state/identity/sync information shape

6. **Failure / Retry Detail Drawer** — CLOSED AS UI SCAFFOLD

7. **Sync / Activity History** — CLOSED AS UI SCAFFOLD

## Important distinction

`CLOSED` here means **the UI surface exists in the approved prototype**.

It does **not** mean backend/runtime functionality exists.

Claude must connect these screens to the new contracts from `docs/ARCHITECTURE.md`. Prototype data and prototype JavaScript are not runtime owners.

## New UI gaps from architecture review

Claude's roadmap review introduced additional backend contracts. They do not require a redesign before M1, but the following UI hooks may be added later when the corresponding runtime capability lands:

- REVIEW_REQUIRED counts/actions: reuse Dashboard + relevant existing screens; do not add a new top-level menu yet.
- Fulfillment: add under Order Management/Order detail, not as a new top-level menu. First vertical needs supplier-order reference + tracking state fields.
- Estimated vs actual margin: add a small state/label in Analytics once Settlement exists.
- Audit/Job internals: no developer console UI required for first vertical; user-facing failures surface through existing failure/history patterns.
- FX details: only when a non-KRW supplier is introduced.

## Conclusion at the time (v28)

The user did **not** need to build more P0 UI before Claude started the fresh implementation skeleton.

The v28 HTML was sufficient to begin (M0 was then built from v29, which superseded it):

```text
M0 Foundation
→ M1 KM통상 CONNECT
→ M2 SmartStore CONNECT
→ M3 one-product COLLECT
```

Any new visual request should come from a proven runtime gap, not speculative UI expansion.
