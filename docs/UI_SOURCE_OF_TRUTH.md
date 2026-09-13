# ICBM-NEW UI Source of Truth

## Canonical visual source

The current approved visual prototype is:

`icbm_redesign_test_v27_global_help_tooltips.html`

Attached prototype fingerprint:

- SHA-256: `2ca7413faa44a248c04d5e5dae3fb22b738b0011a23bb0412788f8f1ab90895e`
- Size: `258116` bytes

## Rules

- This HTML prototype is the visual/product UI reference for ICBM-NEW.
- Legacy repository UI implementations, including the old `ICBM-PROJECT` UI rebuild work, are **not** implementation sources for ICBM-NEW.
- Do not copy legacy functional owners, API bindings, DB assumptions, handlers, or runtime state from old UI code.
- The new implementation may reproduce the approved visual structure, spacing, navigation, responsive behavior, tooltip behavior, and screen hierarchy from this prototype.
- Functional behavior must be connected fresh to the new ICBM-NEW contracts.
- When the user uploads a revised approved HTML, that approved revision replaces this prototype as the visual source of truth. Record its filename/hash before implementation.

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
