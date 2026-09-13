# ICBM-NEW UI Source of Truth

Status: **APPROVED — CANONICAL**
Architect approval: **2026-09-13**

## Canonical visual source

The current approved visual prototype is:

`ui/prototypes/icbm_redesign_test_v29_final.html`

Verified attachment fingerprint:

- SHA-256: `896ad87011b8615b8a6a9cd3e790ca04f52e908e4ff7b6a26ea4bf5372dfeb82`
- Size: `323751` bytes
- Revision: `v29 — defect fixes + unified platform identity`
- Architect review source: `docs/review/V29-CHANGES-BY-CLAUDE.md`

v29 supersedes v28. v28 superseded v27.

The uploaded v29 attachment was independently re-hashed before approval and matched the fingerprint above exactly.

> Repository-copy status: **PENDING** until the exact HTML file is present at the path above and its SHA-256 is verified again from the repository copy. Approval of the revision is complete; only the raw-file copy remains.

## Revision history

| Revision | Fingerprint | Status | Note |
| --- | --- | --- | --- |
| v27 | not recorded | superseded | global help tooltips |
| v28 | `3689b86c8c06fb10c5337ab661e8eae8712e1f04bb5afbaa0b1896e9729eba63` | superseded | ICBM-NEW required UI gaps closed |
| v29 | `896ad87011b8615b8a6a9cd3e790ca04f52e908e4ff7b6a26ea4bf5372dfeb82` | **approved** | defect fixes + platform identity registry |

## What v29 keeps from v28

The following v28 surfaces remain part of the approved visual/interaction contract:

- runtime-ready zero-data/empty states
- Collection Management `수집 / 공급처 관리` split
- supplier connection cards and credential/add-supplier surface
- product detail editor modal
- source-evidence/ProductFacts presentation
- option/SKU and image/detail editing surfaces
- registration preflight/readiness checklist + marketplace preview
- registered SmartStore read-back detail surface
- collection/registration failure detail drawer
- operation/history modal wiring

## What v29 changes

v29 is a defect and consistency pass, not a new information architecture.

Approved changes include:

- duplicate help-icon defect removed
- channel-price text returned to neutral semantic colouring
- adaptive list vertical/horizontal overflow corrected
- dashboard responsive grid corrected
- AI Shopping Insight KPI row consistency corrected
- WCAG-oriented text contrast improvements
- global keyboard focus treatment
- `prefers-reduced-motion` support
- clearer `카테고리 추천 신뢰도` label
- duplicated dashboard slogan removed
- platform identity registry/rendering path added for visual consistency

The prototype's embedded platform registry is a **visual prototype mechanism only**. Runtime marketplace identity/configuration belongs to the new marketplace adapter/application contracts.

## Architect ruling — structural, not literal, reproduction

M0 must reproduce the **approved visual result and interaction structure**, not copy the prototype's accumulated CSS/JS patch chain literally.

M0 must preserve:

- screen hierarchy and top-level IA
- major layout composition and density
- navigation
- responsive behaviour
- modal/drawer/empty-state interaction shapes
- status semantics
- help-tooltip behaviour
- platform-identity presentation
- accessibility improvements visible in v29

M0 must **not** transplant:

- v18/v22/v27 help-system generations as separate implementations
- prototype runtime/demo state
- prototype business rules
- old patch-specific CSS/JS owners
- inline/demo platform data as canonical marketplace truth

Instead, M0 creates a small design-token layer (`tokens.css` or equivalent) for typography, spacing, radii, surfaces and common controls. Token consolidation is allowed only when it preserves the approved v29 visual result materially. Any meaningful visual deviation must be documented in M0 acceptance evidence.

This ruling is also recorded in `docs/adr/0003-ui-reproduction-strategy.md`.

## Architect ruling — registration automation controls

The three prototype toggles do not grant unrestricted runtime authority.

- `등록 실패 자동 재시도`: may automate only errors classified as `TRANSIENT` or `RATE_LIMITED`. `UNKNOWN` CREATE outcomes must reconcile first; `VALIDATION` and `POLICY_BLOCKED` are not blindly retried.
- `카테고리 자동매칭`: may auto-apply only an already accepted/persisted `CategoryMapping`. AI candidate ranking alone cannot finalize a category.
- `자동 가격조정`: may recompute/propose a price automatically, but may not silently write a live marketplace price merely because supplier facts changed. Live update authority is a later REGISTER/OPERATE policy decision.

For M0 these controls are visual shell only and cause **zero external writes**.

## Architect ruling — BLOCKED visibility

`BLOCKED` remains a first-class ComplianceGate state. A sixth registration KPI is **not required for M0**.

By M5, BLOCKED must be discoverable without opening raw logs through:

- registration preflight/readiness
- registration list/filter or review queue
- dashboard/review count where applicable

It must never be folded into a generic successful/failed state in the domain model.

## Rules

- This HTML prototype is the visual/product UI reference for ICBM-NEW.
- Legacy repository UI implementations, including old `ICBM-PROJECT` / #86 functional implementation, are **not** implementation sources for ICBM-NEW.
- Do not copy legacy functional owners, API bindings, DB assumptions, handlers, or runtime state from old UI code.
- Functional behaviour must be connected fresh to ICBM-NEW application contracts.
- If prototype JavaScript conflicts with `ROADMAP.md`, accepted ADRs, or `docs/ARCHITECTURE.md`, the canonical architecture wins. Prototype JavaScript is demo interaction only.
- When the user approves a later prototype revision, record filename, SHA-256, size, review decision and repository-copy verification before implementation switches to it.

## Current visual top-level IA

1. 대시보드
2. 수집관리
3. 통합DB
4. 등록관리
5. 주문관리
6. 문의관리
7. 품절확인
8. AI 쇼핑 인사이트
9. 분석
10. 설정

## Important distinction

`visual source of truth != runtime source of truth`

The UI controls presentation and interaction shape. Runtime truth belongs to the new services/contracts defined in ICBM-NEW.

Prototype counts, product rows, statuses, JavaScript demo behaviour and logo registry data are not runtime owners.
