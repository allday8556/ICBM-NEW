# ICBM-NEW UI Build Gaps

Basis: `icbm_redesign_test_v27_global_help_tooltips.html`

Goal: keep the current visual language and only add UI that is required to implement the new clean architecture. Do not redesign screens that are already sufficient.

---

# P0 — build before the first vertical

## 1. First-run / empty-state set

ICBM-NEW starts with an empty DB and zero registered marketplace products. The current prototype is filled with demo counts/rows, so every major page needs a real zero-data state.

Required empty states:

- Dashboard — no supplier connected / no marketplace connected / no products yet
- Collection Management — no collection jobs yet
- Integrated DB — no products yet
- Registration Management — no registration candidates / no registered products
- Order Management — no synced orders
- Inquiry Management — no synced inquiries
- Sold-out Confirmation — no stock review items
- Analytics — not enough data yet
- AI Shopping Insight — no internal sales/product history yet

Each empty state should have one obvious next action, not explanatory paragraphs.

Examples:

- `공급처 연결하기`
- `첫 상품 수집하기`
- `스마트스토어 연결하기`

---

## 2. Supplier Connection Management UI

Current prototype only has a supplier dropdown + `로그인됨` chip on Collection Management. That is not enough for the new CONNECT layer.

Do **not** add a new top-level menu. Keep it inside `수집관리` as an internal view/tab such as:

`수집 | 공급처 관리`

Required supplier list/card fields:

- supplier name
- domain/base URL
- connection state
- authentication state
- collection-profile state
- last verified time
- last collection time
- actions

Required states:

- 미연결
- 자격증명 저장됨
- 로그인 확인 필요
- 로그인 성공
- 세션 만료
- 사이트 분석 필요
- 수집 규칙 준비됨
- 오류 / 재확인 필요

Required actions:

- 공급처 추가
- 로그인 정보 등록/수정
- 연결 테스트
- 로그인 다시 확인
- 사이트 열기/분석
- 수집 규칙 확인
- 비활성화

Credential modal UI:

- ID
- password
- save
- test connection
- masked existing credential state
- never display the stored password back to the user

This screen is the UI owner for CONNECT on the supplier side.

---

## 3. Canonical Product Detail / Editor

The current Integrated DB has a compact side detail, but the new system needs a real product inspection/edit surface.

Open it from `통합DB > 상세 편집`.

Recommended tabs/sections:

1. `기본정보`
   - canonical product ID
   - supplier
   - source product ID
   - source URL
   - original name
   - brand / manufacturer / origin

2. `수집 근거`
   - purchase price evidence
   - supplier shipping evidence
   - minimum sale price evidence
   - sold-out evidence
   - collected timestamp
   - raw/source value vs normalized value

3. `옵션 / SKU`
   - atomic supplier SKU table
   - option hierarchy
   - quantity tiers
   - SKU identity
   - cost by SKU where present
   - review-required markers

4. `이미지 / 상세`
   - representative images
   - detail images
   - detail content preview
   - product-information notice source images/data

5. `가격`
   - purchase price
   - supplier shipping
   - total acquisition cost
   - minimum sale price
   - target-margin calculated price
   - final price
   - price basis
   - channel price/margin preview

6. `등록 준비`
   - category
   - notice
   - options
   - images
   - shipping/returns
   - prohibited/sold-out state
   - platform readiness summary

Keep explanations in `ⓘ` tooltips. The main body should show actual data/status.

---

## 4. Registration Preflight / Preview

The current Registration Management screen shows queue/progress, but before marketplace CREATE the user needs a per-product preflight screen.

Open from a product row or `등록 확정`.

Required layout:

### Header

- product name
- canonical ICBM product ID
- selected marketplace
- readiness status

### Readiness checklist

- source facts
- category
- required options
- product information notice
- images
- price / price basis
- shipping
- returns/exchange
- prohibited/sold-out check
- marketplace-required fields

Each item: `PASS / 확인필요 / BLOCKED` + exact reason.

### Marketplace preview

- final product name
- category
- option structure
- representative image
- selling price
- shipping
- notice summary
- tags/keywords where supported

### Actions

- AI 재추천 (only where relevant)
- 수정하러 가기
- 등록 실행

After registration succeeds, the same UI should show read-back:

- marketplace product ID
- marketplace state
- registered price
- last read-back time

---

## 5. Registered Product Read-back Detail

The prototype has registered-product list examples, but the new operation spine needs one canonical detail view after registration.

For the first vertical, design **SmartStore only**. Coupang/11st details can be added later.

Required fields:

- canonical ICBM product ID
- marketplace product ID
- marketplace status
- marketplace current price
- last synced/read-back time
- source supplier/product link
- last stock check
- registration history
- last sync error if any

Required actions:

- 상태 동기화
- 가격/정보 read-back
- 원본 상품 보기
- ICBM 상품 보기

Do not design marketplace-delete as a default primary action.

---

# P1 — after first CONNECT → COLLECT → DB → REGISTER vertical works

## 6. Failure / Retry Detail Drawer

Collection and registration already show failure rows, but a common detail drawer is needed for real operations.

Show:

- failed step
- human-readable reason
- time
- supplier/marketplace
- product ID / URL
- retry eligibility
- previous attempts
- latest evidence/read-back

Actions:

- 재시도
- 원본 보기
- 상품 보기
- 보류

---

## 7. Sync / Activity History

A lightweight history drawer/modal for:

- supplier connection checks
- collection runs
- registration attempts
- marketplace read-backs
- stock sync

This is not a developer log screen. Only user-relevant operation history should appear here.

---

# Existing prototype UI that is already sufficient as the visual base

Do not redesign these now unless a real implementation requirement proves a gap:

- top-level navigation / shell
- dashboard layout
- collection job list + preview basic layout
- integrated DB list/KPI/filter layout
- registration queue / failure / history basic layout
- order management basic layout
- inquiry management basic layout
- sold-out review basic layout
- AI Shopping Insight visual structure
- analytics visual structure
- common settings IA
- SmartStore/Coupang/11st API form visual language
- global `ⓘ` tooltip pattern

---

# Build order for the user UI work

Please build in this order:

1. **First-run / empty states**
2. **수집관리 > 공급처 관리**
3. **통합DB > 상품 상세/편집**
4. **등록관리 > 등록 Preflight/Preview**
5. **등록상품 > SmartStore read-back 상세**
6. Failure/retry detail (P1)
7. Operation history (P1)

The first five are enough for Claude to implement the first real vertical without inventing UI during coding.
