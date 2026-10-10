# B-DETAIL — Canonical detail content composition

Status: **design accepted** (PR #223, DUAL_PASS, merged `83a3ff3`); the HIGH_RISK implementation
slice is recorded in §8. Owner decisions of 2026-10-04 (D1–D4, G-1, invariants) are recorded below.

Base: main `ec0d305`. Every current fact below cites the code or document it comes from.

## 1. Why

A selected detail-body image (`OutputRole.DETAIL`, Issue #219) has no place in the listing:
REGISTER refuses the unit with `PUBLICATION_DETAIL_IMAGES_UNPLACED` (`BLOCKED`,
`app/stages/register/preparation.py:1115-1128`) and the CREATE projection refuses it with
`WIRE_DETAIL_IMAGE_NOT_PLACEABLE` (`integrations/marketplaces/smartstore/product.py:752-758`). All
27 operational KM products carry detail images, so none can register. This is a first-vertical
functional prerequisite, not a UI improvement.

## 2. Owner decisions (2026-10-04)

- **D1** The first image-placing section order is `DETAIL_IMAGES` → `BODY` (profile content v2;
  content v1 stays `BODY`-only). Top/bottom guidance (Issue #61), video and
  an option table are not implemented; only the section vocabulary stays extensible.

  > **Amendment note (ADR-0033 G5; owner decision 2026-10-10, Issue #219 `6085853541`).** The
  > reserved `TOP_GUIDANCE` and `BOTTOM_GUIDANCE` become image sections of profile content v3
  > (`TOP_GUIDANCE` → `DETAIL_IMAGES` → `BODY` → `BOTTOM_GUIDANCE`, `detail-renderer/v2`), stamped
  > for SmartStore only. `VIDEO` and `OPTION_TABLE` stay reserved and unrepresentable. It takes
  > effect once ADR-0033 slice G5 is accepted and landed; until then D1 holds unchanged.
- **D2** `BODY` (operator text) is optional when `DETAIL_IMAGES` holds at least one image. COLLECT's
  `detail_description` (a 200-character truncated text) is never used to fill `BODY`.
- **D3** No operator raw HTML. Authoring accepts plain text only; the wire renderer HTML-escapes it
  deterministically. No operator input can inject HTML, an image tag, a URL, a script or any markup.
- **D4** No numeric limit on detail images without adopted official evidence. A NAVER limit, once
  proven, is applied in the REGISTER projection/preflight only; the canonical composition is never
  truncated for a provider limit.
- **G-1** NAVER `detailContent` evidence: **INSUFFICIENT** (§3).
- **No new owner.** The existing `DETAIL_COMPOSITION` authoring-revision owner is extended.

## 3. G-1 — provider evidence (verdict INSUFFICIENT)

Officially confirmed: `originProduct.detailContent` is a required string; a group product requires
its detail through `commonDetailContent` or `detailContentTempId`; the gallery images
(`images.representativeImage`, `images.optionalImages`) take URLs returned by the product-image
upload API (`documents/evidence/marketplace-apis/PRODUCT_CREATE.md`, `IMAGE_UPLOAD.md`).

Not established by official evidence: the HTML allowed in `detailContent`, its sanitization, its
maximum length, the maximum number of body images, any host or domain restriction on body images,
and whether a body image must be an upload-API URL. **None of these is adopted as a production
contract.** No provider-specific limit or HTML claim is made until official evidence is captured.

## 4. Inventory of the current owner (exact)

| Concern | Today | Source |
| --- | --- | --- |
| Schema | `registration_authoring_revisions` (migration `0032`): `kind IN (CATEGORY_MAPPING, DETAIL_COMPOSITION)`, `content_json` is any JSON object naming its own kind and scope; one unique index per kind (`marketplace_key, seq` for DETAIL_COMPOSITION) | `app/stages/register/authoring_revisions.py:164-215` |
| Revision model | Append-only, no pointer, current = highest `seq` per `marketplace_key`; server is the only author; content strictly typed per version (`DetailCompositionContentV1`: `sections: ("BODY",)`, `guidance: False`) and sanitized | `authoring_revisions.py:1-27`, `:83-118`, `:125-157` |
| Scope | **One profile per marketplace** — not per product. It holds no product content | ADR-0014 §27.1 |
| Initialization / stamp | A target-policy save stamps the current revision; `ensure` reuses a current revision with equal content and appends one otherwise | `authoring_revisions.py:314-402` |
| Per-unit inputs | The preparation revision (append-only) holds `detail_composition_revision`, `detail_body`, `detail_sections` (client-sent, not checked against the profile) | `app/stages/register/contracts.py:256-258`, `authoring.py:113-175` |
| Preflight | `DETAIL_COMPOSITION_MISSING`, `DETAIL_BODY_EMPTY`, `AUTHORING_REVISIONS_UNOWNED` (exact equality with the target policy's stamped revision), sanitation over the whole detail | `preparation.py:1083-1108`, `:1202-1224` |
| Stale dependency | The candidate fingerprint names `detail` (composition revision, sections, `body_digest`) and every selected image (role, position, asset kind, SHA-256, derivation, QA) | `preparation.py:1302-1378`, `:620-635` |
| Snapshot pin | `registration_snapshots.detail_composition_revision` (NOT NULL); `payload.detail = {composition_revision, sections, body}`; `publication_assets` already carry every selected image with `role` and `position` | `app/stages/register/models.py:226-254`, `payload.py:75-84`, `:125-156` |
| Wire | `detailContent` = `payload.detail.body` verbatim; `sections` ignored; no length check | `product.py:692-697`, `:931`, `:485` |
| Upload | ASSET grants and the final preflight already require **every** selected image, DETAIL included | `app/capabilities/live_safety/authority.py:115-127`, `preparation.py:1244-1249` |
| Read-back | Neither retains nor compares `detailContent` | `integrations/marketplaces/smartstore/registry.py:278-285`, `readback.py:305-385` |
| Sanitizer | Refuses `scheme://`, `="//host` and `www.` anywhere in a business value | `app/stages/register/sanitize.py:38`, `:58-70` |

**Migration: none required.** The profile content, the preparation `detail_json` and the Snapshot
`payload_json` are JSON documents whose CHECKs constrain shape and scope, not the content version.

## 5. Design

### 5.1 Two layers, both existing owners

The profile is marketplace-scoped (§4), so it cannot hold one product's images. The composition is
therefore split across the two owners that already exist, and the two layers are never mixed in one
profile row: the profile holds the section vocabulary and order, the renderer/template version and
the rendering policy; the product-specific immutable plan holds the selected `DETAIL` asset
identities, the optional `BODY` and the exact authoring/preparation revision inputs.

- **Profile (DETAIL_COMPOSITION, content v2, per marketplace):** which sections exist and in what
  order, the BODY format and the pinned renderer version:
  `{"content_version": "registration-detail-composition/v2", "kind": "DETAIL_COMPOSITION",
  "marketplace_key": …, "sections": ["DETAIL_IMAGES", "BODY"], "body_format": "PLAIN_TEXT",
  "renderer": "detail-renderer/v1", "guidance": false}`. Section values are a closed enumeration;
  the reserved `TOP_GUIDANCE`, `BOTTOM_GUIDANCE`, `VIDEO`, `OPTION_TABLE` are refused in v2.
- **Per unit:** `BODY` text stays in the preparation revision (plain text, optional under v2 when
  detail images exist). The ordered `DETAIL_IMAGES` identities are the M4 current selection's
  `DETAIL` outputs in position order — already resolved into each Item's `PublicationImage`s and
  already in the candidate fingerprint. REGISTER never chooses or reorders images.
- The preparation's `detail_sections` become **server-derived from the profile** (a client value
  other than the profile's is refused).

> **Amendment note (ADR-0033 G5; owner decision 2026-10-10, Issue #219 `6085853541`).** The
> SmartStore profile moves to content v3:
> `{"content_version": "registration-detail-composition/v3", "kind": "DETAIL_COMPOSITION",
> "marketplace_key": …, "sections": ["TOP_GUIDANCE", "DETAIL_IMAGES", "BODY", "BOTTOM_GUIDANCE"],
> "body_format": "PLAIN_TEXT", "renderer": "detail-renderer/v2", "guidance": true}`. The reserved
> `TOP_GUIDANCE`/`BOTTOM_GUIDANCE` are sections of v3; `VIDEO` and `OPTION_TABLE` stay reserved and
> are refused in every version. The profile still holds no product content (two layers): a unit's
> resolved Detail Guidance notices are the product-specific plan's (`payload.detail.guidance`,
> `registration-payload/v3`), and their uploaded images are the Snapshot's `guidance_assets`. v1
> and v2 keep reading as before, and every other marketplace keeps v2. It takes effect once
> ADR-0033 slice G5 is accepted and landed; until then §5.1 holds unchanged.

### 5.2 What the Snapshot pins (URL-free)

`payload.detail` (builder `registration-payload/v2`):
`{composition_revision, sections, body_format, renderer, body, images: [{item, position,
asset_kind, sha256, derivation_id}]}` — the image entries reference the same Item publication
assets the Snapshot already freezes. No provider URL is in the plan. A v1 payload keeps reading as
BODY-only.

### 5.3 Rendering — the trusted REGISTER renderer

Only the REGISTER wire projection renders `detailContent`, from the Snapshot's plan plus the same
Snapshot's `publication_assets[*].provider_asset_ref` (each already proven by
`safe_provider_reference`) under the pinned renderer version:

- the sections in profile order; `DETAIL_IMAGES` as one image element per detail image in position
  order, each pointing at that image's uploaded provider reference; `BODY` as HTML-escaped text
  (paragraphs on blank lines, line breaks preserved), never parsed as markup;
- a detail image without an uploaded provider reference refuses the projection
  (`WIRE_IMAGE_NOT_PREPARED`); a source or supplier URL can never reach it because none exists in
  the plan;
- the rendering is deterministic: the same Snapshot and renderer version give the same bytes.

The sanitizer is not relaxed: business values still hold no URL. The renderer is a separate, typed
boundary that emits only provider references the upload path produced — never an operator string.

### 5.4 Preflight

- `PUBLICATION_DETAIL_IMAGES_UNPLACED` stays `BLOCKED` while the unit's profile does not place
  `DETAIL_IMAGES` (v1, or no profile). It is released when the owned profile is v2 with
  `DETAIL_IMAGES`; the final preflight's existing `PROVIDER_ASSET_IDENTITY_MISSING` keeps every
  detail image's upload mandatory.
- `DETAIL_BODY_EMPTY` only when the unit has neither detail images nor body text.
- No count or length limit (D4, G-1). A provider limit, once proven, is a REGISTER projection
  reason; it never edits the composition.
- The rendered representation's digest and the renderer version join the candidate fingerprint, so a
  renderer change or a new image order is `STALE` through the existing propagation.

### 5.5 Rollout

v2 is appended by the server like v1 (`ensure`) once the slice ships; a target-policy save stamps
it. Preparations authored against v1 then read `AUTHORING_REVISIONS_UNOWNED` and are re-authored —
the existing no-backfill rule. A unit without detail images under v2 behaves as under v1 (BODY
required, nothing else rendered), so the no-image first-vertical contract is preserved.

### 5.6 Temporal rule

Every amendment recorded with this design (ADR-0014 §6, §19, §27.1; ADR-0013 §9; ARCHITECTURE;
GLOSSARY; M5 acceptance) takes effect only once the B-DETAIL implementation slice is accepted and
landed. Until then the current rule holds unchanged: the composition is content v1 `BODY`-only, a
unit with detail images stays `BLOCKED` by `PUBLICATION_DETAIL_IMAGES_UNPLACED`, and the CREATE
projection refuses it. The PRODUCT_CREATE G-1 row is evidence, not a rule change.

## 6. Invariants and their tests

| # | Invariant | Test (implementation slice) |
| --- | --- | --- |
| 1 | Source/supplier hotlink 0 | rendered `detailContent` holds only provider references from `publication_assets`; a fixture with a supplier URL anywhere cannot render |
| 2 | Operator raw URL 0 | a BODY with `https://…`, `//host`, `www.` is refused by the sanitizer at save, preflight and payload build |
| 3 | Operator raw HTML 0 | a BODY with `<img>`, `<script>`, `<a>` renders as escaped text, never as markup |
| 4 | BODY escaping | `&`, `<`, `>`, `"`, `'` are escaped; golden rendering |
| 5 | Deterministic image order | rendering twice and across processes is byte-identical; order = selection position |
| 6 | Renderer version pinned | the Snapshot names the renderer; a different renderer is a new candidate fingerprint |
| 7 | Snapshot immutability | an authoring change after freezing leaves the Snapshot and its rendering unchanged |
| 8 | Stale on selection change | a new image selection revision makes prepared assets and the candidate `STALE` |
| 9 | No SEND without uploads | a detail image without a provider reference refuses the projection |
| 10 | UNPLACED without composition | v1 profile + detail images → `PUBLICATION_DETAIL_IMAGES_UNPLACED` `BLOCKED` |
| 11 | Released with composition | v2 profile + uploaded detail images → the reason is gone, the unit can be `READY` |
| 12 | BODY optional | v2 + detail images + empty body → no `DETAIL_BODY_EMPTY` |
| 13 | No truncated description | nothing reads `detail_description` into BODY (contract scan) |
| 14 | No truncation on unknown limits | 50 detail images render 50 images; no code path drops any |
| 15 | Provider incompatibility never edits the canonical composition | a projection refusal leaves the preparation and Snapshot unchanged |

## 7. Exact implementation scope (HIGH_RISK)

- `authoring_revisions.py`: `DetailCompositionContentV2`, `detail_composition_content` → v2, v1
  still parsed.
- `preparation.py` / `authoring.py` / `contracts.py`: server-derived sections, BODY optional rule,
  release condition, renderer and rendering digest in the candidate fingerprint.
- `payload.py`: builder v2 with the URL-free plan.
- `integrations/marketplaces/smartstore/product.py`: the renderer (`detail-renderer/v1`), wire
  `smartstore-register-wire/v6`, removal of `WIRE_DETAIL_IMAGE_NOT_PLACEABLE` for v2 plans.
- `ui/web/js/pages/register.js`: BODY label shows it is optional with detail images; no new editor.
- Tests for §6; ADR/ARCHITECTURE/GLOSSARY/M5 amendments take effect.
- Out of scope: Issue #61 guidance, video, option table, per-product section toggles, read-back
  comparison of `detailContent`, Coupang.

## 8. Implementation (the HIGH_RISK slice)

Base: main `83a3ff3`; the inventory of §4 is unchanged on it (the moves since `ec0d305` touch no
REGISTER, SmartStore or live-safety code).

- `app/stages/register/detail.py` — the pure plan and the trusted renderer: `DetailProfile`,
  `PlannedImage`, `DetailPlan` (`plan_of` reads a frozen plan strictly: an unknown member, a URL
  included, refuses it), `UploadedProviderAsset` (a type of its own, built only from a safe provider
  reference) and `render` (`detail-renderer/v1`).
- `authoring_revisions.py` — content v2 beside v1 (both readable); `ensure`/`stamp` now append v2;
  `AuthoringRevisionStore.detail_profile` reads one revision's profile.
- `preflight.py` — the service resolves the target's profile through `DetailProfileSource` into
  `ResolvedUnit.detail_profile` (the container wires the authoring-revision store).
- `preparation.py` — `detail_plan` (a plan exists only for an owner-held composition under a v2
  profile: exactly the target's revision and exactly its sections); `DETAIL_BODY_EMPTY` only without
  planned images; `PUBLICATION_DETAIL_IMAGES_UNPLACED` released only by a plan that places images;
  `AUTHORING_REVISIONS_UNOWNED` also when the sections are not the profile's; the plan's renderer,
  body format and ordered image identities join the candidate fingerprint, named only when a plan
  exists.
- `authoring.py` — the stored sections are the owned profile's; a client section the profile does
  not hold is refused (`REGISTER_DETAIL_SECTIONS_NOT_PROFILE`). A client still sending `["BODY"]`
  is not refused: `BODY` is in the profile, and the profile's own sections are what is stored.
- `payload.py` — `registration-payload/v2` freezes the plan as `payload.detail`; a BODY-only
  composition keeps the v1 document exactly.
- `product.py` — `smartstore-register-wire/v6`: a plan Snapshot's `detailContent` is rendered from
  the plan and the Snapshot's own `DETAIL` publication assets (their set must equal the plan's,
  `WIRE_DETAIL_PLAN_MISMATCH`); its detail images are never gallery images. A BODY-only Snapshot
  projects its frozen body exactly as before, and a detail image without a plan is still
  `WIRE_DETAIL_IMAGE_NOT_PLACEABLE`.
- `register.js` — the body label says it follows the detail images and is optional with them; the
  form sends the server's sections back and an empty body under an image-placing profile.

Recorded choices within the design:

- A frozen v1 (BODY-only) Snapshot keeps its v1 projection: rendering it under a renderer it never
  pinned would change what was frozen. New compositions are v2 as soon as a target-policy save
  stamps the v2 profile (the existing no-backfill rule).
- An asset two Items of one listing share is placed once, at its first Item and position.
- Tests: `tests/unit/register/test_b_detail_composition.py` (the owner's 20 required cases, the
  profile owner, no truncation of 50 images), provider-zero (every socket refused).

