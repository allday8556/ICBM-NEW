# ADR-0019 — Registration authoring before AI execution: sequencing, capability gates, and no-pull-forward rule

Status: **ACCEPTED** 2026-09-26

Decision owner: Architect (ChatGPT)

Sources:
- `ROADMAP.md` §§7, 10, 12 and 14
- `docs/architecture/CANONICAL-V3.1.md` §7
- `docs/adr/0012-ai-runtime-provider-contract.md`
- `docs/adr/0013-m4-canonical-product-contract.md` §10
- Issue #30 and refinement comment `5661813529`
- `docs/platforms/smartstore/ENDPOINT_MATRIX.md`
- Gate 3 visual-acceptance contract in ADR-0018

Related:
- ADR-0012 defines AI runtime/provider neutrality and PromptTemplate/PlatformPolicy composition.
- ADR-0013 keeps enrichment attached to the canonical Product and forbids AI from creating source truth.
- ADR-0014 owns marketplace registration and read-back.
- CANONICAL-V3.1 §7 already defines product-name, tag/search, category and option enrichment, marketplace-scoped name/tags, AI_UNREVIEWED, lock and optimistic concurrency.

---

## Context

ICBM-NEW already has a complete canonical AI direction, but the current milestone is still the first SmartStore vertical. The registration UI also needs a full individual-product authoring workspace so an operator can prepare a product manually: images, options/SKU, product-information notice, detail page, price/cost and marketplace-specific registration fields.

Implementing that editor does **not** require pulling forward every AI task, marketplace metadata endpoint, SearchSignalAdapter or bulk orchestration. Doing so before the first vertical closes would duplicate work already scheduled for horizontal expansion and AI implementation, and would mix current REGISTER completion with later capability work.

This ADR fixes the implementation order.

---

## Decision

### 1. The individual registration editor is part of the registration authoring path, not an AI milestone

Registration Management has two operating levels:

```text
Registration list
  → multi-item status / selection / lightweight edits

Individual registration editor
  → one product / one Draft
  → complete manual authoring
  → preview / preflight / register
```

The individual editor must be fully usable **without any AI provider configured**.

It may expose the future AI entry points that CANONICAL-V3.1 §7 already defines, but an unavailable capability is shown as unavailable/disabled. No mock AI result, hidden fallback or UI-owned substitute may pretend that the capability exists.

The editor uses existing owners instead of creating a second product truth:
- immutable source facts remain `ProductFactsRevision`;
- canonical Product/Item, image selection, pricing and readiness remain M4 owners;
- marketplace/account registration inputs remain REGISTER preparation/Draft owners;
- AI enrichment, when later implemented, attaches to the canonical Product and flows into marketplace authoring without rewriting source truth.

### 2. What may be completed before the first vertical closes

Provider-zero registration-authoring work may proceed when separately authorized:
- individual-editor information architecture and navigation;
- manual field entry/editing required by the current SmartStore vertical;
- image selection/exclusion/recovery through the existing image owner;
- option/SKU and composition editing only within already accepted product contracts;
- product-information notice entry using verified source values, operator entry, or an allowed detail-page-reference mode;
- detail-page authoring;
- price/cost display from the Pricing owner;
- marketplace/account scoped final values and locks;
- preview, preflight and registration actions from server-owned state;
- UI states already required by CANONICAL-V3.1, including `AI_UNREVIEWED`, `SOURCE_DRIFT`, lock state and optimistic-concurrency refusal results.

This work must not adopt provider endpoints, call an AI provider, create a second enrichment database, or introduce bulk/horizontal behavior merely to make the screen look complete.

### 3. AI is optional enrichment for the first vertical, not a prerequisite

The first SmartStore vertical may close using verified source data plus operator-authored values.

AI execution is not a prerequisite for M5/M6/M6.5 acceptance.

No first-vertical acceptance gate may require:
- product-name AI;
- tag/search AI;
- category AI ranking;
- option AI mapping;
- image OCR/translation;
- bulk AI recommendation.

This does not remove those capabilities from the product contract. It only fixes their implementation order.

### 4. Existing AI contract is preserved

The later implementation MUST follow the already frozen model:

```text
canonical Product facts / accepted operator values
+ ROLE revision
+ applicable PlatformPolicy revision
+ one task PromptTemplate revision
→ AIProvider
→ structured task result
→ deterministic validation/policy
→ preview
→ operator/system application under the task contract
```

Required task separation remains:
- product name;
- tags/search keywords;
- category validation/ranking;
- required option mapping/normalization;
- missing-fact/notice validation where applicable.

AI never creates unsupported source facts.

For product-information notices and other legal/factual fields:
- **generation of missing factual values is forbidden**;
- validation, conflict detection, normalization proposals and review recommendations are allowed when grounded;
- deterministic policy/compliance checks remain authoritative.

