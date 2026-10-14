# ICBM-NEW UI Source of Truth

Status: **APPROVED — CANONICAL**
Architect approval: **2026-09-13**

## Canonical visual source

The current approved visual prototype is:

`design/prototypes/icbm_redesign_test_v29_final.html`

Verified prototype fingerprint:

- SHA-256: `896ad87011b8615b8a6a9cd3e790ca04f52e908e4ff7b6a26ea4bf5372dfeb82`
- Size: `323751` bytes
- Git blob SHA: `2dea109fa819123601e23581d7b8b88e52507713`
- Revision: `v29 — defect fixes + unified platform identity`
- Architect review source: `documents/archive/reviews/V29-CHANGES-BY-CLAUDE.md`
- Repository copy: **VERIFIED PRESENT on `main`**

v29 supersedes v28. v28 superseded v27.

The uploaded v29 attachment and the repository copy were independently verified. The local uploaded file has SHA-256 `896ad87011b8615b8a6a9cd3e790ca04f52e908e4ff7b6a26ea4bf5372dfeb82`, size `323751` bytes, and Git blob SHA `2dea109fa819123601e23581d7b8b88e52507713`; the GitHub repository copy reports the same Git blob SHA. Therefore the repository copy is byte-identical to the approved attachment.

## Extension Collector visual source

The Chrome extension's side panel is a separate surface (ADR-0019 §12.1), not part of the v29
application. Its approved visual prototype is:

`design/prototypes/icbm_extension_collector.html`

- Approved by the user as `ICBM 확장 수집기.html`, unchanged since approval (user instruction of
  2026-10-02).
- SHA-256: `5eec99911aca06a857ea5b5460c77ac25f0270a4384e7257a81b5466b6bdb879`
- Size: `7949739` bytes
- Git blob SHA: `16679dfbf730ba2948252d57bf2800c5dcc5ac02`
- Repository copy: byte-identical to the approved file (same SHA-256 and size).
- Scope:
  - The side panel reproduces the boards `확장 — 상품 상세 수집`, `확장 — REVIEW 포함` and
    `확장 — 예외와 AUTH 중단` structurally (ADR-0003).
  - From E3 (ADR-0019 §8.1) it also reproduces `확장 — 목록 발견과 대기열` structurally.
  - The three `수집관리` boards depict the application's Collection Management, which this record
    does not change.
- Where a board shows what a canonical rule forbids, the rule wins:
  - no resend of an unsent capture (ADR-0019 §12.6);
  - no supplier-session state the extension does not know;
  - a queue row's chip is the item's own state or its run's own outcome, never `REVIEW`
    (AC-21, AC-31);
  - no pre-filled queue bound: the board's `20` and `[간격]` are placeholders, and an empty
    bound is refused, never defaulted (ADR-0019 §8.1).

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

This ruling is also recorded in `documents/decisions/adr/0003-ui-reproduction-strategy.md`.

## Architect ruling — registration automation controls

The three prototype toggles do not grant unrestricted runtime authority.

- `등록 실패 자동 재시도`: may automate only errors classified as `TRANSIENT` or `RATE_LIMITED`. `UNKNOWN` CREATE outcomes must reconcile first; `VALIDATION` and `POLICY_BLOCKED` are not blindly retried.
- `카테고리 자동매칭`: may auto-apply only an already accepted/persisted `CategoryMapping`. AI candidate ranking alone cannot finalize a category.
- `자동 가격조정`: may recompute/propose a price automatically, but may not silently write a live marketplace price merely because supplier facts changed. Live update authority is a later REGISTER/OPERATE policy decision.

For M0 these controls are visual shell only and cause **zero external writes**.

## Architect ruling — BLOCKED visibility

`BLOCKED` remains a first-class ComplianceGate state. A sixth registration KPI is **not required for M0**.

**Scope correction — post-merge full-audit architect resolution (Issue #89 `5851284598`, R1).** The M5
requirement below was over-broad as originally written: read literally it made a production
ComplianceGate owner, a `COMPLIANCE` review count, a registration list/filter screen and a
review-queue view of these states M5 requirements. The ComplianceGate part `documents/architecture/ARCHITECTURE.md` §7
("**No production ComplianceGate owner exists yet** … Gate 3 implements no ComplianceGate"),
`documents/roadmap/ROADMAP.md` §14.1 and ADR-0018 §5 assign to a **separate later owner** under its own contract and
authorization; the list/filter and review-queue part no authorized M5 slice owns at all. That
ownership boundary wins; the requirement is restated accordingly and narrowed to the surfaces that
M5 actually authorized.

By M5, the **currently implemented server-owned registration safety/readiness `BLOCKED` states** must
be discoverable without opening raw logs, through the only surfaces that own those states:

- registration preflight/readiness
- the derived canary readiness verdict with its per-requirement reason codes (`app/stages/register/canary.py`)

That is the whole requirement. M5 requires **no** further BLOCKED-visibility surface: no registration
list/filter screen, no review-queue view of these states, and no dashboard or review count of them.
The Gate 2 review path is not such a surface and this requirement adds nothing to it — it indexes
only its own producers' owner-derived conditions and their coverage (ADR-0016, `documents/architecture/ARCHITECTURE.md`
§9). Any later list/filter, queue or count over these states needs its own contract and
authorization.

> **B-UX1 (owner directive 2026-10-04).** The registration readiness summary is such a count, under
> its own contract (ADR-0014 §22 amendment note): the pre-send registration units only, evaluated at
> read time by the preflight owner, with `NOT_EVALUATED` kept apart from the five readiness
> statuses. It is not a review count, a review queue or a ComplianceGate surface, and it adds
> nothing to the Gate 2 review path.

> **B-UX2 (owner directive 2026-10-04).** The fix-only projection of Registration Management is
> another such surface under its own contract (ADR-0014 §22 amendment note): it lists only what the
> operator can fix or re-check now and where, from server-owned actionability, and counts the rest.
> It is not a review queue: it resolves no item and fixes nothing, and its review rows are only the
> REGISTER execution producer's open items.

