# ADR-0041 — Image Studio, first capability: translating the text inside product images into Korean

Status: **ACCEPTED** 2026-10-14. This is the contract for translating the Chinese or Japanese text inside a supplier's product images into Korean. It writes a new derived image the operator reviews, approves and selects; the source image is never edited. It is the first capability of Image Studio (Issue #56). It lands before its code. Each slice of §10 cites it as canon.

Decision owners:
- **Product direction:** the owner, in Issue #219.
  - `6109102924`: image translation (Image Studio, #56) is Track A's, after supplier onboarding.
  - `6054956408` (PR #264): the 「이미지 자동번역」 buttons exist as inert 준비 중 placeholders.
  - `6057252039`: the AI foundation is provider-zero. A real AI call, its model, key and cost are the owner's decisions.
  - `6165660910` (2026-10-14): the design choices D1–D8 of §2.
  - `6109102895`: the seller-risk principle.
- **Already decided by canon:**
  - ADR-0010 and ADR-0013 §9: `SourceAsset` and `ProductFactsImageRef` are the source truth. Derived images, their QA and selection belong to the M4 image owner.
  - ADR-0012, ADR-0026 and ADR-0027: the AI provider port, the task registry, provenance, the CLIProxyAPI profile, and keys in the OS secret store only (AIS-01..09).
  - ADR-0033 §3: the deterministic Pillow renderer and the bundled Noto Sans KR font.
  - ADR-0037: image caps by role and the SmartStore upload-fit derivation, which already accepts a derived parent.
  - ADR-0038 and ADR-0040 (Track B): the overseas 구매대행 suppliers and their capture collectors, the main users of this capability.
- **Implementation choices:** the implementation agent, under ADR-0022 and rule 14.

What it authorizes:
- this contract;
- the slices of §10;
- the amendment of §9, effective when the slice it names lands.

What it does not authorize:
- **No change to a source.** A `SourceAsset`, `ProductFactsImageRef` or `ProductFactsRevision` is never written, re-encoded or deleted. OCR text is an observation, never a fact.
- **No generated pixels for text.** Characters are drawn only by the deterministic renderer (§5). No model paints text into an image.
- **No image transfer.** Only recognised text leaves the machine, for translation (D1, D8). No image bytes are sent to any provider.
- **No automatic run.** Translation runs only when the operator asks, product by product (D4).
- **No LIVE.** Upload of a translated image uses only the existing exact-sha grant path, under the execution mode in force (DRY_RUN).
- **No new marketplace and no HTML text editing.**

Recorded by: Claude Code (Track A). The number was agreed with Track B, which owns ADR-0040 and uses its own numbers.

Date: 2026-10-14

---

## 1. What translation means here

For one product the operator chooses, and for the images they choose:

```text
SourceAsset (sha) ─ OCR (local) → regions (text, box, script, confidence, background)
                  ─ translate the recognised text only (AI task, text) → Korean suggestions
                  ─ operator reviews every region (edit / keep original / confirm flagged ones)
                  ─ erase only where clean (§4) → render Korean in the matched style (§5)
                  ─ completeness and leftover-text QA (§6)
                  → derived artifact `image-translate/v1` → operator QA verdict → selection
```

The derived artifact joins the M4 derived-image chain. When a translated image is larger than SmartStore allows, ADR-0037's upload fit derives from it as from any derived parent:

```text
SourceAsset(sha) → image-translate/v1 → [smartstore-upload-fit/v1 when needed] → upload grant
```

## 2. The owner's choices (`6165660910`)

- **D1 Provider approach.** OCR runs locally. Only the recognised text is translated, through the existing AI provider port and the CLIProxyAPI profile (ADR-0027), as a new registered task (§9).
- **D2 OCR engine.** RapidOCR: the PP-OCR models on onnxruntime, Apache-2.0, with Chinese and Japanese models pinned by SHA-256. Installing it is a download confirmed with the owner (file, source, size) when slice I7 lands. Until then a `NoOcrProvider` is bound, and the capability reads `NOT_CONFIGURED`.
- **D3 Which images.** All roles: representative, additional and detail. The text-removal rule of §4 keeps a product photo intact whatever its role.
- **D4 When it runs.** Only on the operator's request, for one product: 「이미지 자동번역」 in the registration editor. There is no automatic run at collection.
- **D5 Cost guard.** The AI profile's existing daily call cap applies, and one run translates at most 30 images.
- **D6 Text removal.** Only as far as it does not harm the image (§4).
- **D7 Styling.** The Korean text follows the style of the font actually used in the source text (§5).
- **D8 Data transfer.** Text-only transfer of the recognised text, for the new task, on the existing `gpt-5.6-sol` profile. No image transfer.

## 3. Records (one migration, the next free number when I1 lands)

All writes go through one owner store, `app/capabilities/image_studio/`, with an injected clock and an audit event per write. Every table refuses UPDATE and DELETE except where a pointer column is stated.

| table | holds |
| --- | --- |
| `image_ocr_observations` | One OCR reading of one source image, keyed by the source SHA-256, the OCR engine id and version, and the preprocessing version. It holds the regions: the box or polygon, the source text, the detected script (`zh` / `ja`), the confidence and the background-uniformity score. Content-addressed, so the same image is read once however many products use it. |
| `image_translation_candidates` | The AI's Korean suggestion per region for one observation, with the provenance copied from the outcome (ADR-0012 §6), the composed `prompt_version`, `policy_version` and output schema version, and the input fingerprint below. |
| `image_edit_revisions` | The operator's saved edit for one image of one product, numbered per lineage (product group and source SHA-256). Per region, it holds the final Korean text, its origin (`AI_SUGGESTION` or `OPERATOR_CONFIRMED`), include or keep-original, the erase decision and the matched style. It also holds the renderer version, the font file hashes and a content fingerprint. |
| `image_edit_current` | The forward-only current pointer per lineage. A parent id never owns the current state. |

**The translation fingerprint is ADR-0012 §6 / ADR-0026's, unchanged.** It is computed before the call, over:

```text
hash(relevant_input + policy_version + prompt_version + requested_provider + requested_model
     + routing_config_version + proxy_version + output_schema_version)
```

- `relevant_input` is the digest, by value, of exactly what the task reads: the observation's fingerprint, the source language and the ordered region texts that are sent (a region the operator excludes, or a contact detail kept by default, is not part of it).
- Any change to the prompt, the policy, the requested provider or model, the routing or proxy revision (ADR-0027 §2) or the output schema therefore changes the fingerprint.
- **Reuse** happens only on an equal fingerprint, and the response says `REUSED`. A candidate whose recomputed fingerprint differs is `STALE`, derived on read and never stored, with its reason (input, prompt, policy, provider or schema). A stale candidate is never shown as a fresh suggestion.
- No secret is ever part of a fingerprint or provenance (AIF-07).

The derived artifact itself is recorded by the M4 image owner (`ProductImageService.record_completed_derivation`). Its transformation spec names only the edit revision id and fingerprint: the spec stays small (`MAX_SPEC_BYTES`) and the revision holds the detail. Facts staleness uses the existing `validated_source_revision_id`.

**Jobs.**
- The job type `image.translate` is not idempotent (`idempotent=False`), since a provider call may cost money.
- A batch only coordinates: each image is its own item, and only failed items are retried.
- A re-run reuses an OCR observation with the same key, and a translation candidate only with an equal translation fingerprint (above), so no provider is called twice for the same inputs and a changed prompt, policy, model, routing or schema is never served from an old candidate.
- A failed item leaves no durable artifact, and the source stays selected.
- The editing session is not durable. A stateless preview renders without storing anything, as ADR-0033's preview does.

## 4. Text removal: only as far as it does not harm the image (D6)

- **Where it erases.** Source text is erased only where the erase is clean: the region's background is uniform enough (the background-uniformity score passes), and the fill is the median colour of a ring around the box.
- **Where it does not.** On any other background — a product photo, a gradient, a pattern — the text is **not erased**. The region is flagged `COMPLEX_BACKGROUND` and defaults to keep-original. The operator may still choose to place the Korean text as a labelled box beside or over it, which is visibly an overlay and never a repaint of the photo.
- **What it never does.** No pixel outside a region's box is changed. A region the operator keeps is not touched at all.
- **Later.** Better inpainting (classical or model-based) may widen where text can be erased, only behind the same no-damage QA and its own recorded decision.

## 5. Rendering in the style of the source font (D7)

- **The renderer.** `image-translate-renderer/v1` is deterministic: the same revision, font files and source give the same bytes. It is built on Pillow (ADR-0033 §3).
- **The matched style.** Per region, the renderer follows the style measured from the source text:
  - weight (stroke width relative to the glyph height): regular, bold or black;
  - sans or serif;
  - size: the source line height, shrunk to fit the box down to a floor;
  - colour: sampled from the source glyph pixels.
- **The fonts.** The style is matched to the nearest face of a **bundled, licensed Korean font set**. Noto Sans KR (variable weight) is already bundled; a serif face (for example Noto Serif KR, SIL OFL) is added by the slice that needs it, as a download confirmed with the owner. Exact identification of the source font is not attempted.
- **Overflow.** Text that does not fit at the floor size is flagged `LAYOUT_OVERFLOW`, and it is never cut.
- **Editable style.** The operator may override the matched weight, face, size and colour per region. The override is part of the revision.

## 6. Review and QA

- **The Studio panel.** It shows the original and a live preview. There is one row per region: the source text, the Korean text (editable), include or keep-original, and the confidence and finding badges.
- **Regions confirmed one by one.** A region is flagged, and needs its own confirmation, when any of these holds:
  - low OCR confidence;
  - a digit, unit, model number or specification in the source that the Korean text does not carry (a deterministic comparison);
  - `LAYOUT_OVERFLOW`;
  - `COMPLEX_BACKGROUND`;
  - leftover source script found by reading the rendered image again.

  「모두 적용」 never passes a flagged region.
- **Contact details.** A region holding a supplier's contact details (a messenger id, a phone number) defaults to keep-original and is flagged, and is not sent for translation unless the operator includes it.
- **「적용」.** It saves the revision, renders, runs the completeness check (every confirmed region rendered, in bounds, with every glyph resolved; anything else fails) and the leftover-text QA, and records the derived artifact. The operator's QA verdict on that exact SHA-256 then makes it selectable (`USE_DERIVED`, operator origin).
- **「되돌리기」.** It moves the selection back to the source (`USE_SOURCE`). Nothing is deleted.

## 7. The buttons

The registration editor's inert 「이미지 자동번역」 placeholders (`6054956408`) become live in slice I5:
- ⑤ 상세페이지: translates the product's detail images;
- ② the selected-image bulk action: translates the selected images.

Both stay inert, with 준비 중, while the capability reads `NOT_CONFIGURED`: no OCR engine installed, or no translation profile configured.

## 8. Safety

- **Sources.** No source is ever written (IT-01).
- **Failure.** Only a fully successful recipe leaves a durable artifact; a failure leaves the source selected and untouched (IT-02).
- **Secrets and data.**
  - Keys stay only in the OS secret store, and the provider endpoint stays loopback (AIS-01). Any loosening is a BLOCKER.
  - Audit rows and logs never carry image bytes or OCR text.
  - Provenance is copied, never invented, and an unknown cost is null.
- **Cost.** Calls stop at the daily cap or at 30 images per run. Without the D8 approval in force, no call is made. Tests use a fake OCR engine and a fake AI provider.
- **QA.** QA belongs to the exact artifact SHA-256. A translated artifact never inherits the source's QA, and readiness needs the operator's verdict (ADR-0013 §9).
- **Execution.** It stays DRY_RUN, and upload uses only the existing exact-sha grant (ADR-0018, ADR-0037).

## 9. Amendment, effective when its slice lands

- **ADR-0026 §3.1 (the AI task registry)** gains `TASK_IMAGE_TEXT_TRANSLATE_V1` (slice I4). It is a text task: its input is the recognised text of one image's regions and the source language; its output is one Korean string per region. It is used on the existing CLIProxyAPI profile under D8, and it stays a suggestion (AIS-08).

## 10. Slices

| slice | scope |
| --- | --- |
| I1 | The migration and the store (the four tables of §3), and the job type over a fake OCR engine and a fake AI provider. Provider-zero. |
| I2 | The `OcrProvider` port, with `NoOcrProvider` bound by default (capability `image_ocr` `NOT_CONFIGURED`, never a core failure); the region model; confidence and background-uniformity scoring. |
| I3 | The renderer: clean-only erase (§4), style matching to the bundled font set (§5), shrink-to-fit, completeness, pinned-hash tests. |
| I4 | The translation task in the registry (§9), candidate recording, the deterministic digit/unit/spec check and the contact-detail default. |
| I5 | The Studio panel; the ⑤ and ② buttons go live behind the capability; preview, save, apply, QA verdict, select and revert. |
| I6 | QA: reading the rendered image again for leftover text; a large detail image through the upload-fit chain to the fake SmartStore. |
| I7 | The real OCR engine (D2, its confirmed download) and any added font; the first live translation, under the owner's go-ahead and an acceptance record on images from both overseas suppliers. |

## 11. Risks and spikes

- **Images to translate.** Super Delivery captures bring detail images. 1688's description images arrive with ADR-0040 slice C2 (Track B). Until then, 1688 offers the main gallery only.
- **OCR quality.** RapidOCR on about 20 real images: recall, confidence calibration, vertical Japanese text, and tiling tall images.
- **Style matching.** How well weight and face are estimated from short source runs; the floor size on dense specification tables.
- **Measure.** Time per image on the operator's machine, the model download size, and onnxruntime on the project's Python and Windows.

## Invariants

- **IT-01** The image translation owner never writes, re-encodes or deletes a `SourceAsset`, `ProductFactsImageRef` or `ProductFactsRevision`.
- **IT-02** Only a fully successful recipe leaves a durable artifact. A failure leaves the source selected and untouched.
- **IT-03** Final characters are drawn only by the deterministic renderer, with its version and the font hashes pinned. The same input gives the same bytes.
- **IT-04** A pixel is changed only inside a confirmed region's box, and source text is erased only where the erase is clean. A region on a complex background is never repainted.
- **IT-05** A flagged region (§6) is never applied without its own confirmation.
- **IT-06** An AI translation is a suggestion. Only the operator's save makes a revision, and a processing success is never an approval.
- **IT-07** No image bytes leave the machine. Only recognised text is sent, under D8, through the existing provider port, with keys in the OS secret store only.
- **IT-08** Calls stop at the daily cap and at 30 images per run. No call is made without the D8 approval.
- **IT-09** QA belongs to the exact artifact SHA-256, and readiness needs the operator's verdict.
- **IT-10** Translation runs only on the operator's request (D4).
