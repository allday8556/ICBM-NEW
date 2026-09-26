# ADR-0017 — Adaptive Collector: profile-interpreted extraction, semantic comparability and one-fetch shadow validation

Status: **ACCEPTED** — decided by the architect PASS `5302952567` on the Adaptive Collector
Proposal (PR #111, exact head `1dbb13353ace0a7fd6f3ba56591edee69a80a4e3`), the Claude AI
cross-audit PASS the architect agreed with, and the drafting authorization on Issue #110
(`5812422770`), on exact main `c031f5adf03d100e931effe460dffa8406a420b8`.
- It records that accepted design as contract, and it closes the six cross-audit items recorded on
  Issue #110 (`5812200650`) (§14).
- Where the Proposal left a value for "the ADR" to fix — the shadow retention bounds and the Phase C
  evidence window — this ADR fixes it, and that value is open to the exact-head audit.
- Amended before merge by the architect audit `5307128101` on `43cbb0fd`:
  - the ADR-0010 §7 level name is aligned with `COVERAGE` (§4);
  - a `ValidationSample` keeps admissible embedded data and excludes non-authoritative regions
    (§7.3);
  - the shadow decision is frozen per run at the product-read reservation (§10.1, §11.1);
  - raw shadow retention has no hold exception, and windows keep their own evidence ledger (§10.5);
  - a `NO_REVISION` shadow record has no `revision_id` (§10.5).
- Amended before merge by the architect re-audit `5307485431` on `fe335cd7`:
  - a truncated embedded block makes its sample unable to support a `PASS` (§7.2, §7.3);
  - Phase C has an explicit no-cherry-pick rule: only `SHADOW_MISSING_AFTER_RECOVERY` lets a window
    be superseded for the same bundle (§11.2, §11.3).
- Amended before merge by the architect re-audit `5307562621` on `2592e71e`:
  - the window ledger is an append-only event stream per run with one derived effective state;
  - a window final-closes only when every effective state is terminal, so its closeout is never
    revised (§10.5, §11.1).
- **Its authority becomes effective only after this exact contract PR is audited, independently
  cross-audited and merged**, and even then only phase by phase (§2).

**It authorizes no runtime code, model, schema, migration, route, job, prototype, Phase B, Phase C,
canonical COLLECT change, Adaptive `ProductFactsRevision` write, provider call or second-supplier
read.** Each phase it names needs its own authorization in GitHub.

Decision owner: Architect (ChatGPT). Sources:
- Issue #110 (the Adaptive Collector design track) and the kickoff `5811580104` (DESIGN only, the
  nine required decisions);
- the merged Proposal `docs/review/ADAPTIVE-COLLECTOR-PROPOSAL-BY-CLAUDE.md` (revision 4) and its
  audit trail on PR #111: review `5302725919`, re-audits `5302852218` and `5302910552`, PASS
  `5302952567`;
- the cross-audit items recorded in Issue #110 `5812200650` and the ADR authorization `5812422770`;
- the contracts this ADR builds on and does not replace: ADR-0007 (CONNECT), ADR-0010 (COLLECT and
  `ProductFactsRevision`), ADR-0012 (AI runtime), ADR-0013 §3 (the current source revision
  pointer), ADR-0016 (ReviewItem).

Recorded by: Claude Code. The number was confirmed free in `docs/adr/`, on `main` and in every open
PR immediately before writing.
Date: 2026-09-24
Related:
- **Amends** three ADR sections:
  - ADR-0010 §6 and §12: provenance and extraction identity for profile-interpreted extraction
    (§5);
  - ADR-0010 §7: its historical level label "Source coverage" / "source-coverage" is read as the
    enum level `COVERAGE` (§4);
  - ADR-0013 §3: drift comparability keys on `comparability_key` (§5.4).

  These amendments give **identical answers for every revision that exists today**. Both ADRs carry
  a pointer to this one at each amended section. No other sentence, invariant or ruling of any ADR
  changes.
- ADR-0016: unchanged. `ReviewKind` stays closed, and a shadow mismatch is never a `ReviewItem`
  (§10.4).

---

## Context

A new supplier today needs a supplier-specific Python parser: ADR-0010 §3 makes the supplier's
collection definition "pure, deterministic parser functions", pinned by a repository-file extraction
identity (§12). That is safe, but it grows linearly: every supplier is new code, a new
reconnaissance and a new review of that code.

Issue #110 asks for the opposite growth curve. A new supplier should usually be onboarded by
validating a **profile** against 1–2 representative products, not by writing a new collector. The
constraints are fixed:
- `ProductFactsRevision` remains the append-only source truth.
- `FieldStatus`, `EvidenceKind` and `ReviewKind` are unchanged.
- Production collection makes zero AI/OCR calls.
- The request-budget, pacing, session and gateway owners stay canonical.
- `CLAUDE.md` §12 still forbids horizontal supplier expansion before the first vertical closes.

The seam this design uses already exists. `SupplierCollection` (`integrations/suppliers/collection.py`)
asks a supplier exactly three interpretive questions over an immutable `DocumentView`:
- which product this is;
- what its fields say;
- which of its images are product evidence.

Everything that acts is generic COLLECT core (`app/collect/collection.py`).

## Decision

### 1. The Adaptive Collector is a second implementation of the parser seam

The Adaptive Collector is **not a second COLLECT**. One generic engine interprets an immutable,
digested profile bundle and answers the same three questions `SupplierCollection` asks. Everything
else stays where ADR-0010 put it, unchanged:
- the gateway, session, pacing and request budget;
- the image fetch and asset store;
- the revision store, the job and the audit.

| layer | holds | lives in | changes at run time |
| --- | --- | --- | --- |
| **Access envelope** | the CONNECT `SupplierDefinition` (ADR-0007) and the `CollectionProfile`: product path form, policy paths, explicit hosts, safe query keys, limits, `RequestPolicy` | repository, reviewed | **never** (ADR-0010 §9) |
| **Generic engine** | locator interpreter, generic extractors, normalizers, template matcher | repository code with its own extraction-identity manifest | no |
| **`ExtractionProfileRevision` (EPR)** | one supplier's interpretation (§3) | canonical DB, immutable rows | a new revision only |
| **`PageTemplateRevision` (PTR)** | one page shape of that supplier (§3) | canonical DB, immutable rows | a new revision only |
| **Site adapter hooks** | allowlisted pure functions (§6) | the supplier package, under its own hook manifest | no |

- **Profile-only onboarding** means a new supplier needs only these three things:
  - its access envelope;
  - its CONNECT definition, the reviewed declarations ADR-0007 and ADR-0010 already require;
  - profile data.

  It needs no supplier-specific parser or extractor Python and **no hook**. A supplier with any
  hook binding is reported as *profile + hooks*, never as profile-only.
- **The v1 limit.** `CollectionProfile` is HTTP-only, and `EvidenceKind` has no network or XHR kind.
  A supplier whose product facts arrive only through a script-loaded API response, or only after
  browser rendering, cannot be profile-only in v1. Widening that is a gateway and `EvidenceKind`
  decision for a later ADR. This ADR adds no `EvidenceKind`.

### 2. What is authorized, phase by phase

> **Amendment note (ADR-0019).** Extending the gateway or the acquisition transport is decided by
> ADR-0019: `EXTENSION` primary and `DIRECT_URL` fallback. No phase here authorizes a transport.

