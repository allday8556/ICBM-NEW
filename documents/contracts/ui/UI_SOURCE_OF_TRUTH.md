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
reproduces that structure in the focused run of 수집관리, where the run's revision is read back:

- per field: its status (`확정` / `없음` / `확인 필요`), its stored value only when `CONFIRMED`, and its
  stored evidence on demand (kind, locator, observed, normalized, status, digest), plus the
  extractor revision, the transport and the image count included / total, all as the revision holds
  them;
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
