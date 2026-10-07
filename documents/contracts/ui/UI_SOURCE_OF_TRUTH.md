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
- the recent-runs list offers `전체 / 원천 확인 필요 / 실패 / 기록할 식별자 없음`. The server applies the
  filter before it orders and bounds the list, reports how many runs the filter selects in all, and
  pages on with `더 보기`; a page is never shown as the whole list.

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

## Collection intake (A-UX2, owner decisions 2026-10-04 D1 and D2)

- Collection Management's URL box follows v29's `상품 URL을 입력하세요. 여러 개는 줄바꿈`: one URL per
  line, at most 50. Under the box the screen counts `입력 n줄 · 중복 n · 잘못된 URL n · 수집 예정 n` and
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