| phase | scope | contract / schema | real reads |
| --- | --- | --- | --- |
| **A** | the Proposal | none | 0 — done (PR #111) |
| **ADR** (this) | this contract and the canonical-document alignment it requires | contract only | 0 |
| **B** prototype | an isolated, disposable, fixture-only prototype (Q5, §13) | none; nothing wired | 0 |
| **Production implementation** | engine, profile owner, validation owner, shadow owner, their schema | only after this ADR is merged, each slice separately authorized | 0 |
| **C** KM통상 shadow | the shadow switch and the evidence windows of §11, on ordinary operator collections | per its authorization; **§11.3 gates it** | only ordinary operator collections, with the user's go-ahead; none exists for the shadow's sake |
| **Cutover** | `ACTIVE` bundles writing canonical revisions | a separate cutover ADR | per that ADR |
| **D** second supplier | §12 | deferred (Q6) | none until `CLAUDE.md` §12 permits it |

Until a phase is authorized, these are forbidden: a change to the canonical COLLECT runtime, a
shadow or Adaptive write to any `product_facts_*` table, a schema or migration, a new `EvidenceKind`,
`FieldStatus` or `ReviewKind` value, and any second-supplier read.

### 3. `ExtractionProfileRevision` and `PageTemplateRevision`

**Ownership.**

| owns | EPR | PTR |
| --- | --- | --- |
| scope | one supplier, all its product pages | one page shape of that supplier |
| identity rule | **yes**: which source statements declare the product identity and how they must agree; never a name or content hash (ADR-0010 §5) | no |
| vocabularies | label sets per field, money and number formats, sold-out vocabulary, purchase-control anchors | may narrow them for its own shape; never adds a field |
| image-role rules | supplier-wide | the image region locators the rules apply to |
| template set | a **closed set of PTR digests it pins**; no precedence (matching is exclusive, §8) | no |
| signature | no | required anchors, forbidden anchors |
| field rules | no | per field: a primary locator, optional declared alternatives, an expected cardinality |
| hook bindings | `(hook_point, target, format_class)` → hook name + the supplier's **semantic** `HOOK_REVISION` (§6); never an implementation fingerprint | no |

- **One bundle.** An EPR pins its PTRs by digest, so its digest covers the whole bundle.
  Validation and activation happen on the EPR only, never on a template alone.
- **Reuse.** A PTR is content-addressed. Several EPRs of the same supplier may pin it; EPRs of
  different suppliers never share it.

**What a profile never contains.** The profile schema is strict (`extra="forbid"`); an unknown key is
refused at DRAFT save. A profile never contains:
- a host, a URL to fetch, a query key, a limit, a pacing value, a transport or a credential;
- code, an import path other than a §6 hook binding, or an expression language;
- an unbounded pattern. Text patterns apply only to one already-located text value, under a
  bounded, backtracking-safe grammar the engine owns. A new dependency needs the user's approval
  (`CLAUDE.md` §4);
- a new field key, status, evidence kind or value shape;
- any value presented as a source fact. A profile says **where** the source states a fact, never
  **what** it is.

**Immutable revisions.**
- **Content-addressed.** A revision's digest is SHA-256 over a canonical serialization, scheme
  `icbm-profile/v1`, which includes `profile_schema_version`.
- **Never updated or deleted.** The database enforces this, as it does for `audit_events`. A
  revision that a `ProductFactsRevision` or a `ValidationRun` references stays loadable forever.
- **Digest recomputed on load.** A stored digest that does not match its content refuses the
  bundle, and nothing is collected.
- **Lifecycle is not content.** State lives in a separate append-only transition log: who, when,
  from, to, reason, correlation. `VALIDATED` is derived (§7.1), never stored.
- **Lineage.** Every revision records `parent_revision_id`, `origin` (`OPERATOR` | `AI_PROPOSAL` |
  `IMPORT`), `created_at` and a change note.
- **Supplier gate.** A profile exists only for a supplier whose access envelope and CONNECT
  definition are registered in the build.

### 4. Terminology

These names are fixed (Q1, and cross-audit item 6). `docs/GLOSSARY.md` records them.
- The CONNECT **`SupplierProfile`** (ADR-0007) and the COLLECT access envelope
  **`CollectionProfile`** (ADR-0010) are distinct from each other, and from the EPR and PTR.
- The two field levels are **`CORE`** and **`COVERAGE`** (`FieldLevel`, `app/collect/facts.py`).
  Every non-CORE field is a `COVERAGE` field.
- **ADR-0010 §7 is amended.** It calls the second level "Source coverage" in its table and
  "source-coverage" in its prose. Those words are the historical label of the enum level
  `COVERAGE`, and they are read as `COVERAGE` wherever they appear. ADR-0010 §7 carries an
  amendment note saying so, and no new contract uses them. The level itself is unchanged: its
  fields, its `ABSENT` rule and its acceptance semantics are exactly as ADR-0010 §7 states them.

### 5. Extraction identity and comparability (amends ADR-0010 §6, §12 and ADR-0013 §3)

> **Amendment note (ADR-0019 §4).**
> - Transport is not an input to the semantic tuple, `comparability_key` or any fingerprint.
> - A different transport is, by itself, never drift.
> - Differences in observed evidence follow the existing `EVIDENCE_DRIFT` rules, unchanged.

#### 5.1 Provenance of an Adaptive revision

Exact column names are the schema slice's decision; the semantics are fixed here.

| field | meaning | for a code extractor (KM통상 today) |
| --- | --- | --- |
| `extractor_revision` | **the engine's** semantic revision, e.g. `adaptive-engine-1`. It keeps the ADR-0010 meaning, "the semantic identity of code" | unchanged (`kmretail-1`) |
| `extractor_fingerprint` | the engine manifest fingerprint (ADR-0010 §12 mechanism) — implementation provenance | unchanged |
| `extraction_profile_revision_id` | the EPR used | `NULL` |
| `extraction_profile_digest` | its digest. It covers every pinned PTR and every bound hook's semantic `HOOK_REVISION`, and **no implementation fingerprint** | `NULL` |
| `profile_schema_version` | the profile schema the EPR was parsed under | `NULL` |
| `page_template_revision_id` | the PTR this document matched. Provenance only (§5.5) | `NULL` |
| `hook_fingerprint` | the supplier hook manifest's implementation fingerprint at extraction time, or `NULL` when no hook is bound. Implementation provenance, **outside every semantic identity** | `NULL` |
| `extraction_semantics_id` | the stored digest of the semantic tuple (§5.2), on new Adaptive rows only | not stored |

#### 5.2 The semantic tuple

```text
adaptive_semantics = ("ADAPTIVE", extractor_revision, profile_schema_version, extraction_profile_digest)

extraction_semantics_id = SHA-256( "icbm-extraction-semantics/v1" ‖ LP("ADAPTIVE") ‖ LP(extractor_revision)
                                   ‖ LP(profile_schema_version) ‖ LP(extraction_profile_digest) )
    LP(x) = the 4-byte big-endian length of the UTF-8 bytes of x, then those bytes
```

- **The digest is only a stored form.** `extraction_semantics_id` is SHA-256 over a
  domain-separated, length-prefixed encoding. It is collision-resistant, not collision-free.
  **Nothing in this contract relies on it being injective**, and it never decides comparability by
  itself (§5.3).
- **No implementation fingerprint enters the tuple** (Q4). This covers the engine and the hooks,
  whether directly or through the EPR digest.
  - An implementation-only change keeps comparability.
  - It lapses `VALIDATED` (§7.1), so the profile is revalidated.
  - It is **never by itself `EXTRACTOR_CHANGED`**.
- **Semantic changes are mechanically forced** (§5.6):
  - the engine goldens force `extractor_revision` to advance;
  - the hook goldens force `HOOK_REVISION` to advance, and that forces a new EPR.

#### 5.3 `comparability_key` decides drift comparability

```text
comparability_key(row) =
    ("ADAPTIVE", row.extractor_revision, row.profile_schema_version, row.extraction_profile_digest)
                                        when the row carries profile provenance
    ("CODE", row.extractor_revision)    otherwise — every row that exists today
```

- **Rule.** Two revisions are drift-comparable **exactly when their `comparability_key` tuples are
  equal**.
- **Existing rows.** Two existing rows compare exactly when their `extractor_revision` strings are
  equal. That is the ADR-0013 §3 rule of today, and no hash is involved.
- **The two populations.** A `"CODE"` tuple and an `"ADAPTIVE"` tuple are never equal, because
  their tags and arities differ. This is a structural property of the comparison, not a
  hash-collision claim.
- **Integrity check.** Where a row stores `extraction_semantics_id`, a read-time check recomputes it
  from the row's own tuple. A mismatch makes that row non-comparable (fail closed) and is reported.
  The row is **never corrected in place**.

#### 5.4 No backfill, ever

**No existing `ProductFactsRevision` row is backfilled, updated or rewritten** — not to add a
column value, and not to normalize provenance (ADR-0010 §6).
- `comparability_key` is derived on read for every legacy row.
- New provenance columns are **nullable**. They never carry a default that the database would
  materialize into old rows.
- A code-extractor row written after this ADR may leave them `NULL`; its key is then the same
  `("CODE", …)` tuple.
- New Adaptive rows persist §5.1 only once the schema slice and the cutover ADR authorize Adaptive
  revisions. No shadow stores any of it in a `product_facts_*` table.

**Amendment of ADR-0013 §3.** Wherever that section says "the same `extractor_revision`", read
"equal `comparability_key`". For every row that exists today, the two readings give identical
answers.

#### 5.5 The matched template is provenance, not semantics

- One bundle may match template A on one capture and template B on the next. That is a real
  change in the source's shape. Values extracted under one bundle stay comparable, so value drift
  is still evaluated.
- The template switch is recorded as a structural observation (§8).

#### 5.6 Reproducibility and its guards (amends ADR-0010 §12)

ADR-0010 §12 promises that a revision can be reproduced from the repository. For an Adaptive
revision the promise becomes: **reproducible from the repository at the engine and hook
fingerprints the revision records, plus the stored immutable profile rows it names.** The
fingerprints serve reproduction and validation freshness. They are not semantic identity.

1. **Pins.** The existing manifest mechanism covers the engine package. A separate hook manifest
   covers each supplier (§6.2).
2. **Engine goldens.** Synthetic fixtures carry expected outputs per engine `extractor_revision`.
   An engine semantic change without a revision advance fails the build.
3. **Digest recomputation** happens on every load (§3).
4. **Hook goldens and the hook revision check.**
   - Every bound hook has synthetic golden cases, recorded under its supplier's `HOOK_REVISION`.
     A behaviour change without a revision advance fails the build.
   - At load, the `HOOK_REVISION` an EPR binds must equal the running hook manifest's `HOOK_REVISION`,
     or the bundle is refused.
5. **Clean-tree campaigns** are unchanged (ADR-0010 §12 guard 3).

### 6. Site adapter hooks

#### 6.1 Hook points (closed)

A new hook point needs an amendment of this ADR.

| hook point | input | output |
| --- | --- | --- |
| `identity_decode` | one located, source-stated text | a `source_product_id` candidate, or `CANNOT_PARSE` |
| `value_parse` | a field key and one located observed text | a candidate of that field's `FIELD_REGISTRY` value model, or `CANNOT_PARSE` |
| `option_decode` | a located option-control fragment | an ordered options candidate, or `CANNOT_PARSE` |
| `embedded_decode` | a declared embedded data block | a JSON-like tree for locators to address, or `CANNOT_PARSE` |

**What is never a hook:**
- fetching, or building a URL to request;
- a host decision;
- the stock verdict;
- a `FieldStatus`;
- the identity agreement decision;
- an image's role;
- the choice between two conflicting values.

**A hook emits no status and no evidence.**
- The engine validates a hook's output strictly against the field's value model and decides the
  status from its own evidence rules.
- It records the `EvidenceKind` of the input the hook read, and names the hook in provenance.
- `CANNOT_PARSE` becomes `REVIEW_REQUIRED`.

#### 6.2 Where hooks live and how they are identified

- **Location.** Hooks live in `integrations/suppliers/<key>/hooks/`. They are pure, fall under
  `test_supplier_packages_hold_site_knowledge_only`, and import nothing but the value models.
- **Manifest.** `integrations/suppliers/<key>/hook_identity.py` holds exactly three constants on
  the ADR-0010 §12 pattern:
  - `HOOK_REVISION` — semantic;
  - `HOOK_INPUTS` — every module of `hooks/`, never the manifest itself;
  - `HOOK_FINGERPRINT` — implementation.

  The pin is the same acyclic, recomputed pin. It is separate from the `collect/` manifest, so a
  code extractor's identity is untouched.
- **What binds where.**
  - The EPR binds `HOOK_REVISION` only.
  - `HOOK_FINGERPRINT` is recorded as `hook_fingerprint` (§5.1) and in each `ValidationRun`
    freshness tuple (§7.1).

#### 6.3 The promotion key

Every hook binding carries three parts, and the engine refuses a binding that lacks any of them.
- **`hook_point`** — from the closed enum of §6.1.
- **`target`** — follows from the hook point:
  - `identity` for `identity_decode`;
  - a `FIELD_REGISTRY` key for `value_parse` (an unknown key is refused);
  - `options` for `option_decode`;
  - `embedded` for `embedded_decode`.
- **`format_class`** — a closed, engine-owned enum per hook point. v1:
  - `identity_decode`: `PATH_CODE`, `ENCODED_TOKEN`, `COMPOSITE_CODE`;
  - `value_parse`: `MONEY_TEXT`, `QUANTITY_TEXT`, `CONDITIONAL_POLICY_TEXT`, `LABELLED_TEXT`;
  - `option_decode`: `SELECT_CONTROL`, `BUTTON_GROUP`, `SCRIPT_MATRIX`;
  - `embedded_decode`: `KEY_VALUE_BLOCK`, `SCRIPT_ASSIGNMENT`.

  A new class is an engine change with its own review.

```text
promotion_key   = (hook_point, target)                  the trigger and the counting unit; derived
promotion_group = (hook_point, target, format_class)    reported beside it; never suppresses the flag
```

- **Computed on every validation run**, over the bindings of every non-`RETIRED` EPR of every
  supplier. No operator action clears it.
- **Resolved only by an architect decision recorded against the `promotion_key`**. The decision
  either promotes the behaviour into a generic extractor or profile rule, in which case the hooks
  are retired through new EPRs, or keeps it site-specific with a reason.

#### 6.4 Anti-growth guards

| # | guard | enforcement |
| --- | --- | --- |
| G1 | no supplier-specific branch in core | an AST repository rule: no comparison against a registered `supplier_key` literal, and no `integrations.suppliers.<key>` import, in `app/` or the engine |
| G2 | no unknown profile key | strict schema at DRAFT save |
| G3 | closed hook points | an engine-owned enum; an unknown binding refuses the bundle |
| G4 | measurable adapter use | hook invocations per supplier × `promotion_key` in every conformance and shadow record; profile-only = zero bindings |
| G5 | promotion review | when EPRs of two or more suppliers share a `promotion_key`, validation computes `HOOK_PROMOTION_REVIEW_REQUIRED` for each of them; `ACTIVE` is refused until an architect decision names that key |
| **G6** | **per-EPR cap, in bindings** (cross-audit item 4) | an EPR that binds **more than two distinct `(hook_point, target)` bindings** is refused `VALIDATED` until an architecture review is recorded. The count is over `promotion_key`s, **not over distinct hook points**: two `value_parse` bindings for `prices` and `shipping` count as two |
| G7 | proven paths only | no `VALIDATED` unless every bound hook is exercised by an approved sample or fixture |

### 7. Lifecycle, validation and samples

#### 7.1 States

| state | how it is entered | may be used for |
| --- | --- | --- |
| `DRAFT` | a strict schema parse passes, the digest is computed and lint findings are recorded. An unmapped CORE field is allowed in a DRAFT. Costs no network | offline evaluation against samples |
| `VALIDATED` | **derived**: a `PASS` `ValidationRun` exists for this exact freshness tuple (below). A `ValidationRun` ends `PASS`, `FAIL` or `INCOMPLETE`; only `PASS` counts | shadow |
| `SHADOW` | a designation: VALIDATED plus the per-supplier shadow switch | shadow comparison (§10) |
| `ACTIVE` | **not authorized by this ADR** — see §7.4 | canonical revisions, after the cutover ADR |
| `RETIRED` | an explicit transition; the revision is never deleted | reading history |

**The freshness tuple** is:

```text
(extraction_profile_digest, profile_schema_version, engine extractor_revision,
 engine extractor_fingerprint, hook_fingerprint, sample-set digest, capture_revision)
```

- A change to any member lapses `VALIDATED`. No stored flag exists that could be forgotten.
- The freshness tuple **includes** the implementation fingerprints; the semantic tuple (§5.2)
  **excludes** them.

#### 7.2 Validation checks — deterministic, zero network

- **V1 referential.**
  - Every pinned PTR exists and recomputes to its digest.
  - Every hook binding resolves to a running hook manifest whose `HOOK_REVISION` equals the bound
    one.
  - Every manifest is current.
- **V2 coverage** (cross-audit item 5):
  - **Parser fields.** Every CORE field in `SUPPLIED_FIELDS` (`original_name`, `prices`, `options`,
    `stock`) has a field rule in every PTR.
  - **`IMAGES_FIELD` (`images`) is CORE, but no parser field produces it.** The image pipeline
    produces it (`SUPPLIED_FIELDS` excludes it). V2 therefore covers it separately:
    - (a) the EPR declares image-role rules that can assign the representative role;
    - (b) **every PTR** declares at least one image region locator those rules apply to;
    - (c) every approved sample states its expected ordered image references by role and ordinal.

    A PTR without an image region, or an EPR whose rules cannot assign a representative image, fails
    V2.
  - **COVERAGE fields** need either a rule, or the rule's own observable absence condition
    (§8.2). A missing COVERAGE rule is a lint finding, never an `ABSENT` fact.
- **V3 sample agreement.** For each approved sample, the engine output equals the sample's expected
  facts on every must-match dimension of §10.3.
  - At least one sample per template, and at least two in total.
  - **The operator authors or verifies the expected facts from the source — never AI, and never
    the profile under validation.**
  - Offline validation fetches no bytes. Image agreement compares role, ordinal and reference
    identity, and it compares checksums only where the capture recorded them.
- **V3a conflict scan.** Generic, profile-independent detectors run over the **whole** captured
  snapshot, including every product control the capture kept (§7.3). They cover:
  - price-like label rows;
  - purchase, cart and sold-out controls;
  - option selectors and their states;
  - identity declarations;
  - JSON-LD offers.

  Any statement they find that the bundle neither reads nor explicitly disposes of must be
  resolved by the operator before `PASS`. V3a scans the admissible embedded data too. It never
  scans an excluded non-authoritative region, because that region is not in the sample.
- **V4 negative controls.** A login page, a non-product page, and a mutation suite derived
  deterministically from each sample must fail closed exactly as §8 says. The mutations include:
  - removing a required anchor;
  - duplicating a price row;
  - injecting hidden sold-out text;
  - adding a second, conflicting identity;
  - removing the image region.
- **V5 determinism.** Two evaluations give identical outputs and digests.
- **V6 safety.**
  - The evidence sanitizer and the secret scan pass.
  - Observed fragments stay within 4 KiB.
  - Nothing appears that ADR-0010 §8 forbids.
- **V7 hook guards.** G5–G7.
- **V8 complete samples only.** A `ValidationRun` that includes a sample marked
  `SAMPLE_TRUNCATED` (§7.3) ends **`INCOMPLETE`**, never `PASS`.
  - Nothing may treat the omitted bytes as inspected: no locator, no hook exercise (G7), no expected
    fact, no `ABSENT` decision and no V3a result.
  - The truncated block's digest is kept for diagnostics only.
  - Validating that path requires a different, bounded capture.

#### 7.3 `ValidationSample`: independent capture and its own retention

> **Amendment note (ADR-0019 §5, §6).**
> - **How the two owners relate.** The `BrowserCapturePolicy` cuts an `EXTENSION` capture to the
>   product scope in the browser, **before** any sanitizer. The sample sanitizer below, with its
>   `capture_revision`, then applies unchanged to what arrived, and so does the server's final
>   scan.
> - **Independence is unchanged.** Both are independent of the EPR and PTR being validated, so the
>   independent-capture-owner principle holds, and so does AC-11.

A `ValidationSample` is what V3, V3a and V4 replay: a sanitized, product-scoped **structured**
snapshot (an element tree with its text, attributes, control state and image references), plus the
operator-verified expected facts.

**An independent capture owner decides what a sample contains. The bundle under validation never
does.** The snapshot is cut at capture time by two things.

1. **A versioned capture sanitizer** (`capture_revision`). It has its own parser and its own
   generic rules, and those rules are profile-independent. It strips by what a value **is**,
   never by element type.

   | kept, sanitized — product-scoped source evidence, `CONTROL_STATE` among it (ADR-0010 §8) | stripped |
   | --- | --- |
   | purchase, buy and cart buttons and their enabled/disabled/hidden state | credentials, passwords, tokens, CSRF and other hidden security fields |
   | sold-out and restock controls and markers | authorization, session and cookie material in any attribute or value |
   | option selectors (`select`/`option`, radio and button groups) with labels, order, selected/disabled state and non-secret values | member and account fields: names, IDs, grades, points, addresses, contacts |
   | quantity-input structure and bounds (`min`, `max`, `step`), never a user-entered value | cart and account submission payloads, and every user-entered or private value |
   | the forms that carry the above, reduced to structure; an `action` kept only as a sanitized path under ADR-0010 §9 | account, member and navigation regions; secret-bearing URL material |
   | **admissible embedded data** (below), as a parsed data tree, never as script text | executable code: every script that is not admissible embedded data, and every part of one that is not a literal |

   - A `form`, `input`, `select` or `button` is never removed merely for being a control.
   - **Admissible embedded data.** `EvidenceKind.EMBEDDED_JSON` and `JSON_LD` stay canonical, and
     `embedded_decode` / `SCRIPT_ASSIGNMENT` (§6) must stay provable under V3 and G7. The
     capture sanitizer therefore keeps a bounded, sanitized, product-scoped representation of
     embedded data. It uses only its own generic rules, never a profile's:
     - **What qualifies:** a JSON-LD block; a `<script>` whose type declares JSON data; or a
       top-level assignment of one **pure literal** (object, array, string, number, boolean or null)
       to a name. The sanitizer's own strict literal parser decides this. A block with a function,
       call, operator, template, reference or any other non-literal is not admissible and is
       stripped whole.
     - **How it is stored:** as its parsed data tree, with the assignment target's name where there
       is one. It is never stored as script text, and it never becomes executable again.
     - **What is stripped from inside it:** every key or value that is a credential, token, session,
       authorization or CSRF value, a member or account field, a cart or account payload, or
       secret-bearing URL material. Keys are matched by generic name rules and values by the same
       secret scan the rest of the sample passes. Each removal is recorded.
     - **Bounds:** at most 64 KiB per block and 256 KiB of embedded data per sample, after
       stripping.
       - A larger admissible block inside the product scope is kept **by digest only**, for
         diagnostics.
       - The whole sample is then marked **`SAMPLE_TRUNCATED`**, and it can never support a
         `PASS` (V8).
       - A digest is never proof material: it cannot show what facts or conflicts the omitted tree
         held. A block is never silently cut.
     - **Scope:** a block qualifies only inside the recorded product scope (point 2). Page-wide
       analytics, advertising, consent and tracking configuration is outside it by the
       non-authoritative rule below.
   - When a kept control has a secret-bearing attribute, the control is kept, that attribute is
     removed, and the removal is recorded.
2. **An operator-approved product scope.** The operator chooses it on the captured page, and it is
   recorded with the sample (who, when, the boundary) **before** any candidate profile evaluates
   the sample.
   - The default scope is the document body after the sanitizer's exclusions **and after its
     non-authoritative exclusions**.
   - **Non-authoritative regions are not product scope.** These regions are never source authority
     (ADR-0010 §10: review badges and description text do not decide stock):
     - customer reviews and ratings;
     - Q&A;
     - recommendations and related or recently viewed products;
     - other user-generated content;
     - advertising.

     They also carry other people's personal data. The capture sanitizer identifies them by its
     own generic rules and the operator confirms each one. They are excluded from the sample and
     recorded as excluded (their boundary and class, never their content). So they never become
     V3 or V3a source evidence merely because the scope started from the body.
   - The operator may exclude further regions, for privacy or as another non-authoritative region,
     and each exclusion is recorded with its reason. The operator may never exclude a region
     **because a candidate profile disagrees with it**. No profile has been evaluated at the time
     the scope is recorded.
   - A region is re-included only by a new capture with a new recorded scope, never by editing a
     sample.

**What a profile may do with a sample.**
- No EPR or PTR, candidate or validated, may define, narrow or filter a snapshot. A bundle only
  **reads** it.
- A sample's provenance names its `capture_revision` and its scope decision, never a profile. A
  `ValidationRun` refuses any sample whose provenance names a profile.

**What a sample never is or holds.**
- Never whole authenticated HTML.
- Never cookies, headers, session or authorization material.
- Never account, member or page-wide data.
- Never secret-bearing URL material (ADR-0010 §8, §9).

A snapshot that fails the sanitizer or the secret scan is not saved.

**Integrity and retention.**
- **Immutable and content-addressed.** A corrected expectation is a new sample. A sample-set
  digest is taken over the ordered sample digests.
- **Retained while referenced.** A sample is kept while any `ValidationRun` or recorded profile
  proof references it. No shadow bound reaches it (§10.5).
- **Local only.** Samples stay in the data root and are never committed. Repository fixtures are
  synthetic.

#### 7.4 An `ACTIVE` switch is not a pointer move

ADR-0013 §3 advances a `SourceProduct`'s current source revision only to a newly recorded,
eligible `ProductFactsRevision`. An activation records none.

- **What an `ACTIVE` switch writes.** It writes **only** one append-only lifecycle transition. It
  writes no `ProductFactsRevision`, no pointer row, no derived-result invalidation and no ReviewItem.
- **When the pointer moves.** A later canonical collection records a new eligible revision under
  the newly active bundle, and that revision becomes current by the unchanged ADR-0013 §3 rule. Its
  `comparability_key` differs, so **that** pointer move is recorded as `EXTRACTOR_CHANGED`, with the
  usual invalidation.
- **No new revision, no pointer move.** An uncollected product keeps its current revision and its
  provenance indefinitely. It is never re-labelled, re-evaluated or backfilled.
- **Leaving `ACTIVE`** works the same way: the transition only.
- **What `ACTIVE` itself requires:**
  - the cutover ADR;
  - Phase C evidence (§11);
  - no unresolved `HOOK_PROMOTION_REVIEW_REQUIRED` (G5);
  - the user's approval.

  At most one bundle per supplier is `ACTIVE`.

### 8. Template conformance and the three drift owners

| question | owner | inputs | output |
| --- | --- | --- | --- |
| Is this profile trustworthy enough to be `VALIDATED`? | **profile validation** | a bundle, the engine identity, approved samples, the negative-control suite | an immutable `ValidationRun` (§7) |
| Does the page still fit the validated template? | **structural drift** — template conformance inside the engine, on every extraction | the document and the bundle | a conformance record; the affected fields fail closed |
| Did the source's values change? | **source-value drift** (`docs/ARCHITECTURE.md` §11, ADR-0013 §3) | two revisions with **equal `comparability_key`** | unchanged, except for the §5.3 comparability condition |

#### 8.1 Template matching fails closed

- A template signature is a predicate: every required anchor is present, and no forbidden anchor
  is. There is no score and no closest match.
- Exactly one template must match. No match → `TEMPLATE_UNMATCHED`. More than one →
  `TEMPLATE_AMBIGUOUS`.
- **In the shadow this is a verdict.**
- **For the cutover ADR (Q3).** Nothing implements this before that ADR:
  - if stable source identity still resolves but no unique template matches, **append a new current
    revision** whose affected facts fail closed as `REVIEW_REQUIRED`, with a structural reason;
  - if identity is unresolved or contradictory, **no revision** is appended (ADR-0010 §11).

#### 8.2 Inside a matched template

- All of a field's locators are evaluated. If two hit with different normalized values, the field
  is `REVIEW_REQUIRED`; the engine never picks one.
- If only a declared alternative hits, the value stands only when that alternative was proven on
  samples. The conformance record then carries `ALTERNATIVE_USED`, which is a structural-drift
  signal.
- A violated expected cardinality → `REVIEW_REQUIRED`.
- `ABSENT` requires that the rule's own absence condition is observed in the document. A missing
  anchor is never `ABSENT`.
- The drift owner never edits, learns, proposes or silently demotes a profile.
  - Profile health (`HEALTHY` / `DEGRADED`) is derived from recent conformance records.
  - A conformance failure that leaves a field `REVIEW_REQUIRED` reaches review through the existing
    COLLECT producer as `COLLECT_EVIDENCE`.
  - A new owner reason code for it is added only by the slice that implements it, under ADR-0016.

### 9. AI boundary

- **Production collection and the shadow make zero AI, OCR or vision calls**, and this is
  enforced structurally:
  - the engine, profile runtime, validation and shadow packages are source-truth roots under
    `test_collect_source_truth_path_imports_no_ai_ocr_or_marketplace_code`;
  - the campaign hard-zero list covers them.
- **Onboarding assistance is a separate package**, outside the source-truth roots.
  - It uses the ADR-0012 port, with prompts only from the persisted `PromptTemplate`.
  - Deterministic candidate generation comes first. AI may only rank candidates or propose a
    DRAFT, and onboarding works with AI disabled (ADR-0012 §9).
- **AI never:**
  - authors or verifies expected sample facts;
  - saves a profile without an operator action;
  - transitions a state;
  - triggers a read;
  - appears in any production path.

  An AI-proposed DRAFT carries `origin = AI_PROPOSAL`, with the AI profile and the prompt revision.
- **Egress.** AI sees only a `ValidationSample`. Sending one to a cloud provider is data egress
  under ADR-0012 and needs the user's approval.

### 10. One-fetch shadow comparison

> **Amendment note (ADR-0019 §2, §4).** The shadow is **transport-neutral**.
> - It compares over the one `DocumentView` the canonical extractor read, whether it came through
>   `EXTENSION` or `DIRECT_URL`.
> - **S1** (zero additional supplier requests) applies equally to the one `DocumentView` the
>   extension brought.
> - The **transport** is added to §10.3's "may differ (never a mismatch)" column.

#### 10.1 Where the shadow runs, and the transaction boundary (cross-audit item 2)

- **Placement.**
  - The shadow runs inside `ProductCollectionService.collect`, in the same call, over the same
    in-memory `DocumentView`.
  - It runs after the canonical revision append, and also on the unresolved-identity return, where
    the canonical side is `UNRESOLVED(reason)`.
  - It does not run on an attempt that recovered an already-appended revision, because that
    attempt read no document (§11.2).
- **The per-run decision is frozen at the reservation.**
  - When a run first reserves its product read (`CollectionRunStore.reserve_product_read`), the same
    canonical write unit records `shadow_enabled_for_run`. That is the identity of the shadow-switch
    history entry in effect at that moment, or an explicit *disabled*.
  - A retry of the same run keeps the value its first reservation froze. It is never re-evaluated.
  - **Both** the shadow step of that run and the Phase C denominator (§11.1) read that one frozen
    value. Neither consults the switch's current setting.
  - A switch change after the reservation affects **later reservations only**.
  - This is the one canonical-run field this contract requires. It is an additive, nullable field
    on the run record, written only by the run store, and added by its authorized production slice
    with no backfill. A run without it is never shadow-eligible.
- **Ordering.** The canonical revision's write unit **has committed before the shadow starts**.
- **The shadow's own write.**
  - The engine evaluation is pure and holds no write unit.
  - The shadow then writes its record in **its own separate write unit**, opened only after the
    canonical unit has exited.
  - It is never nested in, and never shares, a canonical write unit.
  - This is a correctness requirement, not a style choice. `Database.write` refuses a nested unit
    and **poisons the enclosing unit, which then rolls back** (`app/db/database.py`). A shadow
    write nested in the canonical unit would therefore undo the canonical revision.
- **Failure.** A shadow failure, timeout or rollback can never undo, delay past its commit, or
  change the canonical revision, run outcome or audit.

#### 10.2 Shadow invariants

| # | invariant | how it is made structural |
| --- | --- | --- |
| S1 | zero additional supplier requests | the runner receives only a `DocumentView`, the source URL, the canonical run's in-memory image candidates and the stored references; it gets no gateway, session, budget or egress handle; a differential test proves ledger reservations are equal with the shadow on and off |
| S2 | canonical invariance | a differential test with the shadow on and off: identical run outcome, revision rows, fingerprints, job state, canonical audit events, ReviewItems and current source revision pointer |
| S3 | failure isolation | every shadow exception becomes a `SHADOW_FAILED` record, or a log line if the shadow's own write fails, and never reaches the run; the engine is step-bounded |
| S4 | no canonical write | the shadow package may not import the revision store, source-asset recorder, run store, review owner or pointer owner (repository rule), and it opens no canonical write unit |
| S5 | no body persistence | `body` stays in memory (ADR-0010 §3) |
| S6 | no AI | §9 |
| S7 | off by default, **frozen per run** | enabled per supplier by explicit configuration whose changes form an append-only history. Each run's decision is **frozen once, at its first product-read reservation** (§10.1); disabled means zero engine calls |

#### 10.3 What is compared

Both sides produce `FieldFact`s, and both go through the same pure `evaluate` in memory.

| must match | may differ (never a mismatch) | observed only |
| --- | --- | --- |
| identity: resolved or unresolved, and `source_product_id` | evidence `locator`, `digest`, `ordinal` | evidence count per field |
| per field: `FieldStatus` | `field_fingerprint`, `source_fingerprint` | `EvidenceKind` distribution |
| per field: normalized value as canonical JSON, keeping source order where order is a fact | `extractor_revision`, `extractor_fingerprint`, `extraction_semantics_id` | alternative usage |
| options: the exact ordered atomic configuration set, including option-level sold-out evidence | image `provenance` text | hook invocations per `promotion_key` |
| stock availability | | template matched, and a template switch |
| images: per role, the ordered `(role, ordinal, sha256)` and each reference's status, by §10.4 | | engine steps |
| `facts_status`, as a consistency check | | |

**Verdicts.**
- Per field: `MATCH`, `STATUS_MISMATCH` or `VALUE_MISMATCH`. `ABSENT` vs `REVIEW_REQUIRED` is a
  mismatch.
- Per run: `MATCH`, `MISMATCH`, `IDENTITY_MISMATCH`, `TEMPLATE_UNMATCHED`, `TEMPLATE_AMBIGUOUS`,
  `IMAGE_UNMATCHABLE` (§10.4) or `SHADOW_FAILED`.

**Severity**, most severe first:
1. `CONFIDENT_DISAGREEMENT`;
2. `ADAPTIVE_OVERCONFIDENT`;
3. `ADAPTIVE_CONSERVATIVE`.

The engine inherits the M3 boundary (`docs/acceptance/M3.md` §2). Until an ADR accepts positive
option and tier support, an Adaptive `CONFIRMED` option or tier value is `ADAPTIVE_OVERCONFIDENT`.

**Resolution.**
- The canonical extractor is not presumed correct.
- A human resolves each mismatch from source evidence and approved fixtures, recording
  `CURRENT_CORRECT`, `ADAPTIVE_CORRECT`, `BOTH_WRONG` or `SOURCE_AMBIGUOUS` with the evidence used.
- A resolution never edits a canonical revision.
- A canonical defect is a separate `kmretail-*` fix under its own authorization.
- **A shadow mismatch is never a `ReviewItem`.**

#### 10.4 Image matching, including references with no stable locator (cross-audit item 3)

The shadow never fetches (S1). Matching happens **in memory, in the same run**, between the
canonical run's image candidates and the Adaptive candidates. **A persisted locator is never used
to match**, so a reference with no stable locator (the ADR-0010 §9 provenance-only case) is matched
exactly like any other. Each Adaptive candidate is tried against the following rules, in order,
and the first rule that decides it applies.

| step | match on | when it applies |
| --- | --- | --- |
| M1 | the **exact resolved reference**: the absolute URL both sides resolve against the same document URL, query included, compared as a string in memory | both sides have a resolved reference |
| M2 | the **exact written reference**: the reference text exactly as the page wrote it (`ImageCandidate.source`), compared in memory | either side has no resolvable target, e.g. an unparseable or refused reference |
| — | duplicates: equal references are paired by their occurrence index in each side's own order | the same reference appears more than once |

**Outcomes per reference.**

| outcome | meaning | counts as |
| --- | --- | --- |
| `MATCHED` | paired by M1 or M2; role, ordinal and status are compared; the SHA-256 is **inherited** from the canonical fetch, stated as inherited, never independently observed | compared under §10.3 |
| `MATCHED_NO_BYTES` | paired, but the canonical run observed no bytes (fetch failure, budget, refusal); no checksum is claimed on either side | compared on role, ordinal and status only |
| `UNOBSERVED` | an Adaptive candidate with no canonical partner; it is never fetched | a mismatch |
| `MISSED` | a canonical reference with no Adaptive partner | a mismatch |
| **`UNMATCHABLE`** | M1 and M2 cannot pair a reference **uniquely** — for example, occurrence counts that conflict with differing roles, or a reference that neither side can state as a resolved or written reference | **never a match and never a success**; the run verdict is `IMAGE_UNMATCHABLE`, which §11 counts as `INCOMPLETE` |

**Persistence of image comparisons.**
- The shadow record keeps only:
  - each side's role and ordinal;
  - the outcome;
  - the canonical SHA-256 where one was observed.
- It never keeps a URL, a written reference, or a plain digest of either. For a tokenized or
  secret-bearing reference a plain digest would be persisting secret material (ADR-0010 §9). No
  keyed digest is needed, because matching happened in memory.

#### 10.5 The shadow store and its retention

- **Ownership.** The shadow store is a separate, non-canonical owner. Nothing canonical
  references it or joins it.
- **Keys.** A shadow record's required key is **`collection_run_id`**, with at most one record per
  run.
  - `revision_id` is a **nullable**, one-way reference. It is present when the canonical side
    appended a revision.
  - It is **absent for a `NO_REVISION` (identity-unresolved) run**, whose canonical side is
    `UNRESOLVED(reason)`.
  - A run with no shadow record has no raw record; its `OUTCOME_RECORDED` ledger event (§10.5,
    §11.2) carries only the `collection_run_id`, the absence of a verdict and its cause.
- **Committed evidence** drawn from it follows ADR-0010 §6: counts and statuses, keyed
  fingerprints, and no plain digests of business values.
- **Raw shadow records: a hard bound with no exception.**
  - A raw shadow record is kept at most **90 days** and at most **5,000 per supplier**,
    whichever bound is reached first.
  - **There is no hold.** An open or cited window never keeps a raw record past either bound.
  - A missing bound is a refusal, never a code default.
  - Pruning never touches a canonical row.
- **What a window keeps instead: its evidence ledger** (re-audit `5307562621`). The ledger is an
  **append-only event stream per `collection_run_id`**. Nothing in it is ever updated or deleted.
  One **effective state** per run is derived from that stream.
  - **Events.** Each event is immutable and sequence-numbered within its run. Each carries the
    `collection_run_id`, the window, the time and the correlation. An event holds **no source
    values**. The three kinds:

    | event | appended when | carries |
    | --- | --- | --- |
    | `OUTCOME_RECORDED` | once per eligible run, in the same shadow write unit as the run's shadow record, or by the startup reconciliation when the run has none (§11.2) | the `revision_id` or its absence, the per-field and run verdicts, the §11.1 count-as outcome and its cause |
    | `RESOLUTION_RECORDED` | a human resolution (§10.3) is recorded, and only while the effective state is `UNRESOLVED_MISMATCH` | the resolution, its evidence reference, and the count-as outcome it yields |
    | `RAW_PRUNED_UNRESOLVED` | the run's raw shadow record is pruned **while its effective state is still `UNRESOLVED_MISMATCH`** — in the same shadow write unit as the prune | the prune time |

  - **The effective state** is a pure fold over the run's events, in sequence order.
    - It starts from `OUTCOME_RECORDED`.
    - A `RESOLUTION_RECORDED` replaces an `UNRESOLVED_MISMATCH` with the outcome of its resolution:
      a success or a failure.
    - A `RAW_PRUNED_UNRESOLVED` replaces an `UNRESOLVED_MISMATCH` with `INCOMPLETE`, cause
      `PRUNED_BEFORE_RESOLUTION`.
  - **Terminal and non-terminal states.** `UNRESOLVED_MISMATCH` is the **only non-terminal**
    effective state. Every other state is terminal: a success, a failure, and every other
    `INCOMPLETE` cause. No event is ever appended to a run whose effective state is terminal.
    - A resolution after `PRUNED_BEFORE_RESOLUTION` is refused; the evidence it needs is gone.
    - Pruning the raw record of a resolved or otherwise terminal run appends nothing, and changes
      nothing.
  - **Counted once.** The denominator counts each eligible `collection_run_id` **exactly once**,
    through its single effective state, never through its number of events. A run belongs to at
    most one window, because at most one window per supplier is open at a time (§11.1).
  - **What verdicts read.** Window and bundle verdicts (§11.3) read **only** effective states. The
    full event history is retained beside them and is part of the evidence.
- **Retention of the ledger.** The ledger has a retention class of its own. It is bounded by what it
  counts: at most three events per eligible collection, and eligible collections are
  operator-submitted real reads under the frozen request budget (ADR-0010 §4). It is kept while its
  bundle is not `RETIRED`, or while recorded acceptance evidence cites the window.
- **A raw record pruned before its outcome is settled.** The evidence needed to resolve a mismatch
  lives in the raw record, so a resolution must happen inside the raw bound. Otherwise the prune
  appends `RAW_PRUNED_UNRESOLVED`, and the run's effective state becomes terminal
  `PRUNED_BEFORE_RESOLUTION`.
- `ValidationSample` retention is separate (§7.3).

### 11. Phase C evidence (cross-audit item 1)

#### 11.1 Windows and the denominator

- **Declaring a window.** An evidence window is declared **before** its first eligible collection.
  The declaration records the supplier, the bundle (its `comparability_key`), the start and the
  minimum size K.
- **One open window per supplier.** At most one window per supplier is open at a time.
- **Window events.** A window has its own append-only events: `DECLARED`, `ENDED`, `CLOSED` and,
  where §11.2 allows it, `SUPERSEDED`. None is ever updated or deleted.
  - **`ENDED`** stops eligibility. A run whose first reservation comes after the end is not in the
    window. A window may end at any time, and ending it records no verdict.
  - **`CLOSED` is the final closeout.** It may be recorded **only when every run in the window has a
    terminal effective state**. While any run is still `UNRESOLVED_MISMATCH`, the window can end
    but cannot close.
  - **No run stays open forever.** An unresolved mismatch becomes terminal either by its resolution
    or, at the latest, by `RAW_PRUNED_UNRESOLVED` when its raw record reaches the 90-day or
    5,000-record bound (§10.5).
  - **What the closeout records**, immutably:
    - the window's verdict and its denominator;
    - each run's effective state;
    - the sequence number of the last event each state was derived from.

    Every state it records is terminal, so no later event can change it. The closeout is therefore
    **never revised, versioned or mutated**.
  - A window never ends or closes by pruning or by omission.
- **A window belongs to one bundle.** A profile fix is a new EPR and a new `comparability_key`, so
  it opens a new window.
- **Eligible collection.** A canonical collection run qualifies when all three hold:
  - its terminal outcome is `RECORDED` or `NO_REVISION`, so a product document was read;
  - its frozen `shadow_enabled_for_run` (§10.1) names an enabled switch entry;
  - its first product-read reservation falls inside the window.
- **Denominator.** Every eligible collection is counted. The denominator is derived from the
  canonical run records and their frozen per-run shadow decision, **never from the shadow store**
  and never from the switch's current setting. The shadow step ran for exactly the runs the
  denominator counts, because both read the same frozen value.

**What each eligible collection counts as.**

| shadow outcome | counts as |
| --- | --- |
| `MATCH` | success |
| a mismatch resolved `ADAPTIVE_CORRECT` | success for the Adaptive side (the canonical defect is filed separately) |
| resolved `SOURCE_AMBIGUOUS` | success only if the Adaptive side failed closed on every ambiguous field |
| resolved `CURRENT_CORRECT` or `BOTH_WRONG` | failure |
| an unresolved mismatch | `INCOMPLETE`, cause `UNRESOLVED_MISMATCH` — **blocking** (§11.3) until it is resolved |
| a mismatch whose raw record was pruned before resolution (§10.5) | `INCOMPLETE`, cause `PRUNED_BEFORE_RESOLUTION` — **blocking**; it can no longer be resolved |
| `TEMPLATE_UNMATCHED` / `TEMPLATE_AMBIGUOUS` | failure, unless resolved `SOURCE_AMBIGUOUS` |
| `IMAGE_UNMATCHABLE` | `INCOMPLETE`, cause `IMAGE_UNMATCHABLE` — **blocking**; it is never a success and never resolvable (§10.4) |
| `SHADOW_FAILED` | failure |
| **no shadow record** after a crash between the canonical commit and the shadow record (§11.2) | **`INCOMPLETE`**, cause `SHADOW_MISSING_AFTER_RECOVERY` — the **only non-blocking cause** (§11.3) |
| **no shadow record** for any other reason, such as a failed shadow write | **`INCOMPLETE`**, cause `SHADOW_MISSING` — **blocking** |

Each `INCOMPLETE` effective state carries **exactly one** of these causes, derived from the run's
ledger events (§10.5). No other cause exists, and a new one needs an amendment of this ADR.
`UNRESOLVED_MISMATCH` is the only one that is not terminal.

#### 11.2 A crash between the canonical commit and the shadow record

The failure window is fixed by §10.1: the canonical revision's unit has committed, and the process
dies before the shadow's own unit commits.

- **What happens to the run.** The recovery attempt settles the run `RECORDED` from the revision it
  already appended (the existing `collect.product` recovery path). It reads no document, so it
  **cannot and does not** run the shadow. The body was never persisted (S5), and no refetch exists
  for comparison's sake (S1).
- **What happens to the evidence.**
  - The run is **eligible**. It stays **in the denominator**, and it counts **`INCOMPLETE`**, with
    the cause `SHADOW_MISSING_AFTER_RECOVERY`.
  - It is never excluded, never counted as a success, and never retried away.
- **Missing records are made visible.** At startup, the shadow owner's reconciliation lists every
  eligible run, in a window that is not closed, that has no `OUTCOME_RECORDED` event. For each
  one it appends that run's `OUTCOME_RECORDED` in its own write unit.
  - The cause is **`SHADOW_MISSING_AFTER_RECOVERY`** only when the run's canonical job history shows
    that the §11.2 recovery path settled it: an attempt recovered a revision that was already
    appended.
  - Otherwise the cause is `SHADOW_MISSING`.
  - The denominator never depends on that event existing (§11.1).
- **The window can never pass.** Any `INCOMPLETE` makes the window `INCOMPLETE`. That window can
  never become `PASS`, because the missing comparison cannot be recreated. It stays recorded, with
  its missing runs listed.
- **The one supersession this contract allows.** A **closed** window whose `INCOMPLETE` effective
  states are **all** `SHADOW_MISSING_AFTER_RECOVERY` may be superseded by a new window for the
  **same** bundle.
  Nothing else in that window may be a failure or a blocking cause.
  - The supersession is recorded before the new window's first eligible collection: the superseded
    window, the reason `SHADOW_MISSING_AFTER_RECOVERY`, the runs it names, and who and when.
  - The superseded window stays in the final evidence, verdict and all. It is **never deleted,
    closed away or abandoned**.
  - This is the only exception (§11.3).

#### 11.3 The Phase C verdict and its gates

- **Window verdict.**
  - `PASS` only when every eligible collection counts as a success, and the window holds at least
    K = 3 eligible collections, at least one of them after a process restart (the fresh-session
    condition, `CLAUDE.md` §9).
  - `FAIL` when any collection counts as a failure.
  - `INCOMPLETE` otherwise: some entry counts `INCOMPLETE`, or the window is still below K.
- **No cherry-picking** (re-audit `5307485431`). A bundle's evidence is **every** window ever
  declared for it. A later `PASS` window never hides an earlier window.
- **Bundle verdict.** A bundle passes Phase C only when **all** of these hold:
  - at least one of its windows is `PASS`;
  - **none** of its windows is `FAIL`;
  - **no** run in any of its windows has an **effective state** with a blocking cause:
    `UNRESOLVED_MISMATCH`, `PRUNED_BEFORE_RESOLUTION`, `IMAGE_UNMATCHABLE` or `SHADOW_MISSING`.
    The full event history stays in the evidence, and a cause that has since been superseded by a
    resolution no longer blocks;
  - every window that is `INCOMPLETE` only because of `SHADOW_MISSING_AFTER_RECOVERY` has a
    recorded supersession (§11.2);
  - every other window is `PASS`, or is the one open window still below K.
- **What clears a blocking cause.**
  - `UNRESOLVED_MISMATCH` clears only by an appended `RESOLUTION_RECORDED` (§10.3). The run's
    effective state then counts by that resolution, which may make the window `FAIL`. The earlier
    event stays in the history.
  - The other three blocking causes can never clear. They are cleared for the bundle only by a
    **new EPR**, which is a new bundle with new windows. The old bundle's evidence stays recorded.
  - **Exact bundles, no nonce** (clarification closing carry-forward `5818794101` item 1; P3
    authorization `5824551569` item 9). An EPR is content-addressed (§3, §5.2): lineage, origin,
    author and change note lie outside its content, and the strict schema admits no key a nonce
    could hide in. A content-identical EPR is therefore the **same** digest, the same bundle and
    the same evidence, whoever saves it and however often.
    - `PRUNED_BEFORE_RESOLUTION`, `IMAGE_UNMATCHABLE` and `SHADOW_MISSING` are permanent blocking
      evidence for that exact EPR digest and bundle. No same-bundle retry, re-save, re-issue under a
      nonce or counter, or new window clears them, and a bundle holding one — or a `FAIL` window — is
      given no further window.
    - "A new EPR" means a **genuinely content-different** EPR, with a different content digest.
      Only such an EPR starts fresh bundle evidence. No semantic nonce or counter may be introduced
      to manufacture one.
    - `SHADOW_MISSING_AFTER_RECOVERY` remains the one same-bundle supersession (§11.2), and
      `UNRESOLVED_MISMATCH` clears only by `RESOLUTION_RECORDED`.
- **Fixing a bundle.** A `FAIL` disqualifies the bundle, and a fix is a new EPR with new windows.
  Every window of the bundle — `INCOMPLETE` and superseded ones included — is listed in the evidence
  by `collection_run_id`, with each run's effective state, its cause and its full event
  history.
- **Phase C gates.** Phase C may not start until all of the following are merged and accepted, with
  their negative controls:
  - **§10.4 image matching**, including `UNMATCHABLE` and its `INCOMPLETE` counting;
  - **§11.2 crash handling**, including a test that kills the process between the canonical commit
    and the shadow record and then proves the run is in the denominator as `INCOMPLETE`;
  - the §10.1 separate-unit boundary, with a test that a failing shadow write leaves the canonical
    revision committed;
  - the §10.5 retention bounds and event ledger, with tests of four rules:
    - pruning an unresolved run appends `RAW_PRUNED_UNRESOLVED`;
    - pruning a terminal run appends nothing;
    - a resolution after that prune is refused;
    - the denominator counts a run with several events once;
  - the §11.1 window close, with a test that a window holding an `UNRESOLVED_MISMATCH` cannot
    close;
  - the §10.1 frozen per-run decision, with a test that a switch change between the reservation and
    the shadow step changes neither that run's shadow execution nor its eligibility;
  - the §11.3 bundle verdict, with tests of two cases. (1) A bundle with a later `PASS` window and an
    earlier window holding a blocking cause does **not** pass. (2) A window superseded only for
    `SHADOW_MISSING_AFTER_RECOVERY` stays in the evidence.

**Honest limit.** The accepted KM통상 product states no options and no tiers (`docs/acceptance/M3.md`
§2.1). Positive options and tiers are therefore proven only on synthetic fixtures.

### 12. Second supplier (Phase D, deferred)

Nothing here runs until the first vertical closes, or until a canonical amendment explicitly
authorizes a bounded proof (Q6).

**Preconditions.**
- a KRW supplier (`ROADMAP.md` §14.3);
- its CONNECT definition and access envelope, reviewed after a bounded reconnaissance on the
  ADR-0010 §5 pattern, with the user's go-ahead;
- server-rendered product HTML over HTTP.

The proof runs **validation-only** first. It runs as a canonical collection only under the cutover
ADR.

**Criteria, fixed before the run:**

| # | criterion |
| --- | --- |
| P1 | the operator verifies 1–2 representative products; the bundle becomes `VALIDATED` |
| P2 | N further products, fixed in advance and chosen for diversity, are extracted automatically and then verified by the operator against the source |
| P3 | **confident-wrong facts = 0**; the CORE `CONFIRMED` rate is reported against a target the architect sets |
| P4 | zero supplier-specific extractor or hook Python: no module under `integrations/suppliers/<key>/collect/` or `hooks/`, and no `app/` change that names the supplier |
| P5 | no core contract change |
| P6 | production AI/OCR calls = 0 |
| P7 | a structural mutation yields `TEMPLATE_UNMATCHED` or `REVIEW_REQUIRED`, never a confident fact |
| P8 | every read inside frozen caps, and a fresh-session repeat with the same verdicts |

### 13. Phase B (Q5)

Before production implementation, Phase B may exist **only** as an isolated, disposable,
fixture-only prototype, and only after its own go-ahead:
- a top-level `prototypes/adaptive_collector/` on its own draft PR, **not merged to main**;
- synthetic fixtures only;
- it imports at most the pure `app.collect.facts` value models, and nothing imports it;
- no DB, schema, migration or runtime wiring;
- no acceptance claim.

Production code is written fresh after this ADR is merged. It is never made by promoting the
prototype in place.

### 14. The cross-audit items, closed

| # | Issue #110 `5812200650` item | closed by | Phase C gate |
| --- | --- | --- | --- |
| 1 | crash after the canonical commit: missing shadow vs the denominator | §11.2: eligible, in the denominator, `INCOMPLETE` (`SHADOW_MISSING_AFTER_RECOVERY`); the window cannot pass; the startup reconciliation appends its `OUTCOME_RECORDED` event | **yes** (§11.3) |
| 2 | canonical commit before the shadow; the shadow in a separate transaction | §10.1: separate write unit opened after the canonical unit exits; never nested (the `Database.write` poison rule) | with §11.3 |
| 3 | image comparison with no stable locator | §10.4: in-memory M1/M2 matching; a persisted locator is never used; `UNMATCHABLE` is never a success and counts `INCOMPLETE`; nothing URL-derived is persisted | **yes** (§11.3) |
| 4 | G6 counts `(hook_point, target)` bindings | §6.4 G6 | — |
| 5 | V2 covers CORE `IMAGES_FIELD` | §7.2 V2 (image-role rules plus an image region in every PTR, plus expected image references per sample) | — |
| 6 | non-CORE terminology is `COVERAGE` | §4, including the explicit amendment of ADR-0010 §7's historical "Source coverage" label | — |

### 15. What this ADR does not decide

> **Amendment note (ADR-0019).**
> - Extending the gateway or the acquisition transport is decided by ADR-0019.
> - The invariants AC-01 to AC-29 below are neither deleted nor renumbered. Any addition starts at
>   AC-30.

- the cutover to `ACTIVE`: the canonical write path, the canonical unmatched-template behaviour (Q3
  is recorded in §8.1 as its input), and when a code extractor is retired;
- any widening of the gateway, transport, `EvidenceKind` or browser rendering;
- a second currency (`ROADMAP.md` §14.3);
- the exact table and column names of the profile, validation and shadow owners;
- the onboarding UI.

## Rulings carried from the Proposal (PR #111)

| # | ruling | where |
| --- | --- | --- |
| Q1 | `ExtractionProfileRevision` / `PageTemplateRevision`; CONNECT `SupplierProfile` and COLLECT `CollectionProfile` stay distinct; GLOSSARY entries | §3, §4, `docs/GLOSSARY.md` |
| Q2 | `ValidationSample` and shadow records have separate retention rules | §7.3, §10.5 |
| Q3 | the canonical unmatched-template behaviour, for the cutover ADR | §8.1 |
| Q4 | no implementation fingerprint in the semantic identity; semantic changes are mechanically forced | §5.2, §5.6 |
| Q5 | Phase B only as an isolated, disposable, fixture-only prototype | §13 |
| Q6 | Phase D deferred under `CLAUDE.md` §12 | §12 |

## Invariants

```text
AC-01  The Adaptive Collector implements only the parser seam; gateway, session, pacing, budget, image fetch, asset store, revision store, job and audit owners are unchanged
AC-02  The access envelope (hosts, path form, safe query keys, limits, pacing, transport) is never profile data and is never widened at run time
AC-03  EPR and PTR revisions are immutable and content-addressed; lifecycle is a separate append-only log and VALIDATED is derived, never stored
AC-04  A profile never states a source fact, a status, an evidence kind, a new field or a value shape
AC-05  comparability_key decides drift comparability: ("CODE", extractor_revision) for rows without profile provenance, ("ADAPTIVE", extractor_revision, profile_schema_version, extraction_profile_digest) otherwise
AC-06  No existing ProductFactsRevision is backfilled, updated or rewritten; new provenance columns are nullable
AC-07  No implementation fingerprint of the engine or of any hook enters the semantic tuple, directly or through the EPR digest; an implementation-only change lapses VALIDATED and is never by itself EXTRACTOR_CHANGED
AC-08  extraction_semantics_id is a collision-resistant stored digest, never the comparability decision by itself; a mismatch with its recomputation makes the row non-comparable and is never repaired in place
AC-09  Hook points are closed; a hook emits no status and no evidence; the EPR binds HOOK_REVISION only
AC-10  promotion_key = (hook_point, target); G6 caps an EPR at two distinct (hook_point, target) bindings before an architecture review
AC-11  A ValidationSample is cut by the independent capture owner and an operator-approved scope, never by the EPR or PTR it validates; it keeps sanitized product controls and admissible embedded data as parsed literals, strips executable code, private and secret material, and excludes non-authoritative regions
AC-12  V2 covers CORE IMAGES_FIELD through image-role rules, an image region in every PTR and expected image references per sample
AC-13  An ACTIVE switch writes only a lifecycle transition; the current source revision moves only through a later recorded eligible revision
AC-14  Template matching requires exactly one template; no closest match; ambiguity fails closed
AC-15  Production collection and the shadow make zero AI, OCR and vision calls; AI never authors or verifies expected facts
AC-16  The shadow makes zero supplier requests and writes nothing canonical; its record is written in its own write unit only after the canonical unit has committed, never nested
AC-17  Shadow image matching is in memory on the exact resolved or written reference; a persisted locator is never used; UNMATCHABLE is never a success
AC-18  The Phase C denominator is every eligible collection, derived from canonical runs and their frozen per-run shadow decision; a missing shadow record, a crash included, counts INCOMPLETE and is never excluded
AC-19  A window passes only if every eligible collection succeeds; any FAIL disqualifies the bundle; a bundle's evidence is every window ever declared for it
AC-20  Raw shadow records are bounded by 90 days and 5000 per supplier with no hold exception; window outcomes survive only as the event ledger; ValidationSample retention is separate
AC-21  FieldStatus, EvidenceKind, ReviewKind and FIELD_REGISTRY are unchanged; non-CORE fields are COVERAGE
AC-22  Phase C may not start before sections 10.4 and 11.2 are implemented with their negative controls; Phase D stays deferred
AC-23  shadow_enabled_for_run is frozen at a run's first product-read reservation, and both the shadow step and the denominator read only that value
AC-24  A shadow record is keyed by collection_run_id; revision_id is nullable and absent for a NO_REVISION run
AC-25  A ValidationRun that includes a SAMPLE_TRUNCATED sample ends INCOMPLETE, never PASS; a digest is never proof material
AC-26  A blocking INCOMPLETE cause (UNRESOLVED_MISMATCH, PRUNED_BEFORE_RESOLUTION, IMAGE_UNMATCHABLE, SHADOW_MISSING) blocks the bundle until resolved or replaced by a new EPR; only SHADOW_MISSING_AFTER_RECOVERY permits a recorded supersession, and no window is ever abandoned
AC-27  The window ledger is an append-only event stream per collection_run_id; one effective state per run is derived from it; the denominator counts each run once; RAW_PRUNED_UNRESOLVED is appended only while the effective state is UNRESOLVED_MISMATCH
AC-28  A window final-closes only when every run's effective state is terminal; a closeout is never revised, versioned or mutated
AC-29  Blocking evidence is bound to the exact content-addressed EPR: a content-identical EPR is the same bundle, so no re-save, nonce or counter clears PRUNED_BEFORE_RESOLUTION, IMAGE_UNMATCHABLE or SHADOW_MISSING; only a content-different EPR starts fresh evidence (carry-forward 5818794101)
```

## Consequences

- A supplier's interpretation can change without a code change. Such a change is still an
  explicit, digested, validated identity change, never a silent drift.
- Every revision that exists today keeps its meaning and its comparability, with no migration of
  its rows.
- Implementation refactors do not break drift comparability, but they do force revalidation.
- Missing shadow evidence can never make Phase C look better than it is.
- The adapter layer cannot grow silently. Its growth is counted, capped and pushed toward generic
  rules.
- The cost is new owners — profile, validation, samples, shadow — each needing its own slice, schema
  and acceptance. Until the cutover ADR, the canonical extractor stays the only revision writer.

## References

- Issue #110; kickoff `5811580104`; items `5812200650`; ADR authorization `5812422770`
- PR #111 and its reviews `5302725919`, `5302852218`, `5302910552`, `5302952567`; PR #112 audit `5307128101` and re-audits `5307485431`, `5307562621`;
  `docs/review/ADAPTIVE-COLLECTOR-PROPOSAL-BY-CLAUDE.md`
- ADR-0007, ADR-0010 §3–§12, ADR-0012 §9, ADR-0013 §3, ADR-0016
- `docs/ARCHITECTURE.md` §4, §5, §11; `ROADMAP.md` §9, §14.3; `docs/acceptance/M3.md` §2;
  `docs/GLOSSARY.md`
- `integrations/suppliers/collection.py`, `integrations/suppliers/extraction.py`,
  `app/collect/collection.py`, `app/collect/facts.py`, `app/db/database.py`,
  `tests/unit/test_repository_rules.py`