### 5. Marketplace-dependent AI waits for the marketplace data contract it needs

An AI task does not make a `NOT_ADOPTED` or unlisted marketplace endpoint callable.

Before a task consumes provider data, that provider read must have its own reviewed/adopted contract.

SmartStore examples at this main:
- product-name recommendation: no marketplace metadata endpoint is inherently required, but AI execution still waits for the AI implementation sequence below;
- tag/search recommendation: the platform recommendation/search-signal endpoint and `SearchSignalAdapter` must be reviewed/adopted before the canonical §7.8 flow uses them;
- category recommendation: category metadata contract/adoption must exist before provider category data is consumed;
- option mapping: standard-option/attribute metadata contract/adoption must exist before provider option metadata is consumed;
- notice validation against provider notice metadata: notice-type metadata contract/adoption must exist before it is consumed;
- duplicate lookup remains REGISTER reconciliation/safety work, not an AI shortcut.

Absence of a provider endpoint never authorizes scraping or inventing equivalent provider truth inside AI.

### 6. Post-first-vertical implementation order

After the first vertical is accepted twice in fresh sessions, AI capability implementation proceeds in this order unless a later ADR changes it:

```text
A. AI foundation
   PromptTemplate persistence/revisions
   PlatformPolicy persistence/revisions
   AIProvider execution wiring
   structured EnrichmentResult owner
   provenance / fingerprint / STALE
   lock + optimistic concurrency

B. Product-name enrichment
   lowest marketplace-data dependency

C. Search-signal / tag path
   provider endpoint review/adoption
   SearchSignalAdapter
   recommended tags/keywords
   deterministic relevance/prohibited-expression checks

D. Category enrichment
   provider taxonomy/category metadata owner/adoption
   ranking / mapping workflow

E. Option enrichment
   provider attribute/standard-option metadata owner/adoption
   deterministic compatibility remains authoritative

F. Notice/factual validation assistance
   validation only for missing legal facts; no fabrication

G. Multi-marketplace expansion
   Coupang, then 11st, each through its own PlatformPolicy and adapter contracts

H. Bulk AI / bulk registration orchestration
   batch execution, partial failure, per-field revision conflicts, retry only failed tasks

I. AI Insight / analytics expansion
```

A later marketplace may be added before all optional AI tasks are implemented, provided the marketplace's own registration/operation contract is complete and the manual authoring path remains usable.

### 7. Bulk and multi-platform AI are not pulled forward by the individual editor

The individual editor may contain a future `AI 추천` affordance, but that does not authorize:
- multi-product AI batches;
- all-marketplace AI fan-out;
- background seasonal refresh;
- bulk apply;
- automatic registration after AI output.

Bulk AI is implemented with the later bulk/automation milestone and must preserve task-local failure, field revision checks and operator locks.

### 8. AI_UNREVIEWED and concurrency are UI contract, even before AI execution is wired

When AI results exist later:
- `AI_UNREVIEWED` is visible in list filter/count and field detail, but is not itself a registration blocker;
- applying AI output checks `expected_field_revision`;
- revision mismatch refuses the apply and reports it as skipped/changed, never overwrites a newer operator edit;
- an operator-confirmed applied value is protected by the canonical field × marketplace × account lock contract.

Before AI execution exists, the UI must not manufacture these states.

### 9. Seasonal tags are deferred to the tag implementation contract

Time-sensitive keywords are not ordinary source-drift `STALE`.

The tag implementation must decide explicit recommendation validity metadata, such as:
- `seasonal`;
- `valid_from`;
- `valid_until`;
- a separate expired/recommend-refresh state.

Expiration does not silently delete an operator-locked value. It surfaces a replacement recommendation. This is not a first-vertical blocker.

### 10. UI changes still obey visual-proof freshness

Gate 3 visual acceptance is bound to exact accepted code identity. Any later UI/runtime change that affects the accepted code identity makes the earlier proof stale and must be re-run under ADR-0018 before a safety gate relies on it.

Therefore the authoring UI should be implemented in its authorized slice, then visually re-accepted once for that slice. Do not repeatedly rework the screen merely to simulate future AI capabilities.

---

## Consequences

- The current work stays focused on closing the first SmartStore vertical.
- The individual registration editor can be designed and implemented once around stable canonical owners.
- AI slots are preserved without prematurely implementing AI/runtime/provider dependencies.
- Provider endpoint adoption remains explicit and deny-by-default.
- AI implementation after the first vertical reuses CANONICAL-V3.1 §7 instead of inventing a parallel system.
- Bulk and horizontal marketplace work are not smuggled into a single-product editor PR.
