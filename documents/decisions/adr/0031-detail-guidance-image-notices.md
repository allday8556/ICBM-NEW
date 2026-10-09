# ADR-0031 — Detail Guidance: the top and bottom notices of the detail page, as images rendered from templates

Status: **ACCEPTED** 2026-10-10. This activates the Detail Guidance module planned in Issue #61, after the first vertical (M7 accepted). It is the contract of every slice of §9. It lands before its code.

Decision owners:
- **Product direction:** the owner, in Issue #219 comment `6085853541` (2026-10-10):
  - **Templates.** About three official templates.
  - **Generation.** The operator types the notice text. ICBM reflects it automatically into each template as an **image**. The operator picks one of them, and it becomes the **top** notice; the same applies to the **bottom** notice, for delivery (배송) and C/S.
  - **Settings once, per-product override.** The notices are made once in settings and go into every product by default. A product may pick another template or text, or turn a notice off.
  - **Period notices are included.** A holiday, vacation or courier-cut-off notice applies only between its start and its end.
  - The owner declined an AI detail text in `6085786591`. Nothing here is AI.
- **Already decided by canon:**
  - **Issue #61:**
    - the `detail_guidance` owner is apart from `notice.*`, the product-information disclosure owner (§1);
    - append-only revisions (§5);
    - placement `TOP`/`BOTTOM` (§7);
    - a half-open period held as UTC instants with an injected clock (§8);
    - one renderer for the preview and the registration (§10);
    - the Snapshot evidence (§11);
    - no unrestricted operator HTML (§12).
  - **B-DETAIL** (`documents/reviews/B-DETAIL-detail-composition.md`, owner decisions 2026-10-04):
    - **D1.** The section vocabulary is closed and versioned; `TOP_GUIDANCE`/`BOTTOM_GUIDANCE` are reserved.
    - **D3.** Operator text is plain text, never HTML, a URL or markup.
    - **D4.** No provider limit is assumed without official evidence.
    - **Two layers.** The profile and the product plan are kept apart.
    - **URL-free.** The Snapshot holds no URL, and only the trusted REGISTER renderer produces `detailContent`.
  - ADR-0014 §19 (the composition seam), ADR-0018 (the ASSET grant and upload owner), ADR-0013 (image assets).
- **Implementation choices:** the implementation agent, under ADR-0022 and rule 14. These include the image library, the font, the template designs, the schema and the endpoint shapes.

What it authorizes:
- this contract;
- the slices of §9;
- the amendments of §10.

