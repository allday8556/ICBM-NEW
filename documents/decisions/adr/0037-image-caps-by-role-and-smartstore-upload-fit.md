# ADR-0037 — Image caps by role, and a SmartStore upload-fit variant for oversize images

Status: **ACCEPTED** 2026-10-11. The owner decided both rules:
- collection caps images by their role: a representative or additional image at 5 MB, a description image at 30 MB;
- an image larger than SmartStore's upload limit is fitted under it automatically, by a recorded, deterministic re-encoding, before it is uploaded.

This ADR lands before its code.

Decision owners:
- **Product direction:** the owner, in Issue #219:
  - `6103784916`: "이미지 메인 5mb 상세 30mb각각제한";
  - `6104346033`: "자동 압축 (추천)", asked what to do with a description image over SmartStore's upload limit.
- **Already decided by canon:**
  - ADR-0010 §4, §9, §12: collection limits are profile semantics, and a source asset is immutable and content-addressed;
  - ADR-0013 §9: a derived image is its own immutable artifact, with inputs, transformation spec and version, provenance and its own QA. Chains such as source → derived → marketplace variant are allowed;
  - ADR-0014 and ADR-0018: an upload sends exactly the frozen artifact a grant authorizes;
  - ADR-0030: template default limits.
- **Implementation choices:** the implementation agent, under ADR-0022 and rule 14.

What it authorizes:
- the per-role caps of §1 in the collection core, in every template, and in KM통상's profile;
- the upload-fit variant of §2, produced during registration preparation as a derived artifact;
- the slices of §4.

What it does not authorize:
- **no change to a source asset.** It is never re-encoded or replaced (ADR-0013 §9);
- **no other image editing.** Cropping, text removal and translation belong to Image Studio, Issue #56;
- **no change to a recorded revision.** A collection under the new caps writes a new revision, as usual.

Sources:
- Issue #219 `6103784916`, `6104346033`;
- the SmartStore Commerce API image upload (`POST /v1/product-images/upload`), and the maintainer's statement in commerce-api discussion #117 (last edited 2026-03-18) that the image files of one call must total under 10 MB (10^7 bytes), with no resolution limit.

Recorded by: Claude Code (Track B). The number was confirmed free on main and on every open branch; ADR-0036 is Track A's.

Date: 2026-10-11

---

## Context

Two facts meet here.

- **Collection.** Collection caps every image at 5 MiB today (Issue #219 `6086299406`), with 24 MiB per product run. Some suppliers' description images are larger. 건강산's acceptance held one such image as `OVERSIZE` (`documents/acceptance/suppliers/ggsan.md`). The owner wants description images of up to 30 MB collected.
- **SmartStore upload.** SmartStore accepts less than 10 MB per upload call, measured on the whole payload. ICBM re-hosts every image through that call, one image per call. A description image of 10–30 MB therefore cannot be sent as collected.

## Decision

### 1. Collection caps by role

| role | cap per image |
| --- | --- |
| `REPRESENTATIVE` (the representative image and the additional images) | 5 MB (5,000,000 bytes) |
| `DETAIL` (description) | 30 MB (30,000,000 bytes) |

- **Run total.** The per-product run total rises from 24 MiB to **120 MB**. Thirty description images at the cap would still exceed it; an image over what the run has left is `BUDGET_EXHAUSTED`, as today.
- **Fail closed.** An image over its role's cap is `OVERSIZE` and `REVIEW_REQUIRED`. It is refused as it arrives and never stored (ADR-0010 §4).
- **Revisions.** The caps are profile semantics (ADR-0010 §12). They advance:
  - the `cafe24` and `godomall` templates;
  - KM통상's package;
  - every site's extraction identity.

### 2. A SmartStore upload-fit variant

- **When.** During registration preparation, an image chosen for upload whose bytes are **9,500,000 or more** is not uploaded as it is. That threshold leaves room for the multipart envelope under the 10^7-byte limit.
- **What it becomes.** A derived image artifact (ADR-0013 §9), made by the recipe **`smartstore-upload-fit/v1`**:
  1. decode the image;
  2. if it is wider than 1,000 px, scale it down to 1,000 px wide, keeping its aspect ratio;
  3. encode it as baseline JPEG, trying quality 90, 85, 80, 75, 70, 65, 60 in that order, and keep the first result under 9,500,000 bytes.
- **Deterministic.** The same input bytes and the same recipe version give the same output bytes.
  - The decoder and encoder are pinned (Pillow, `pyproject.toml`).
  - Metadata is dropped.
  - The colour profile is converted to sRGB.
- **Lineage.** The artifact records its derivation: input source asset SHA-256, recipe id and version, and output SHA-256, dimensions and bytes. It is content-addressed, so the same input reuses the existing artifact.
- **What is frozen and uploaded.** The snapshot freezes the variant's SHA-256, and the grant and upload authorise exactly that artifact (`DERIVED_ARTIFACT`, already an allowed upload kind). The source asset is kept, unchanged.
- **The owner's standing decision.**
  - ADR-0013 §9 makes using an edited image an operator decision. For this one recipe, the owner has decided it in advance, for every image over the limit.
  - The operator still sees that a variant was used, and which one.
  - Any other derivation remains an operator decision.
- **When nothing fits.** If no quality step brings an image under the threshold, or the image cannot be decoded, the image is `REVIEW_REQUIRED`, with the reason `IMAGE_UPLOAD_FIT_FAILED`. Nothing smaller is guessed.
- **Smaller images are untouched.** An image under the threshold is uploaded byte for byte, as today.

### 3. QA and display

- The variant's QA is its own (ADR-0013 §9).
  - The automatic image QA (`image-qa-auto/v1`) runs on the variant as it runs on any artifact.
  - A variant never inherits the source's QA.
- The registration screens show, beside each image that uses one, that an upload-fit variant was used, with its size and dimensions.

### 4. Slices

| # | slice |
| --- | --- |
| C0 | this ADR |
| C1 | the caps by role (§1) in the collection core and the profiles: `cafe24-4`, `godomall-3` and `kmretail-6`, with the site revisions following |
| C2 | the upload-fit variant (§2, §3) in registration preparation, freeze, grant and upload, with a drill test that uploads a 25 MB description image's variant through the fake SmartStore |

Each slice finalizes in its turn of the global merge lane.

## Invariants

- **IC-01** A representative or additional image over 5 MB, or a description image over 30 MB, is never stored; it is `REVIEW_REQUIRED` as `OVERSIZE`.
- **IC-02** Nothing at or over 9,500,000 bytes is ever sent to SmartStore's image upload.
- **IC-03** The variant is a derived artifact with recorded lineage. The source asset is never altered.
- **IC-04** The same input and the same recipe version give the same variant bytes.
- **IC-05** The grant and upload name the variant's exact SHA-256.
- **IC-06** An image the recipe cannot fit is `REVIEW_REQUIRED`, and nothing is uploaded for it.

## Consequences

- **More images are usable.** Description images up to 30 MB are collected and registered, at a resolution SmartStore displays at 860–1,000 px anyway.
- **Bigger stored assets.** The asset store grows. Variants are content-addressed and reused.

## References

- ADR-0010 §4, §9, §12
- ADR-0013 §9
- ADR-0014
- ADR-0018
- ADR-0030
- Issue #219 `6086299406`, `6103784916`, `6104346033`
- commerce-api discussion #117
