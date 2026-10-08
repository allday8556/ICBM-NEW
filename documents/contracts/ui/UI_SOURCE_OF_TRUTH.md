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
  - v29's `카테고리 / 플랫폼 / 공급처 / 가격기준` filters and `✨ AI 추천` are inert.
- **List columns.**
  - The columns are v29's: `상품명 / 공급처 / 원가 / 배송비 / 최저판매가 / 가격기준 / 네이버 / 쿠팡 / 11번가 / 상태 / 등록유형`.
  - The name cell keeps the Product's id, member and Item counts, membership revision and creation time on its second line.
  - The supplier and the status come from the row. The price columns and `등록유형` read `데이터 없음`.
- **Detail panel.**
  - The title shows the representative image through the bound Item's own image read, the member's confirmed name, the id and the status.
  - v29's groups follow: `브랜드` from the member's facts; `카테고리 / 카테고리 추천 신뢰도 / 가격 정보 / 채널별 판매가 / 마진율 / 태그 / AI 상태` read `데이터 없음`.
  - `✨ AI 추천` and `상세 편집` are inert.
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
  1. `기본정보`: the main image, the platform tabs (the target marketplace only; the others are inert), the category and the name.
  2. `대표이미지`: the Item image editor. The bulk `배경 이미지 제거 / 리사이즈 / 이미지 편집 / 이미지 자동번역` are inert. `자동 선택 다시 실행` is the image owner's own.
  3. `옵션·가격`: the repin and the Items table. The price facts no read states read `데이터 없음`, and the option tools are inert.
  4. `상품정보고시`: the category requirements and their fields.
  5. `상세페이지`: the image-editor mode. `HTML 방식` is inert. The top and bottom notices read `데이터 없음`. The body is the authoring form's. `미리보기` opens the snapshot preview.
  6. `기타 등록정보`: the preflight, the attempts, the scope brake and the unit's own actions.
- **One save.** The authoring form stays one form, and its parts are bound back to it. The editor's `준비 내용 저장` is the register page's one `/preparations` write.
- **`등록 준비` strip.** A one-line strip sits at the bottom. Each step is coloured from the server's preflight reasons only, by the first area each reason names:
  - green: no reason;
  - orange: a reason that is neither BLOCKED nor DUPLICATE;
  - red: a BLOCKED or DUPLICATE reason;
  - grey: no preflight yet.
  - Hovering a step lists its reasons; clicking it moves to that step.
- **AI controls are inert placeholders.** This covers `배경 이미지 제거` and `이미지 자동번역` in 대표이미지 and `이미지 자동번역` in 상세페이지, under owner decision 2026-10-08 (Issue #219 comment `6054956408`; ROADMAP, Issue #127). Each keeps its approved position, disabled and marked 준비 중, and sends nothing. It becomes active only with its server owner.
- **Edit mode is read-only.** No contract yet edits a registered listing on the marketplace. `상품수정` shows the registered unit's contents and says so. A unit with a Snapshot or an Intent is never authored in the editor.
- **The register page is unchanged.** Every section the Gate 3 surface reads stays on `#/register`.

### 설정 › AI / Prompt (ADR-0026 AIF-1)

- **AI Prompt Registry.** The card keeps v29's four sections: 공통 규칙, 역할, 플랫폼 정책 and 작업.
  - Each entry reads its current revision from the server's PromptTemplate or PlatformPolicy store and shows it as `v{n} · 기본값` or `v{n} · 사용자 수정본`.
  - Clicking an entry opens the v29 layered prompt editor on that entry's role × policy × task.
- **Editor.** The editor has v29's seven tabs: 공통 규칙, 역할 Role, 플랫폼 Policy, 작업 Task, 출력 형식, 입력 변수 and 조립 미리보기.
  - The state chip reads 기본값, 사용자 수정본 or 저장되지 않은 변경.
  - `현재 계층 저장` and `↺ 현재 계층 초기화` write one revision of the current tab only.
  - The preview is the server's composition of the saved layers.
- **Other entry points.** Each marketplace tab's `Policy 편집` button opens its platform policy. AI 쇼핑 인사이트's `✨ Shopping Insight Agent 설정` opens its role and task. Both only edit prompts.
- **Still inert.** The "AI 기본 설정" toggles stay inert placeholders until their stage. No control runs an AI call, because no provider exists.

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
