# ADR-0029 — The SmartStore category recommendation: chosen only from the official leaf-category catalog

Status: **ACCEPTED** 2026-10-09. This is the contract for the next item of the ROADMAP §12 "SmartStore single-product authoring AI" stage: category, "only when its authoritative metadata contract exists". The authoritative contract is the official leaf-category catalog (`SMARTSTORE_CATEGORY_LIST`, adopted under owner decision 2026-10-04). Attributes, standard options and notice validation keep their gate, because they have no adopted contract.

This ADR lands before its code. Each implementation slice cites it as canon.

Decision owners:
- **Product direction:** the owner, in Issue #219 comment `6078048955`. It is given under the conditions of `6072888750`: contract-first, DRY_RUN, no LIVE.
- **Already decided by canon:**
  - ADR-0014 §4 and §18: an AI ranking alone is never a category confirmation. `CategoryConfirmation.AI_SUGGESTION` stays `CATEGORY_NOT_CONFIRMED` until the operator confirms it.
  - ADR-0014 §27.1: a selection carries the target's category-mapping revision and the catalog's taxonomy revision.
  - The leaf-category catalog (`app/stages/register/category_catalog.py`): durable snapshots, with `require_current_leaf`.
  - ADR-0026, ADR-0027 and ADR-0028: the foundation, the provider, and the task context with its filter.
- **Implementation choices:** the implementation agent, under ADR-0022 and rule 14.

What it authorizes:
- this contract;
- the slices of §6.

What it does not authorize:
- **No new SmartStore endpoint.** The candidates come from ICBM's durable catalog snapshot only. The task reads no provider.
- **No attribute, standard-option or notice validation.**
- **No AI_INITIAL and no automatic application.** The operator applies a recommendation as `AI_SUGGESTION`, and only the operator's own confirmation makes it `OPERATOR_CONFIRMED`.
- **No LIVE.** CREATE still sends only an operator-confirmed category, as before.

Sources: ADR-0014 §4, §18, §27.1; ENDPOINT_MATRIX §4 (`SMARTSTORE_CATEGORY_LIST`); Canonical v3.1 §7.6; the v29 bundle's `category` output (`{"category_id", "confidence", "requires_review"}`) and its `category_candidates[]` variable.

Recorded by: Claude Code (Track A). The number was confirmed free on main and on every open branch.

Date: 2026-10-09

---

## 1. The task

- **Identity.** The enrichment id is `SMARTSTORE_CATEGORY_V1`. It composes the v29 bundle `TASK_PRODUCT_RECOMMEND_BUNDLE_V1` (its `prompt_key`, as in ADR-0028 §4) with the result key `category`, schema `category-stage-1`.
- **Subject.** It is targeted at the SmartStore account bound to the connection (ADR-0028 T3 binding check), because a category is the marketplace's.
- **Fact fields.** `original_name`, `brand`, `manufacturer`, `origin`, `options`, `detail_description`.

## 2. The candidates (in the job, from the durable catalog only)

1. **The catalog.** It is the current SmartStore snapshot of `CategoryCatalogStore`. With no snapshot, the task fails with `AI_CATEGORY_CATALOG_MISSING`, and no call is made.
2. **The terms.** These are the words of the confirmed name, cleaned as for tags (ADR-0028 §4: bracketed text and number+unit quantities removed), plus the confirmed brand removed from them. A term is at least 2 characters long.
3. **The ranking.** Each **leaf** of the catalog is scored by how many terms occur in its `whole_category_name` or `name`. The comparison is case-folded and ignores spaces.
   - Leaves are ranked by score, then by the deepest whole name, then by `category_id`.
   - The top 30 with a score above zero are the candidates: `{category_id, whole_category_name}`.
4. **No candidate.** The task fails with `AI_CATEGORY_NO_CANDIDATES`, and no call is made.

**Fingerprint.** It covers the candidates and the taxonomy revision. A new catalog snapshot that changes them asks again (ADR-0028 §4 reuse).

**Implementation note (2026-10-09, C2): compound nouns.** A leaf whose own name (at least 2 characters) occurs **within** a term also scores one. Korean compound nouns put the category inside the product word, for example `들기름` in `생들기름`; the rule of step 3 alone finds no candidate for such a name. It adds only candidates from the same catalog, so AIC-01 and AIC-02 are unchanged.

## 3. The AI and the filter

**The answer.** The composed bundle carries the candidates as `category_candidates`. The answer's `category` object carries `category_id`, `confidence`, `requires_review` and the ADR-0026 §5 envelope.

**The filter.** The chosen `category_id` must be one of the candidates, and still a current leaf of the same taxonomy revision when the job records it (`require_current_leaf`).
- Otherwise the result is `FAILED` with `AI_CATEGORY_NOT_A_CANDIDATE`. An AI-invented id is never recorded as OK.

**The value.** It records `category_id`, `whole_category_name`, `taxonomy_revision` and the candidate count. `requires_review` is true when the answer says so, or when the confidence is below 0.7.

## 4. The seed upgrade `v29-ai3`

The bundle's `category` object gains `"evidence":[]`. It already carries `confidence` and `requires_review`. The rules are those of ADR-0027 §6:
- a fresh store is seeded upgraded;
- an existing store is upgraded only where the field still holds the replaced text;
- an operator's edit is never overwritten.

## 5. The apply and the screens

**The apply.** The ADR-0026 §7 command gains the field `category`.
- It writes `CategorySelection(category_id, mapping_revision, taxonomy_revision, AI_SUGGESTION)`:
  - `mapping_revision` is the target's current category-mapping revision, as the target policy holds it;
  - `taxonomy_revision` is the result's.
- The result's taxonomy revision must be the target's current one. Otherwise the apply is refused with `AI_APPLY_RESULT_UNUSABLE`.
- An `OPERATOR_CONFIRMED` selection is never overwritten (`AI_APPLY_FIELD_LOCKED`).
- `CATEGORY_NOT_CONFIRMED` stays until the operator confirms, which is the operator's own save of the category (ADR-0014 §18).

**통합DB detail.** `카테고리` and `카테고리 추천 신뢰도` show the bound account's category result: the whole name, the confidence, review and staleness. The group has its own `✨ AI 카테고리 추천`.

**Editor › 기본정보.** Beside the category, the editor shows the recommendation and `AI 카테고리 적용`, which follows the name's apply rules.

## 6. Implementation order

1. **C1 — this contract.**
2. **C2 — the task.**
   - The candidates, the filter, the task definition and the seed `v29-ai3`.
   - The live test of one test product, through the approved profile, after the owner's 8790 deploy.
3. **C3 — the apply and the screens.**

## Invariants

```text
AIC-01  candidates come only from ICBM's durable official leaf-category catalog; the task reads no provider
AIC-02  a recorded category is one of the candidates and a current leaf of the same taxonomy revision
AIC-03  no candidate, no catalog: FAILED with no AI call
AIC-04  an AI category is applied only by the operator, as AI_SUGGESTION; it never confirms a category (ADR-0014 §18)
AIC-05  an OPERATOR_CONFIRMED category selection is never overwritten
AIC-06  attributes, standard options and notices keep their gate
```