What it does not authorize:
- **No HTML notice.** Operator text never reaches `detailContent` as text; it reaches it only as the pixels of an ICBM-rendered image.
- **No AI and no ICBM-authored wording.**
  - ICBM writes no legal or policy wording (#61 §12).
  - ICBM inserts no policy value (shipping fee, return address and the like) into a notice.
  - A template's built-in headings are editable defaults only.
- **No bulk update of registered listings** (#61 §13). A period that starts or ends changes only what a later registration freezes.
- **No new marketplace endpoint.** Guidance images use the adopted SmartStore image upload exactly as detail images do.
- **No LIVE.** Execution stays `DRY_RUN`. Every real upload or CREATE still needs its own exact grant.
- **SmartStore only.** Other marketplaces keep their current composition until their own decision.

Recorded by: Claude Code (Track A). The number was confirmed free on main and on every open branch.

Date: 2026-10-10

---

## 1. Vocabulary

**Detail Guidance.** These are store-wide notices drawn as images into the detail page. They are never `notice.*`, the 상품정보제공고시.

**Placement.**
- `TOP` is above the detail images.
- `BOTTOM` is below the body.

**Kind.**
- **`STANDING`.** At most one is current per placement. It is the default notice, for example "12시 이전 주문시 당일발송" on top, or the 배송/C/S notice at the bottom.
- **`PERIOD`.** Any number may exist. Each carries `starts_at` and `ends_at`, and applies only inside `[starts_at, ends_at)`.

**Content.** It is plain text only:
- `blocks`: 1 to 3 blocks.
- Each block is `{heading, lines}`.
  - `heading`: optional, at most 20 characters.
  - `lines`: 1 to 6 lines, each at most 40 characters.

**Character rules.** Each line is trimmed. Control characters, and every character the bundled font cannot draw, are refused with `GUIDANCE_TEXT_INVALID`; nothing is silently dropped. These limits are ICBM's own layout limits, not provider limits (B-DETAIL D4).

**Template.**
- **`CLEAN`, `MODERN`, `WARM`.** These are the three official templates (#61 §9 named the candidates). They are distributed with the code.
- **Design.** A template fixes the canvas, colours, margins, font sizes, block layout and decoration. It adds no text of its own except the default headings, which the operator may change or clear.

## 2. The owner and its records

The new owner `app/capabilities/detail_guidance/` is marketplace-neutral. Following #61 §1, it never reads or writes `notice.*`.

**`detail_guidance_revisions`** (append-only; one new migration, numbered when G2 lands):
- **Identity.** `guidance_id` and `seq` (unique together), plus `placement`, `kind`, `template` and `content_json`.
- **Status.** `enabled`. For `PERIOD`, `starts_at` and `ends_at` (UTC); a CHECK enforces `starts_at < ends_at`.
- **Record.** `created_at` (the injected clock), `actor` and `image_sha256`.
- **Change.** A change appends `seq + 1`. Turning a notice off appends a revision with `enabled = false`; nothing is updated or deleted (#61 §5, §8).
- **One standing notice.** Making a `STANDING` notice current for a placement disables the previous one in the same write unit, so at most one enabled `STANDING` revision is current per placement.
- **Audit.** Every write records an audit event.

**`guidance_image_artifacts`:**
- **Columns.** `sha256` (primary key), `width`, `height`, `renderer_version`, `template`, `font_sha256` and `created_at`.
- **Bytes.** They live in a content-addressed file store, `<data>/guidance/sha256/xx/<sha>`. Writes are atomic and fail closed on a conflict, as in `DerivedImageStore`.
- **Not product-scoped.** It is store-level. One image serves every product that uses that revision, and a product's own custom notice (§5) stores its image here too.
- **One constructor.** It is the only constructor of its model (`PREFLIGHT_TRUTH_WRITERS` pattern).

**Saving.** A revision is saved only together with its rendered image: the owner renders, stores the bytes, then appends the revision that names `image_sha256`, all in one write unit.

## 3. The renderer

`render(content, template) -> PNG bytes` is the **only** renderer. It is used for the settings preview, the editor preview and the registration alike (#61 §10).

- **Library and font.**
  - The image library is Pillow, pinned to one exact version in `pyproject.toml` and `constraints.txt`.
  - The font is Noto Sans KR (SIL Open Font License 1.1), Regular and Bold. It is bundled in the owner's `fonts/` with its licence text, and its SHA-256 is pinned in code.
- **Canvas.** It is 860 px wide. The height follows the laid-out text. Line wrapping is deterministic: no line wraps, because §1 caps each line's length, and the template's text box fits 40 characters at its font size. A line that does not fit the box is refused as `GUIDANCE_TEXT_TOO_WIDE`; it is never shrunk or cut.
- **Determinism.** The same `(content, template, renderer_version)` gives the same PNG bytes: fixed encoder parameters and no metadata, time or randomness.
  - The version is `guidance-renderer/v1`. A change of the font, the Pillow version or any template drawing bumps it.
  - A test pins the SHA-256 of one rendering of each template.
- **Preview.** It renders the three templates for the typed content and stores nothing. It returns the PNGs to the page.

## 4. Settings: 설정 › 공통 › 상세페이지 공지

**Where.** This is a new settings card. For each of `상단 공지` and `하단 공지` it offers the following.

**The standing notice:**
- **Inputs.** Headings and lines. The bottom notice opens with two blocks headed `배송 안내` and `C/S 안내`.
- **Preview.** The live preview shows the three templates side by side as the operator types.
- **Saving.** The operator picks one template, then saves. Saving renders and appends the revision (§2).
- **Off.** It can be turned off.

**Period notices.** These have the same inputs plus a start and an end.
- **Time.** They are entered in the operator's local time and stored as UTC instants.
- **Listing.** They are listed as `예정`, `진행 중` or `종료`, judged by the server's clock.
- **Ending early.** A period notice can be ended early, which appends a disabled revision.

**API** (`/api/v1/settings/detail-guidance`, behind the existing CSRF client header):
- list the current revisions;
- append a revision;
- preview;
- `GET` an image by `sha256`.

## 5. Per product: the editor's 상세페이지 step

The preparation revision gains `guidance`. It is server-validated plain JSON, with one entry per placement:
- **`DEFAULT`** (the default). The store-wide notices apply.
- **`OFF`.** No notice at this placement for this product.
- **`CUSTOM`.** `{template, blocks}` of this product's own standing notice. It replaces the store-wide standing notice, and period notices still apply. Saving the preparation renders it into `guidance_image_artifacts`.

**The editor.** Its `상단 공지` and `하단 공지` groups show the resolved images in order, and offer `기본값 / 끄기 / 직접 작성`.
- `직접 작성` shows the same three-template live preview as settings.
- The current inert placeholder `＋ 공지 추가 · 상·하단 공지는 준비 중` is replaced.

## 6. Resolution, staleness and the Snapshot

**Resolution.** For each placement, at the instant `t` of the injected clock, the resolved list is built in this order:
1. **`OFF`.** If the product's entry is `OFF`, the list is empty.
2. **Period notices.** Next come the enabled `PERIOD` revisions with `starts_at ≤ t < ends_at`, ordered by `(starts_at, guidance_id)`.
3. **The standing notice.** Last comes the product's `CUSTOM` notice, or else the current enabled `STANDING` revision, when there is one.

**Each entry.** An entry is `{source: STORE|PRODUCT, guidance_revision_id | preparation_revision_id, template, sha256}`.

**Staleness.**
- The resolved lists of both placements are part of the candidate fingerprint.
- A new revision, a period that starts or ends, or a changed product choice therefore makes a frozen Snapshot stale under the existing rules. It must be frozen again before any send.
- A frozen Snapshot is never edited.

**The Snapshot pin** (#61 §11). It stays URL-free (B-DETAIL §5.2).
- `payload.detail` (builder `registration-payload/v3`) gains `guidance: {top: [...], bottom: [...]}`, the resolved entries.
- The Snapshot gains `guidance_assets`: one entry per distinct `sha256`, with `asset_kind = GUIDANCE_ARTIFACT` and, after the upload, its `provider_asset_ref`, exactly as the Item `publication_assets` carry theirs.

## 7. Composition v3 and the trusted renderer

**The profile.** The SmartStore DETAIL_COMPOSITION profile moves to content v3:
`{"content_version": "registration-detail-composition/v3", "sections": ["TOP_GUIDANCE", "DETAIL_IMAGES", "BODY", "BOTTOM_GUIDANCE"], "body_format": "PLAIN_TEXT", "renderer": "detail-renderer/v2", "guidance": true}`.
- It still holds no product content (two layers, B-DETAIL §5.1).
- v1 and v2 keep reading as before.
- An empty guidance section renders nothing.
- `BODY` and its `DETAIL_BODY_EMPTY` rule are unchanged; a guidance image is not a body.

**The renderer.** `detail-renderer/v2` renders each guidance entry as an `<img>`, exactly as it renders a detail image. It uses only that same Snapshot's `guidance_assets[*].provider_asset_ref`, each proven by `safe_provider_reference`.

**The projection.** The SmartStore projection refuses `WIRE_GUIDANCE_PLAN_MISMATCH` unless the plan's guidance entries equal the Snapshot's `guidance_assets` exactly, as `WIRE_DETAIL_PLAN_MISMATCH` does for detail images. The wire encoding version is bumped.

## 8. Upload and the ASSET grant

**The grant's artifact set.** It becomes the candidate's selected Item images **plus** its distinct guidance images. `LIVE_GRANT_ARTIFACT_SET_MISMATCH` keeps its meaning: an ASSET grant names exactly the current set.

**The upload run.** It reads guidance bytes from the guidance image store, the third byte source beside the source-asset store and the derived store.
- The upload owner, the per-call attempt record and the replay key (marketplace, account, endpoint, content SHA-256) are unchanged.
- An image already `APPLIED_PROVEN` for the account is reused, so one store-wide notice is uploaded once per account.

**Preflight** keeps `PUBLICATION_DETAIL_IMAGES_UNPLACED` semantics for guidance: a resolved guidance image with no place in the profile blocks the unit.

## 9. Implementation order

Each slice is its own PR, with the full suite, Gate 3, M0, `mypy`, ruff, the GPT+Claude exact-head audit and CI.

| Slice | Scope | Risk |
| --- | --- | --- |
| **G1** | The renderer: the Pillow pin, the bundled font and its licence, the three templates, `render`, determinism tests. No DB, no API. | normal |
| **G2** | The owner: its migration, the revision store with clock and audit, the guidance image store, the settings API (list, append, preview, image). | normal |
| **G3** | The settings card of §4, with the live three-template preview. | normal |
| **G4** | REGISTER: the preparation `guidance` choice, the resolution, the fingerprint and preflight, the Snapshot pin, composition v3 stamping, and the editor's 상세페이지 step. | HIGH_RISK |
| **G5** | The wire and the upload: `detail-renderer/v2`, the SmartStore projection, the grant's artifact set, the upload byte source, and the DRY_RUN acceptance of a top and bottom notice end to end. | HIGH_RISK |

**Track B's files.** G4 and G5 touch the following:
- the B-DETAIL owner (`authoring_revisions.py`, `detail.py`, `preparation.py`, `payload.py`);
- the SmartStore projection;
- the live-safety grant and upload run.

These are Track B's files. Track A changes them only as this ADR states, announces each slice in Issue #219 before it starts, and rebases on any Track B change to them.

## 10. Amendments (effective when G5 lands)

The amendments below take effect only once G5 is accepted and landed. Until then the current rule holds unchanged.

- **ADR-0014 §19.** Detail Guidance is implemented by this ADR: composition v3 adds `TOP_GUIDANCE` and `BOTTOM_GUIDANCE` as image sections.
- **B-DETAIL §2 D1 and §5.1.** The reserved `TOP_GUIDANCE`/`BOTTOM_GUIDANCE` become sections of content v3. `VIDEO` and `OPTION_TABLE` stay reserved.

## Invariants

- **DG-01.** A notice reaches `detailContent` only as an ICBM-rendered image, uploaded through the adopted image upload. No operator text, HTML, URL or markup reaches it.
- **DG-02.** One renderer, `render`, serves the preview and the registration. A rendering's bytes depend only on `(content, template, renderer_version)`.
- **DG-03.** Revisions are append-only. Turning a notice off or ending it early appends a revision, and at most one `STANDING` revision is current per placement.
- **DG-04.** Periods are half-open `[starts_at, ends_at)` in UTC, judged only by the injected clock.
- **DG-05.** The resolution is deterministic and in the candidate fingerprint. Any change makes a frozen Snapshot stale, and a frozen Snapshot is never edited.
- **DG-06.** The Snapshot pins the resolved guidance and its asset identities, and holds no URL.
- **DG-07.** An ASSET grant names exactly the Item images plus the guidance images, and an upload reads only ICBM's own stores.
- **DG-08.** ICBM authors no wording and inserts no policy value. The `notice.*` owner is never read or written.
- **DG-09.** Text that cannot be drawn exactly is refused, never shrunk, cut or dropped.
- **DG-10.** No LIVE, no bulk listing update, and no marketplace other than SmartStore.