> **B-PREVIEW (owner decisions `5975647306`).** The kept "marketplace preview" is the frozen-Snapshot
> preview (ADR-0014 §22 amendment note): the prototype's category name path and shipping fee have
> no Snapshot source and are not shown; provider image URLs are never shown.
> Since ADR-0033 G6 a composition-v3 Snapshot's preview also shows its frozen notices in the order
> `detail-renderer/v2` draws them: the `상단 공지` slots (`data-role="preview-guidance-top"`) before
> `상세 이미지`, the body paragraphs after it, and the `하단 공지` slots
> (`data-role="preview-guidance-bottom"`) last. Each slot is the notice's image from the Detail
> Guidance owner's local image route (never a provider URL, never stored in the Snapshot), labelled
> `기간 공지` with its period, `상시 공지` or `이 상품 공지`, or plain `공지` when the owner no longer
> holds that frozen revision as its newest, and whether its marketplace upload is prepared. A
> placement without a notice, and every v1/v2 preview, shows no slot and is unchanged.

> **B-EDITOR (Issue #127 direction; 2026-10-07).** The kept "product detail editor" is the
> registration unit's workspace in Registration Management. It has four sections — 상품 정보, 이미지,
> 가격, 등록 준비 — and a jump bar.
> - Every section stays rendered; the bar hides nothing, so the Gate-3 selectors stay visible.
> - Each section acts only through its own owner: the preparation routes, the image owner's
>   selection and auto-selection routes, and the B-PRICE1 re-pin.
> - It is not a second product record. An image section shows only the Item's CONFIRMED source
>   images of its bound revision, and every image is decided explicitly.

> **B-STATUS (2026-10-07).** The registration status card and panel stay the ADR-0014 §28.5
> partition.
> - Every batch that still holds an open Intent is included whatever its age, so 등록중 and
>   재확인필요 are never pushed out of the window. Only 등록성공 and 등록실패 are windowed.
> - A row shows the latest Attempt's own cause apart from the reconcile result.
> - A row offers "거절 확인 · 미등록 처리" only when the server says the UNKNOWN is a settleable
>   provider 400 rejection (Issue #219 `6031580064`).

M5 does **not** require a production ComplianceGate owner, a `COMPLIANCE` `ReviewItem` producer or a
`COMPLIANCE` dashboard count. `COMPLIANCE` stays `NOT_WIRED` — never an authoritative zero — until a
separately authorized ComplianceGate owner is implemented and accepted (ADR-0018 §5, `documents/roadmap/ROADMAP.md`
§14.1). Regulated-category automation stays forbidden, and the first bounded canary stays restricted
to a product whose reviewed category metadata proves it outside every regulated category: eligibility
evidence for that canary alone, never a `COMPLIANCE PASS` verdict.

It must never be folded into a generic successful/failed state in the domain model.

## Collection Management — 수집 사실 and the run filters (A-UX1, owner decision 2026-10-04)

v29 shows the source-evidence comparison (`수집 근거 / 정규화 비교`: per field, source → normalized
→ status) inside the product editor modal, and a `수집 미리보기` beside the collection list. The app
reproduces that structure in 수집관리, where the focused run and its read-back revision are separate,
adjacent panels so either block remains readable within the viewport:

- per field: its status (`확정` / `없음` / `확인 필요`), its stored value only when `CONFIRMED`, and its
  stored evidence on demand (kind, locator, observed, normalized, status, digest), plus the
  extractor revision, the transport and the image count included / total, all as the revision holds
  them;
- each image reference's recorded disposition reads `포함` / `제외` / `확인 필요`, and its recorded
  reason is worded beside its codes — the exclusion row (`원천이 http 주소로 적은 이미지`), else the
  transport's target refusal (`가져오기 전 거절: …`), else the issue (`파일 크기 제한 초과`, …). The
  words name codes the revision already holds; the screen judges nothing and shows an unknown code
  as itself (A-NEXT1, 2026-10-04);
- a field's value/evidence row and the image-reference table exist in the document only while the
  operator has expanded them. Collapsing removes that detail instead of leaving hidden server-owned
  state behind for the visual acceptance surface;
- when the run's revision is the current bound revision of a Product DB Item, `공통 이미지 제외` counts
  that Item's auto-selection notes (`COMMON_IMAGE_BLOCKED`, `COMMON_IMAGE_UNDECIDED`) and the image
  table adds `자동 선택`, read from the Item's own `image-candidates` preview (Issue #219's owner).
  Without such an Item it says so and shows nothing in its place: a supplier's common-image list is
  never shown as this product's exclusions (A-NEXT2b, 2026-10-07);
- the prototype's percentage chip (`94%`) is demo content and is **not** reproduced: no confidence
  number is shown, and the screen decides no status and changes no fact;
- the run's outcome and the revision's facts status stay two labelled axes, and `NO_REVISION` is
  shown as its own answer with no facts status;
- the recent-runs list is filtered by v29's five state cards — `전체 수집 / 진행 중 / 기록됨 / 실패 /
  원천 확인 필요` — and by `기록할 식별자 없음`, which has no v29 card and sits beside the list's count.
  Each card shows the server's own total for its filter. The server applies the filter before it
  orders and bounds the list, reports how many runs the filter selects in all, and pages on with
  `더 보기`; a page is never shown as the whole list.

## Collection Management — the v29 수집 layout (owner decision 2026-10-07)

The 수집 view reproduces v29's composition (ADR-0003, "major layout composition and density"): the
one-row toolbar, the five state cards, the `수집 작업 목록` beside `수집 미리보기`, then the focused
run and its 수집 사실 as panels below.

- **A v29 slot the system has no source for keeps its place and reads `데이터 없음`** (owner decision
  2026-10-07, option 가): the cards' `전일 대비`, the list's `후보수 / 저장수 / 가격확인`, the preview's
  `AI 학습 노트`, and the preview's representative image until the Product DB holds an Item bound to
  the run's revision. Nothing is estimated, counted in the page or filled from demo content.
- v29 controls with no contract yet keep their place and only explain themselves when pressed
  (`markInert`), sending nothing: the collection-option chips (`상세페이지 수집 / 옵션/색상 수집 /
  이미지 다운로드`), `↻ 재검증` and `✨ AI 추출 보정`.
- `수집 미리보기` shows the focused run's revision as stored: the name, the supplier, `도매가 / 배송비 /
  최저판매가 / 옵션 수` from their fields (a field's recorded status when it has no confirmed value),
  the included detail images, the run's outcome and facts status, and the price/option/image field
  statuses. Its image is the bound Item's own image read
  (`GET /api/v1/products/items/{item_id}/images/{sha256}`).
- The toolbar's chip is CONNECT's own verdict for the chosen supplier, as before.

## Navigation and recovery (A-UX3, owner decision 2026-10-04)

Small additions beside the approved boards, in their existing visual language:

- Extension list mode: a captured row offers `수집관리에서 결과 보기`, which opens that row's own run in
  Collection Management (`#/collect?view=jobs&run=…`) through the same worker path as the
  single-capture `수집관리에서 보기`, which stays as it is.
- Extension list mode: once a queue has ended and ICBM holds no open queue, a note explains recovery —
  go back to the list page yourself, `목록 찾기` again with `이미 수집한 상품 건너뛰기` on. ICBM skips what
  it recorded and declares a new queue; the ended queue is never reissued, the bound is typed again
  (never pre-filled), and the extension never navigates to the list page itself.
- Collection Management: a `FAILED` run offers `다시 수집`, which puts the run's supplier and URL back
  into the submit form and sends nothing. Only the operator's own submit makes a new run, through the
  one submit path and its server rules (the target check and the same-product interval included);
  the failed run stays as recorded. `NO_REVISION` and `RECORDED` runs offer no such control.

## Product DB and Registration Management — the v29 layout (owner decision 2026-10-07)

The same rule as the 수집 layout applies here. v29's composition is reproduced (ADR-0003). A slot with no source keeps its place and reads `데이터 없음`. A v29 control with no contract is `markInert` and sends nothing. No screen-contract API changes, and every existing panel keeps its function and markup.

### 대시보드

- **Cards.** v29's five cards, each a way into its screen.
  - `전체 수집` is the collection runs' server total.
  - `판매 대기` is `registration_candidates_total`.
  - `판매 완료 / 등록 가능 / 확인 필요` and every `전일 대비` read `데이터 없음`.
- **`최근 수집 현황`.** The newest five runs, as the server lists them. A RECORDED run's revision names the product.
- **`플랫폼별 등록 현황`.** The ring holds the server's `registrations_total`. No read breaks registrations down by marketplace, so each marketplace keeps its place in the legend with `데이터 없음` and the ring shows no split.
- **`주문/알림 요약`.**
  - `품절 확인 필요`, `등록 오류` and `가격 변동 알림` are the STOCK, REGISTRATION_ERROR and SOURCE_CHANGE review counts, shown only when the server states them as current.
  - The order counts, `재고 부족 상품` and `시스템 알림` read `데이터 없음`.
- **`오늘 상품 유입`, `일별 수집/등록 추이` and `주요 카테고리 TOP 5`** read `데이터 없음`.
- **The review-count table and the connection summary follow as before.**
- The breakpoints follow v29's own: three panels on a wide landscape screen, `1.6fr 1fr` with the last panel full width on a portrait screen, one column on a narrow one. The 수집 list and its preview sit side by side in portrait from 1200px, not v29's 901px, because the work list has more columns than v29's. 통합DB's detail and 등록관리's settings move under their list as one wide card of columns in portrait, as v29 does.

### 통합DB

- **Cards.** v29's five cards: `전체상품 / 등록가능 / 확인필요 / 품절 / 금지상품`. `전체상품` is the server's `products_total`; the other four have no owner yet and read `데이터 없음`.
- **Filter row.**
  - The search field and `검색` keep their server search.
  - v29's `카테고리 / 플랫폼 / 공급처 / 가격기준` filters and the toolbar's bulk `✨ AI 추천` are inert. Bulk AI keeps its own stage (ADR-0027).
- **List columns.**
  - The columns are v29's: `상품명 / 공급처 / 원가 / 배송비 / 최저판매가 / 가격기준 / 네이버 / 쿠팡 / 11번가 / 상태 / 등록유형`.
  - The name cell keeps the Product's id, member and Item counts, membership revision and creation time on its second line.
  - The supplier and the status come from the row. The price columns and `등록유형` read `데이터 없음`.
- **Detail panel.**
  - The title shows the representative image through the bound Item's own image read, the member's confirmed name, the id and the status.
  - v29's groups follow: `브랜드` from the member's facts; `가격 정보 / 채널별 판매가 / 마진율` read `데이터 없음`.
  - **`카테고리 / 카테고리 추천 신뢰도` (ADR-0029 C3).** These show the `SMARTSTORE_CATEGORY_V1` `category` result for the bound SmartStore account: the chosen leaf's whole name, the confidence, the candidate count, `검토 필요`, `최신` or `입력 바뀜`, and the model.
    - Its own `✨ AI 카테고리 추천` is live only while the `ai` capability is READY and such an account exists.
    - The candidates are the server's, from ICBM's durable official catalog.
  - **`태그 / AI 상태` (ADR-0028 T4).** The group shows the product's `SMARTSTORE_TAGS_V1` `tags` result for the SmartStore account bound to the connection (read from `/register/draft-targets`):
    - each tag with its source, `네이버 추천` or `AI 직접`;
    - what the filter removed and why;
    - when the restricted check ran;
    - `검토 필요`, `최신` or `입력 바뀜`, and the model.
    
    Its own `✨ AI 태그 추천` is live only while the `ai` capability is READY and such an account exists; otherwise it is inert with the reason. The platform is read by the server's job, never by the page. No tag is ever sent to the marketplace.
  - **`AI 상품명 추천` (ADR-0027 AIS-2).**
    - The group holds `✨ AI 추천` and the Product's target-free `product_name` result, as the enrichment owner reads it: the recommendation, its confidence, `검토 필요`, `최신` or `입력 바뀜 · <reasons>`, and the model that answered.
    - The button is live only while the `ai` capability is READY. Otherwise it is inert with the capability's reason.
    - A press asks the product-name task once. The server reuses a result whose inputs are unchanged; otherwise the panel reads the owner again until a newer result is recorded.
    - It applies nothing.
  - `상세 편집` is inert.
  - The membership, the members' facts, the Items, the review items, the target check and the Draft form follow as before.

### 등록관리

- **Cards.** v29's five cards, each a count the server holds:
  - `등록 대기` = `registration_candidates_total`;
  - `등록 완료` = `registrations_total`;
  - the registration status's own `등록실패` and `재확인필요`, with its labels;
  - `카테고리 확인` = the readiness summary's `CATEGORY` area.
  - A count no read states reads `데이터 없음`.
- **Cards bring their section into view instead of switching tabs.** v29 switches 등록 대기 / 등록상품 / 실패 / 등록 이력 as tabs. Here every section stays on screen, because the Gate 3 surface reads all of them on one route (ADR-0018 §9).
- **`등록 이력` row.** It sits beside `중단된 범위 n` and brings 실패 / 재시도 into view.
- **`등록 대기 목록` beside `플랫폼별 등록 설정`.**
  - One row per unit, as the server read it: the unit, its marketplace, its category id, its preparation and its read state. `옵션 상태 / 이미지 상태 / 진행률` read `데이터 없음`. `열기` brings the unit's own workspace into view; the rest of the row opens the 상품 편집기 choice.
  - v29's list tools are inert.
  - The readiness summary sits under the list.
  - The settings panel's defaults read `데이터 없음`. Its three toggles stay the visual shell the architect ruling on registration automation controls requires.
- **Sections, then, in v29's order.**
  - `등록상품 관리` holds the M6-A `판매 상태 동기화` panel, unchanged.
  - `실패 / 재시도` holds `고칠 것` and the registration status panel.
  - `실등록 안전장치` holds the canary and live panels.
  - `검토 항목`.
  - `등록 단위 작업 공간` holds each unit's own workspace, unchanged.

### 상품 편집기 (owner decision 2026-10-08, phase 1)

- **Opening.** A row of `등록 대기 목록` opens a choice: `상품등록` or `상품수정`.
  - `상품수정` needs the server's `REGISTERED` read state. `상품등록` is offered otherwise.
  - The choice sends nothing. The chosen work opens `#/register-editor?draft=&unit=&items=&mode=` in a new tab, without the application's sidebar and top bar.
  - A unit's ref moves when it is authored and again when it is frozen. The editor follows the one unit of the Draft that holds the same Items. Two units holding them are never guessed between.
- **Six steps.** Phase 1 places the register page's own pieces by step and adds no route:
  1. `기본정보`: the main image, the platform tabs (the target marketplace only; the others are inert), the category, the name, `AI 상품명 추천` and the tags.
     - For a SmartStore unit, `AI 카테고리 추천` sits under the category. `AI 카테고리 적용` writes the exact result as an `AI_SUGGESTION` selection under the target's current mapping revision.
       - It is refused under another taxonomy.
       - It never confirms a category: `AI 추천 적용됨 · 저장해야 확정됩니다`, and the operator's save confirms.
       - A confirmed category is never overwritten (ADR-0029 §5).
     - For a SmartStore unit, the tag block shows the unit account's tag recommendation (the 통합DB panel) and `AI 태그 적용`.
     - `AI 태그 적용` applies the exact result revision as one `AI_SUGGESTION` set of texts (ADR-0028 §6).
     - It is enabled only for a fresh OK result, on a saved Preparation with no confirmed tags. A confirmed set is never overwritten.
     - `AI 태그 적용됨 · 저장하면 확인됩니다` marks an applied set, and the operator's save confirms it.
     - Tag editing stays inert.
     - `AI 상품명 추천` shows the same recommendation panel as 통합DB.
     - `AI 추천 적용` applies the exact result revision shown to the name as `AI_SUGGESTION`, against the Preparation revision read (ADR-0026 §7).
     - It is enabled only for a fresh OK result, on a saved Preparation whose name holds no confirmed value. A confirmed name is never overwritten.
     - `AI 추천 적용됨 · 저장하면 확인됩니다` marks an applied name. The operator's own save makes it `OPERATOR_CONFIRMED`.
  2. `대표이미지`: the Item image editor. The bulk `배경 이미지 제거 / 리사이즈 / 이미지 편집` are inert. The bulk `이미지 자동번역` follows the Image Studio capability (below). `자동 선택 다시 실행` is the image owner's own.
  3. `옵션·가격`: the repin and the Items table. The price facts no read states read `데이터 없음`, and the option tools are inert.
  4. `상품정보고시`: the category requirements and their fields.
  5. `상세페이지`: the image-editor mode. `HTML 방식` is inert. The body is the authoring form's. `이미지 자동번역` follows the Image Studio capability (below). `미리보기` opens the snapshot preview, frozen notices included (G6). The top and bottom notices (ADR-0033 §5, G4):
     - `상단 공지` and `하단 공지` each show the notices the server resolves for the unit now (`GET /api/v1/register/detail-guidance`), in order — the active period notices, then the product's own notice or the store-wide standing one — each with its image, `기간 공지 / 상시 공지 / 이 상품 공지` and its template. With none, the group says so.
     - `기본값 / 끄기 / 직접 작성` choose the placement's mode. A changed mode says it applies once the preparation is saved; the list stays the server's for the saved choice.
     - `직접 작성` shows the settings card's own editor (`components/guidance-editor.js`): the same inputs, five-template live preview and `국내배송 / 해외배송` presets. The save is refused locally, sending nothing, until its text and template are chosen; the server validates and draws the text when the preparation is saved.
     - The choice is saved with the preparation through the one `/preparations` write. Any other surface sends the stored choice back unchanged.
     - While a group has a resolved notice that the unit's composition does not place, it shows the server's `PUBLICATION_GUIDANCE_UNPLACED` reason (BLOCKED). Since G5 the SmartStore composition v3 places both notices, so the reason remains only for a unit authored against an older composition revision: saving the target policy again and then the preparation clears it.
  6. `기타 등록정보`: the preflight, the attempts, the scope brake and the unit's own actions.
- **One save.** The authoring form stays one form, and its parts are bound back to it. The editor's `준비 내용 저장` is the register page's one `/preparations` write.
- **`등록 준비` strip.** A one-line strip sits at the bottom. Each step is coloured from the server's preflight reasons only, by the first area each reason names:
  - green: no reason;
  - orange: a reason that is neither BLOCKED nor DUPLICATE;
  - red: a BLOCKED or DUPLICATE reason;
  - grey: no preflight yet.
  - Hovering a step lists its reasons; clicking it moves to that step.
- **AI controls are inert placeholders.** This covers `배경 이미지 제거` in 대표이미지, under owner decision 2026-10-08 (Issue #219 comment `6054956408`; ROADMAP, Issue #127). It keeps its approved position, disabled and marked 준비 중, and sends nothing. It becomes active only with its server owner. The two `이미지 자동번역` buttons have theirs since ADR-0041 I5, and stay such placeholders while it reads `NOT_CONFIGURED` (below).
- **Edit mode is read-only.** No contract yet edits a registered listing on the marketplace. `상품수정` shows the registered unit's contents and says so. A unit with a Snapshot or an Intent is never authored in the editor.
- **The register page is unchanged.** Every section the Gate 3 surface reads stays on `#/register`.

### 상품 편집기 › 이미지 자동번역 (ADR-0041 I5)

- **Two buttons, one panel.** `✨ 이미지 자동번역` sits in ② 대표이미지's bulk bar (the images the operator ticks in the panel, any role, D3) and in ⑤ 상세페이지 (the panel opens with the Item's detail images ticked). Both open the same Image Studio panel (`components/image-studio.js`) for the unit's Items.
- **Live only when READY.** The buttons read `GET /api/v1/image-studio/capability`. While it reads `NOT_CONFIGURED` they keep their place as inert 준비 중 placeholders (`data-ai-placeholder`), send nothing, and their tooltip names what is missing: no OCR engine (`NoOcrProvider`, until I7), no translation task, or no AI provider (an approved CLIProxyAPI profile with its data-transfer approval). Production reads `NOT_CONFIGURED`. On a unit with a Snapshot or an Intent they stay inert too.
- **The run.** The panel shows every CONFIRMED source image of the chosen Item with what its selection does now (`원본 사용 · 대표/상세`, `번역본 사용 중`, `사용 안 함`, `쓰임 미정`) and whether its text was read. The operator ticks images (at most 30), picks `중국어 → 한국어` or `일본어 → 한국어` and presses `번역 시작`: one `image.translate` job, read until it ends. Only text is sent; the copy says so.
- **One image's review.** `검토` opens it:
  - the original and the live preview side by side (a stateless `POST …/preview` PNG; nothing is stored), with `미리보기 · 완전함` or what overflowed;
  - the candidate's state: `AI 번역 제안 · 최신`, or `제안이 최신이 아님 · <what changed>` with no suggestion offered, or `번역 없음`;
  - one row per region: its id, the source text, the editable Korean text, the confidence, the flag badges (`인식 신뢰도 낮음`, `숫자 · 단위 빠짐`, `연락처`, `다른 언어`, `복잡한 배경`, `칸에 넘침`, `그릴 수 없는 글자`), `번역 적용` and, on a flagged row, `확인함`.
- **Defaults and confirmation.** Every row starts as `원문 유지`. `모두 적용` includes every unflagged row with a text and never a flagged one: a contact detail and a complex background stay kept by default. A flagged row included needs its own `확인함`. `적용` stays disabled, saying which region still needs it, until every included flagged row is confirmed and the preview is complete. The server checks the same again.
- **적용 → 확인 완료.** `적용` saves the edit revision against the sequence it read, renders it and records the translated image through the M4 image owner. The panel then shows that exact image and `확인 완료`, which records the operator's PASS on its SHA-256 and only then uses it in place of the source (`USE_DERIVED`, operator origin); every other image keeps its decision and place. `사용 중인 번역본` shows the image in use.
- **되돌리기.** It uses the source image again (`USE_SOURCE`). Nothing is deleted.
- **Layout.** Neither the screen nor the panel scrolls sideways at 1920×1080 or 390×844; on a narrow screen the original and the preview, and each region row, stack.

### 설정 › AI / Prompt (ADR-0026 AIF-1)

- **AI Prompt Registry.** The card keeps v29's four sections: 공통 규칙, 역할, 플랫폼 정책 and 작업.
  - Each entry reads its current revision from the server's PromptTemplate or PlatformPolicy store and shows it as `v{n} · 기본값` or `v{n} · 사용자 수정본`.
  - Clicking an entry opens the v29 layered prompt editor on that entry's role × policy × task.
- **Editor.** The editor has v29's seven tabs: 공통 규칙, 역할 Role, 플랫폼 Policy, 작업 Task, 출력 형식, 입력 변수 and 조립 미리보기.
  - The state chip reads 기본값, 사용자 수정본 or 저장되지 않은 변경.
  - `현재 계층 저장` and `↺ 현재 계층 초기화` write one revision of the current tab only.
  - The preview is the server's composition of the saved layers.
- **Other entry points.** Each marketplace tab's `Policy 편집` button opens its platform policy. AI 쇼핑 인사이트's `✨ Shopping Insight Agent 설정` opens its role and task. Both only edit prompts.
- **AI 공급자 row.** The "AI 기본 설정" card shows a row that reads the readiness `ai` capability (ADR-0026 AIF-2). With no provider it reads `미설정 · AI 공급자 없음`.
- **외부 검색 신호 (ADR-0028 §3).** The card also shows the `search_signal` capability as readiness reports it: `연결된 출처 없음` while the port is provider-zero.
- **AI 공급자 card (ADR-0027 AIS-1).** The card holds the operator's CLIProxyAPI profile: a loopback endpoint, the owner's model (`gpt-5.6-sol`, shown read-only; any other is refused), the billing mode and the daily call cap. Moving the endpoint or the model withdraws the data-transfer approval.
  - It shows the approved executable and routing identity beside what is actually serving now. Each approve button names what is served now and is a protected, audited server action.
  - It holds the data-transfer approval, today's calls and the capability state (`연결됨`, `승인 필요 · …`, or the mismatch reason).
  - The ICBM-dedicated client key is typed once into a password field. The server writes it to the OS secret store, and it is never shown again: the card shows only `저장됨 · <time>` or `없음`.
- **Still inert.** The "AI 기본 설정" toggles stay inert placeholders until their stage.

### 설정 › 공통 › 상세페이지 공지 (ADR-0033 G3)

v29 has no surface for the detail page's notices. They get their own 공통 sub-tab, `상세페이지 공지`,
with one card in v29's card language. The card reads `GET /api/v1/settings/detail-guidance` and
only POSTs to that owner's routes.

- **Two sections.** `상단 공지` (above the detail images) and `하단 공지` (below the body). Each has
  a standing notice and its period notices.
- **상시 공지.**
  - The saved image (`image_url`), its template label, `사용 중` or `꺼짐`, and its revision.
    `공지 끄기 / 켜기` appends the same text and template with `enabled` flipped.
  - The inputs: 1–3 blocks, each an optional heading (at most 20 characters) and 1–6 lines (at
    most 40), with add/remove buttons inside those bounds. With no saved notice, the top opens with
    one empty block and the bottom with the editable headings `배송 안내` and `C/S 안내`.
  - `국내배송` and `해외배송` fill this section's inputs with the server's preset text and select
    its suggested template. Edited inputs are replaced only after a confirmation. A note says the
    text is an example to check against the seller's own conditions, with `○○` to be replaced.
  - The live preview: about 400 ms after typing, the server renders the five templates and the
    page shows them side by side, each selectable, with the chosen one shown larger. Choosing a
    template never changes the text.
  - A refusal (a URL or markup, an empty or too-long line, an undrawable character) shows the
    server's message under the inputs, marks the input it names and disables `상시 공지 저장`.
  - `상시 공지 저장` is enabled only when a template is chosen and the server previewed the text as
    it is now. It appends a revision against the sequence read. `GUIDANCE_CURRENT_MOVED` offers
    `다시 불러오기`; `GUIDANCE_UNCHANGED` says nothing changed.
- **기간 공지.** Each period notice shows its image, its period in local time and the server's
  status label (`예정`, `진행 중` or `종료`). `＋ 기간 공지 추가` opens the same inputs and preview
  plus `시작` and `끝` (`datetime-local`), sent as instants with the browser's offset. `조기 종료`
  appends `enabled = false`.
  - `수정` on a notice that is not `종료` opens the same inputs and preview under its row, filled
    with its saved text and template and with `시작`/`끝` in local time (an untouched field sends
    the saved instant). `기간 공지 수정 저장` appends the next revision of the same notice
    (`guidance_id`, `expected_current_seq` = the seq read, `enabled = true`); its kind and
    placement never change. `GUIDANCE_CURRENT_MOVED` offers `다시 불러오기`, `GUIDANCE_UNCHANGED`
    says nothing changed, and `GUIDANCE_PERIOD_INVALID` and text refusals show the server's reason.
  - A `종료` notice is read-only: it shows no `수정` and no `조기 종료`.
- **Server-owned.** Validation, the rendering, the presets, the revision numbers and each period's
  status belong to the Detail Guidance owner. The page computes none of them. A saved notice is
  resolved into every product whose choice is `기본값` (the editor's 상세페이지 step, G4). Since
  G5 a resolved notice reaches a SmartStore listing only as its uploaded image, placed by the
  frozen Snapshot above the detail images (상단) or below the body (하단), and only through a new
  registration; a registered listing is never updated by a notice change.

## Supplier common images (A-NEXT2a, owner decision 2026-10-04 option 1, Issue #231)

v29 has no board for Issue #219's supplier common images; they live with the supplier they belong
to, in v29's existing language: the 공급처 관리 supplier card and a modal like its credential surface.

- The supplier card shows `공통 이미지` with the owner's counts `확인 필요 / 차단 / 유지`, read from
  `GET /api/v1/products/supplier-common-images/{supplier}`, and a `공통 이미지` button.
- The modal lists every file the owner lists, in its order, each as a tile:
  - the preview, through the owner's read-only bytes route
    (`GET …/supplier-common-images/{supplier}/{sha256}/image`). A file no product shows yet is not
    asked for and reads `미리보기 데이터 없음`;
  - the verdict chip, the products it was found in, and the current decision as recorded: the
    owner's seed (revision 0) reads `초기 지정`, a later decision names its number and who made it,
    and the earlier decisions no read returns read `이전 결정 기록 데이터 없음`. An undecided
    candidate reads `확인 필요` and that the owner treats it as blocked until decided;
  - `차단` and `유지`, each the owner's own decide command
    (`POST …/supplier-common-images/{supplier}/{sha256}` with `{verdict, actor: "operator"}`). The
    list is read again after every answer, so a tile always shows what the owner now holds;
    pressing the verdict a file already has sends nothing.
- The screen detects nothing, counts nothing and decides no verdict. Only these three owner routes
  are used. This is the one write the 수집관리 screen adds besides the collection submit, and it is
  the Product DB owner's write, not COLLECT's.

## Collection Management — 공급처 관리 › 「+ 공급처 추가」 (ADR-0036 O2)

v29's `+ 공급처 추가` and its `새 공급처 추가` card open v29's `supplierCredentialModal` (titled
`새 공급처 추가`: 공급처명, 도메인, ID, Password, 연결 테스트, 저장) with the add flow's steps:

- **플랫폼 확인.** `POST /api/v1/onboarding/detect` with the domain. The server guards the origin
  (an exact public `https` host, never a repository supplier's or an onboarded one's), reads the
  home page and each template's login page once, and reads robots.txt and the detected template's
  terms page. The modal shows the detected platform (`Cafe24` / `고도몰` / `지원하지 않는 플랫폼`),
  the marker names that decided it, the robots.txt lines and both documents' status with a link
  to open them, and every warning (`주의`). A warning never blocks: the seller reads it and decides.
  An unsupported site stops with `템플릿이 필요합니다` and 저장 stays disabled: no template is ever
  forced onto a site that does not carry its markers. Only when both templates' markers matched
  (`AMBIGUOUS`) does a `플랫폼` select offer exactly those templates; a choice reads only that
  template's policy documents. A home page that redirects offers `…로 다시 확인`.
- **저장.** Enabled only after the acknowledgement checkbox. It creates the draft
  (`POST /api/v1/onboarding/suppliers`: name, origin, the confirmed template, the detection id and
  the acknowledgement) and then saves the login through CONNECT's existing
  `PUT /api/v1/connect/suppliers/{key}/credentials`. The password is read from its field once,
  sent once to that route only and cleared; it is never shown, kept or logged.
- **연결 테스트.** CONNECT's existing test; when it passes, `POST …/onboarding/suppliers/{key}/recon`
  moves the supplier to `RECON` and the modal closes. A card's own 연결 테스트 does the same for a
  draft supplier whose modal was closed early.
- **The card.** An onboarded supplier's card carries its onboarding chip beside the connection chip:
  `초안` (DRAFT), `분석 중` (RECON), `사용 중` (ACTIVE), `비활성` (DISABLED). A disabled supplier,
  which CONNECT no longer lists, keeps a card with its chip and no action. `사이트 분석` turns live
  on a `분석 중` card with O3, and `수집 규칙` on a `분석 중` or `사용 중` card with O4 (below).
  Every onboarded card but a `비활성` one has `비활성화` (O5): it asks once, then `POST
  …/disable` appends DISABLED — no collection, no registration preparation, and the supplier's
  egress grant is withdrawn — and the card reads `비활성`.
- **Server-owned.** Detection, the warnings, the conditions for `RECON` and every state belong to the
  onboarding owner. The page decides none of them and keeps nothing in browser storage.

## Collection Management — 공급처 관리 › 「사이트 분석」 (ADR-0036 O3)

v29's `사이트 분석` button on a supplier card is live only on an onboarded supplier in `분석 중`
(RECON); on every other card it stays inert. It opens a `사이트 분석 · <공급처명>` panel in the
existing modal language:

- **Samples.** A URL box and `샘플 읽기` (`POST /api/v1/onboarding/suppliers/{key}/trials`, 202 with a
  job id the page follows like a connection test). Up to 5 samples; a sample is a product page of the
  supplier's own storefront in its template's path form, and anything else is refused before any
  read. Each sample is read once, ever, and a read that failed still uses its sample and counts
  toward the 5; reads are paced on the template's defaults (10 s between reads, and COLLECT's own
  last read of the product). A refusal says why (`이미 읽은 샘플`, `너무 이른 읽기`, `샘플은 최대
  5개`, …). A words change is re-analysed from the stored captures (`다시 분석`), never by reading a
  sample again. Images are never downloaded.
- **Each sample's report** (`GET …/trials`, the newest trial of each sample under the current
  words): its URL, the product number, `확인 가능` / `확인 불가`, and a table `필드 | 상태 | 값 요약`
  with `읽음` (CONFIRMED), `없음` (ABSENT) and `못 읽음` (REVIEW_REQUIRED) per field (the five core
  fields marked `핵심`), the values the operator will verify (name, prices, options, stock, image
  count and addresses) and, for a field not read, the template's own quoted evidence. Below it, the
  unread rows and phrases with the slot a rule proposes (`제안 슬롯`, or `제안 없음`). A structural
  problem shows as `템플릿 수정 필요` with its reason; such a sample is never `확인 가능`.
