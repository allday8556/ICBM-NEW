# ICBM-NEW UI Source of Truth

## Canonical visual source

The current approved visual prototype is:

`icbm_redesign_test_v28_icbm_new_gaps.html`

Attached prototype fingerprint:

- SHA-256: `3689b86c8c06fb10c5337ab661e8eae8712e1f04bb5afbaa0b1896e9729eba63`
- Size: `298398` bytes
- Revision: `v28 ICBM-NEW required UI gaps`

This revision supersedes `icbm_redesign_test_v27_global_help_tooltips.html` as the visual source of truth.

## What v28 adds

v28 closes the UI gaps required to begin fresh implementation:

- runtime-ready zero-data/empty states
- Collection Management `수집 / 공급처 관리` split
- supplier connection cards and credential/add-supplier surface
- product detail editor modal
- source-evidence/product-facts presentation
- option/SKU and image/detail editing surfaces
- registration preflight/readiness checklist + marketplace preview
- registered SmartStore read-back detail surface
- collection/registration failure detail drawer
- operation/history modal wiring

The prototype is a visual/interaction contract only. Demo numbers, mock statuses and prototype JS are not runtime truth.

## Rules

- This HTML prototype is the visual/product UI reference for ICBM-NEW.
- Legacy repository UI implementations, including old `ICBM-PROJECT` / #86 functional implementation, are **not** implementation sources for ICBM-NEW.
- Do not copy legacy functional owners, API bindings, DB assumptions, handlers, or runtime state from old UI code.
- The new implementation may reproduce the approved visual structure, spacing, navigation, responsive behavior, tooltip behavior, modal/drawer behavior and screen hierarchy from this prototype.
- Functional behavior must be connected fresh to the new ICBM-NEW contracts.
- If prototype JavaScript conflicts with `ROADMAP.md` / `docs/ARCHITECTURE.md`, architecture wins. The JS is demo interaction only.
- When the user uploads a revised approved HTML, that approved revision replaces this prototype as the visual source of truth. Record filename/hash before implementation.

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

## Repository copy status

The canonical filename/hash are now registered here. The raw HTML file itself should be placed under `ui/prototypes/icbm_redesign_test_v28_icbm_new_gaps.html` when copied into the repository; implementation must verify its SHA-256 matches the value above before using it as the shell.