- **Progress.** `샘플 n/3 확인 가능`, `값 확인 n/3`, the current words revision (`w<seq>`), the
  template revision, the sample count and `사용 시작` (O5, below).
- **값 확인 (O5).** Under each verifiable sample's values: `위 값이 실제 상품 페이지와 같습니까?` with
  `값이 맞음` and `값이 틀림` (`POST …/trials/{trial_id}/verification` with `CORRECT` or `WRONG` and
  the fingerprint of exactly the facts shown). A confirmed sample shows `값 확인함`, a refuted one
  `값 틀림` (it never proves the site, and a `틀림` stands over any `맞음`). The server refuses a
  `맞음` on a sample that is not verifiable, a verdict on facts other than those shown, and any
  verdict on a trial under older words.
- **분석에서 제외 (O5b, amendment `6160586049`).** A sample that is not confirmed offers
  `분석에서 제외`: it asks for the operator's reason (`window.prompt`, 1–200 characters), then
  `POST …/samples/retire` retires it. The sample stays shown, muted, as `제외됨` with the reason, its
  structural flags and its values; it neither proves nor blocks `사용 시작`, takes no verdict, is not
  read again and still counts toward the 5. A retirement is never undone.
- **사용 시작 (O5).** Enabled only when the server says the site can start (3 distinct samples
  confirmed under the current words and template revision, every core field read, no structural
  problem): it asks once (`window.confirm`), then `POST …/activate` moves the supplier to `사용 중`
  (ACTIVE) through the store's gate, which the database enforces as well, and the screen is drawn
  again from the server. The ACTIVE row names the verifications as its evidence.
- **다시 분석.** Enabled when a sample's newest trial ran under older words: `POST …/reanalyse`
  re-reads the stored local captures under the current words and reads nothing from the supplier.
- **관찰된 호스트.** One table `호스트 | 역할 | 횟수` over the samples: an image or login host the
  guard accepts has `확인` (`POST …/hosts` with the observing trial), a confirmed one shows `확인됨`
  and `해제` (`POST …/hosts/disable`), and an asset host is listed for information only. A confirmed
  image host joins the supplier's collection egress at once.
- **Server-owned.** Every status, value, candidate slot, structural problem, count and refusal is the
  server's. The page keeps nothing in browser storage and never shows a credential, a session or
  page text beyond the bounded values above. The words are edited in `수집 규칙` (O4, below).

## Collection Management — 공급처 관리 › 「수집 규칙」 (ADR-0036 O4)

v29's `수집 규칙` button on a supplier card is live only on an onboarded supplier in `분석 중`
(RECON) or `사용 중` (ACTIVE); on every other card it stays inert. It opens a
`수집 규칙 · <공급처명>` editor in the existing modal language:

- **Slots.** One table `슬롯 | 템플릿 기본 단어 | 이 공급처 단어` (`GET
  /api/v1/onboarding/suppliers/{key}/words`): every slot of the supplier's template, named in
  Korean with its kind (`행 이름`, `문구`, `표시`), the template's own words as read-only chips, and
  one box for the supplier's words, one per line (a comma belongs to the word: ADR-0035's phrases
  hold commas). Only words pass: the server refuses
  pattern characters (`< > { } [ ] \ | * + ? ^ $`), more than 12 words in a slot, a word longer
  than 40 characters and a slot the template does not have, and says why.
- **미리보기.** `POST …/words/preview` re-analyses each sample's stored capture under the current and
  the typed words, and shows per sample the unread rows and phrases the new words read
  (`새로 읽음`), those they no longer read, the fields whose status they change (`가격: 못 읽음 → 읽음`)
  and `읽지 못한 행/문구 n개 → m개`; a sample whose capture reads differently from its page is
  marked as never usable for approval. A preview reads nothing from the supplier and saves nothing.
- **저장.** `PUT …/words` with the words and the revision they were edited from (`expected_seq`)
  appends the next words revision; words that moved since are refused (reload and edit again), and
  identical words are refused. On a `사용 중` supplier the editor warns first: a save returns it to
  `분석 중`, to be approved again. After a save, `다시 분석` (`POST …/reanalyse`) re-reads the stored
  captures under the saved words, again without a supplier read.
- **Server-owned.** Every word rule, preview result, revision and refusal is the server's. The page
  keeps nothing in browser storage.

## Extension — 셀프등록 공급처 권한 (ADR-0036 O5c, owner decision D3)

A small addition below the approved Collector boards, in the list rows' own language. While the
extension is paired, a `셀프등록 공급처 권한 · n곳` section lists each `사용 중` (ACTIVE) onboarded
supplier ICBM names (`GET /api/v1/collect/extension/onboarded-origins`): its name, its exact
storefront origin and key, and a chip `허용됨` or `권한 없음`.

- `권한 요청` asks Chrome for that one origin on the operator's click, and Chrome asks again in its
  own prompt. Nothing is requested in bulk or without the click. `권한 제거` gives a held origin back.
- Until its origin is granted, a supplier's page is neither recognised nor captured.
- When the panel opens, after pairing and on `목록 새로고침`, the extension removes every optional
  origin it holds that ICBM no longer lists, such as a disabled supplier's.

## Collection intake (A-UX2, owner decisions 2026-10-04 D1 and D2)

- Collection Management's URL box follows v29's `상품 URL을 입력하세요. 여러 개는 줄바꿈`: one URL per
  line, at most 50; the box is v29's one-line field and grows with the lines pasted into it. Under
  the box the screen counts `입력 n줄 · 중복 n · 잘못된 URL n · 수집 예정 n` and
  lists the lines it cannot read as an http(s) URL; those are never sent, and a repeated URL is sent
  once. Each planned URL is sent on its own through the one submit call, one after another, and its
  answer — a run or the server's refusal — is shown beside it; refused lines stay in the box. One
  URL behaves exactly as before and opens its run.
- The extension's list mode shows a checkbox on each found product, `선택 n` beside the found
  count, and `전체 선택 / 전체 해제`. Only the chosen links are declared. The queue bound is never
  filled from the selection (the board's no-pre-filled-bound rule above still holds).

## Rules

- This HTML prototype is the visual/product UI reference for ICBM-NEW.
- Legacy repository UI implementations, including old `ICBM-PROJECT` / #86 functional implementation, are **not** implementation sources for ICBM-NEW.
- Do not copy legacy functional owners, API bindings, DB assumptions, handlers, or runtime state from old UI code.
- Functional behaviour must be connected fresh to ICBM-NEW application contracts.
- If prototype JavaScript conflicts with `documents/roadmap/ROADMAP.md`, accepted ADRs, or `documents/architecture/ARCHITECTURE.md`, the canonical architecture wins. Prototype JavaScript is demo interaction only.
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
