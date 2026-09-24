# Adaptive Collector — design proposal (Issue #110, Phase A)

Status: **PROPOSAL — awaiting architect audit**
Author: Claude Code
Issue: #110; architect kickoff `5811580104` (DESIGN only)
Base: main `02dd2a35819bdee0d4209b2c0fa1ba9f10156f2a`
Place at: `docs/review/ADAPTIVE-COLLECTOR-PROPOSAL-BY-CLAUDE.md`

This document is not binding. It changes nothing in ADR-0010, ADR-0013, ADR-0016, `docs/ARCHITECTURE.md`
or the code. A decision here becomes binding only when an accepted ADR records it (`CLAUDE.md` §1.1).

---

## 0. What this proposal is, and what it authorizes

It answers the nine design decisions the kickoff requires (§3–§11 below, D1–D9) and proposes how
the later phases are authorized (§12).

It authorizes **nothing**. In particular, while this proposal is under review:

| still forbidden | why |
| --- | --- |
| a change to the canonical COLLECT runtime | Issue #110 constraints; kickoff |
| a shadow write to `ProductFactsRevision` or to any `product_facts_*` table | kickoff; §5 D3 |
| a new `EvidenceKind` or `FieldStatus` value, or an extraction-identity implementation | kickoff; ADR required |
| a real second-supplier read or campaign | `CLAUDE.md` §12 first-vertical restriction |
| a migration | no ADR exists yet |

The fixture/synthetic research that informed this proposal read only repository code and documents
at the base commit. No supplier, marketplace, AI or OCR call was made.

---

## 1. What exists today, and what this design builds on

Every fact below is at the base commit.

- **The parser seam already exists.** `SupplierCollection`
  (`integrations/suppliers/collection.py:376`) is everything a supplier contributes to a
  collection: a `CollectionProfile` (the access envelope: product path form, policy paths, explicit
  image hosts, safe query keys, frozen limits, HTTP only), image-role rules, an identity rule, a
  field parser and a URL pacing hint. The identity rule, field parser and role classifier are pure
  functions over an immutable `DocumentView` (`:117`).
- **Generic COLLECT core owns everything that acts.** `ProductCollectionService.collect`
  (`app/collect/collection.py:463`) reserves the one product read durably, reads one document
  through the policed gateway, resolves identity, parses fields, fetches bounded images and appends
  one revision (`:503`). An unresolved identity returns before the append (`:493`) and the run ends
  `NO_REVISION`.
- **Extraction identity is repository-file based.** `integrations/suppliers/<key>/extraction_identity.py`
  pins `EXTRACTOR_REVISION`, `EXTRACTOR_INPUTS` and `EXTRACTOR_FINGERPRINT` over every module of the
  supplier's `collect/` package (`integrations/suppliers/extraction.py`, ADR-0010 §12). KM통상 is
  `kmretail-1`. `RegisteredCollection` (`app/collect/collection.py:151`) requires the revision to
  equal the image-role rules' identity.
- **The truth vocabulary is closed.** `FieldStatus` (`app/collect/facts.py:56`) is
  `CONFIRMED | ABSENT | REVIEW_REQUIRED`; `EvidenceKind` (`:72`) has eight members; `FIELD_REGISTRY`
  (`:395`) fixes 13 fields, five of them CORE. `evaluate` (`:933`) is the only computation of
  statuses, fingerprints and `facts_status`.
- **Drift comparability keys on `extractor_revision`.** ADR-0013 §3: fingerprints are drift
  evidence only between revisions with the same `extractor_revision`; otherwise the pointer move is
  `EXTRACTOR_CHANGED` and no drift is inferred.
- **`ReviewKind` is closed** (ADR-0016 §2) and `SOURCE_CHANGE` is `NOT_WIRED` (§6).
- **The source-truth path cannot import AI.** `test_collect_source_truth_path_imports_no_ai_ocr_or_marketplace_code`
  (`tests/unit/test_repository_rules.py`, roots `app/collect/` and `integrations/suppliers/`).
- **M3 is bounded** (`docs/acceptance/M3.md` §2): positive `CONFIRMED` option axes/configurations
  and quantity-tier values are not accepted. The KM parser records option-looking evidence as
  `REVIEW_REQUIRED` with no value.
- **Name collision.** `SupplierProfile` already exists and is the CONNECT profile
  (`integrations/suppliers/base.py:49`, ADR-0007); `CollectionProfile` is the ADR-0010 access
  envelope. See Q1 (§13).

---

## 2. The idea in one paragraph

The Adaptive Collector is **a second implementation of the parser seam, not a second COLLECT**. A
validated profile bundle is interpreted by one generic engine, and together they answer exactly
the three questions `SupplierCollection` already asks — *which product is this*, *what do its
fields say*, *which of its images are product evidence*. The gateway, session, pacing, budget,
image fetch, asset store, revision store, job and audit stay exactly where ADR-0010 put them.
What changes (later, by ADR) is where a supplier's **interpretation** lives: in immutable, digested
profile data instead of a supplier-specific Python parser.

```text
                      unchanged (ADR-0010 §3–§4, §9, §11)
operator URL → durable collect.product job → policed gateway (1 product read, budget, pacing)
                                                     │  DocumentView (in memory only)
                          ┌──────────────────────────┴──────────────────────────┐
                          ▼                                                     ▼
     canonical extractor (today: kmretail-1)                 Adaptive engine + profile bundle
     identity → fields → image roles                         identity → fields → image roles
                          │                                                     │
                bounded image fetch (canonical candidates only)                 │ no request
                          │                                                     │
                 ProductFactsRevision append                    shadow comparison record
                 (the only revision writer)                     (separate store, §5)
```

### 2.1 Layers and what may change at run time

| layer | holds | lives in | changes at run time |
| --- | --- | --- | --- |
| **Access envelope** | CONNECT `SupplierDefinition` (ADR-0007) and `CollectionProfile`: product path form, policy paths, explicit hosts, safe query keys, limits, `RequestPolicy` | repository, reviewed | **never** (ADR-0010 §9: the allowlist is never widened at run time) |
| **Generic engine** | locator interpreter, generic extractors, normalizers, template matcher | repository code with its own extraction-identity manifest | no |
| **`SupplierProfileRevision`** | supplier-wide interpretation (D1) | canonical DB, immutable rows | a new revision only |
| **`PageTemplateProfile` revision** | one page shape: signature and per-field locators (D1) | canonical DB, immutable rows | a new revision only |
| **Site adapter hooks** | allowlisted pure functions (D6) | repository code in the supplier package, inside its manifest | no |

**What "profile-only onboarding" means.** A new supplier needs its access envelope and CONNECT
definition — reviewed declarations that ADR-0007 and ADR-0010 require anyway — plus profile data.
It needs **no supplier-specific parser or extractor Python and no hook**. A supplier that needs a
hook is reported as *profile + hooks*, never as profile-only.

**Honest v1 limit.** `CollectionProfile` is HTTP-only and `EvidenceKind` has no network/XHR kind. A
supplier whose product facts arrive only through a script-loaded API response, or only after
browser rendering, **cannot** be profile-only in v1. Widening that is a gateway and EvidenceKind
decision for an ADR, and this proposal does not ask for it (default: no new EvidenceKind).

---

## 3. D1 — `SupplierProfileRevision` vs `PageTemplateProfile`: ownership and immutable revisions

### 3.1 Ownership

| owns | `SupplierProfileRevision` (SPR) | `PageTemplateProfile` revision (PTR) |
| --- | --- | --- |
| scope | one supplier, all its product pages | one page shape of that supplier |
| identity rule | **yes** — which source statements declare the product identity and how they must agree (the ADR-0010 §5 "exact rule"); never a name or content hash | no |
| vocabularies | label sets per field (e.g. price labels), money and number formats, sold-out vocabulary, purchase-control anchors | may narrow them for its own shape, never add a field |
| image role rules | supplier-wide role rules | per-template region locators the rules apply to |
| template set | **a closed set of PTR digests it pins**; no precedence order (matching is exclusive, D5) | no |
| signature | no | required anchors, forbidden anchors (D5) |
| field rules | no | per field: a primary locator rule, optional declared alternatives, expected cardinality |
| hook bindings | hook point → hook name + the supplier hook manifest fingerprint (D6) | no |

**Activation and validation happen on the SPR only.** Because the SPR pins its templates by digest,
the SPR digest covers the whole bundle. Rejected alternative: independently activated templates.
Mixing template revisions freely would run combinations no validation ever saw.

A PTR is content-addressed and may be pinned by several SPRs of the same supplier. A PTR is never
shared across suppliers: the thing that repeats across suppliers is a generic extractor or a hook
promotion (D6), not a copied template.

### 3.2 What a profile may never contain

The profile schema is strict (`extra="forbid"`, the `FactValue` precedent); an unknown key is
refused at DRAFT save. A profile never contains:

- a host, a URL to fetch, a query key, a limit, a pacing value, a transport choice or a credential
  (all of that is the access envelope);
- code, an import path other than a D6 hook binding, or an expression language;
- an unbounded pattern: text patterns are allowed only over one already-located text value, with
  a bounded, backtracking-safe grammar (the engine owns the grammar; the exact choice is Phase B
  work and adds no dependency without the user's approval, `CLAUDE.md` §4);
- a new field key, a status, an evidence kind or a value shape — those come from `FIELD_REGISTRY`,
  `FieldStatus` and `EvidenceKind` unchanged;
- any value presented as a source fact (a default price, a default brand). A profile says **where**
  the source states a fact, never **what** it is.

### 3.3 Immutable revision model

- **Content-addressed.** `digest = SHA-256` over a canonical serialization
  (proposed scheme name `icbm-profile/v1`, the same canonical-JSON rules as `canonical_json` in
  `app/collect/facts.py`), including `profile_schema_version`.
- **Immutable rows.** A profile revision row is never updated or deleted; the database enforces it
  as it does for `audit_events` and the facts tables. A revision referenced by a
  `ProductFactsRevision` must remain loadable forever (FK, no delete).
- **Digest recomputed on load.** The engine recomputes every loaded digest; a stored digest that
  does not match its content refuses the bundle (fail closed, no collection).
- **Lifecycle is not content.** State lives in a separate append-only transition log (who, when,
  from, to, reason, correlation), never in the revision row. `VALIDATED` is derived (D7), not stored.
- **Lineage.** `parent_revision_id`, `origin` (`OPERATOR` | `AI_PROPOSAL` | `IMPORT`), `created_at`,
  a change note.
- **Scoped by `supplier_key`.** A profile may exist only for a supplier whose access envelope and
  CONNECT definition are registered in the build.

---

## 4. D2 — Extraction identity with profile revision, digest and schema provenance

### 4.1 What a revision produced by the Adaptive path would persist

| field | meaning | for a code extractor (KM today) |
| --- | --- | --- |
| `extractor_revision` | **the engine's** semantic revision, e.g. `adaptive-engine-1`; keeps the ADR-0010 meaning "semantic identity of code" | unchanged (`kmretail-1`) |
| `extractor_fingerprint` | the engine manifest fingerprint (ADR-0010 §12 mechanism, a generic package) | unchanged |
| `supplier_profile_revision_id` | the SPR used | `NULL` |
| `supplier_profile_digest` | its digest; covers every pinned PTR and every hook-manifest fingerprint bound | `NULL` |
| `profile_schema_version` | the profile schema the SPR was parsed under | `NULL` |
| `page_template_revision_id` | the PTR this document matched (provenance; see §4.3) | `NULL` |
| `extraction_semantics_id` | **the one identity drift comparability keys on** (§4.2) | derived, non-null |

Exact column names and schema are the ADR's decision; the table fixes the semantics.

### 4.2 `extraction_semantics_id`

```text
extraction_semantics_id = SHA-256( "icbm-extraction-semantics/v1" 0x00
                                   extractor_revision 0x00
                                   profile_schema_version-or-empty 0x00
                                   supplier_profile_digest-or-empty )
```

- **Profile changes never masquerade as source drift.** Any profile edit is a new SPR digest, so a
  new `extraction_semantics_id`, so the ADR-0013 pointer move is `EXTRACTOR_CHANGED` and no drift is
  inferred. No new move reason is needed.
- **Engine semantic change.** The engine's golden guard (§4.4) forces `extractor_revision` to
  advance; every profile's semantics id changes with it, and every VALIDATED status lapses (D7).
- **Implementation fingerprint is excluded**, matching the ADR-0010 split: a refactor that keeps
  every golden output keeps comparability. (Q4 asks the architect to confirm.)
- **Code extractors are unchanged in behaviour.** For KM the id is a pure function of
  `extractor_revision`, so it is equal exactly when `extractor_revision` is equal. Existing rows can
  be backfilled deterministically; no historical value is rewritten.

**Required amendment:** ADR-0013 §3 compares on `extraction_semantics_id` instead of
`extractor_revision`. For every row that exists today the two rules give identical answers.

### 4.3 The matched template is provenance, not semantics

One bundle may match template A on one capture and template B on the next. That is a real change in
the source's shape; the values extracted under one bundle remain comparable, so value drift is
still evaluated. The template switch itself is recorded as a structural observation (D5).

### 4.4 Guards that replace "reproducible from the repository"

ADR-0010 §12 promises reproduction from the repository. With data-backed interpretation the
promise becomes: **reproducible from the repository at the engine fingerprint plus the stored
immutable profile rows the revision names.** The guards:

1. **Engine pin** — the existing manifest mechanism over the engine package.
2. **Engine goldens** — synthetic fixtures with expected outputs per engine `extractor_revision`,
   so an engine semantic change without a revision advance fails.
3. **Digest recomputation** on every load (§3.3).
4. **Hook manifest check** — the fingerprint an SPR binds must equal the running supplier manifest,
   or the bundle is refused.
5. **Clean-tree campaigns** — unchanged (ADR-0010 §12 guard 3).

---

## 5. D3 — One-fetch shadow comparison and a separate shadow result store

### 5.1 Where the shadow runs

Inside `ProductCollectionService.collect`, in the same call, **after** the canonical revision is
appended, over the same in-memory `DocumentView` and the stored revision's image references. It
also runs on the unresolved-identity return (`:493`), where the canonical side is
`UNRESOLVED(reason)`. It does not run on an attempt that recovered an already-appended revision,
because that attempt read no document.

Running after the append means the canonical outcome is decided before the shadow starts and the
shadow record can reference the `revision_id` one way.

### 5.2 Shadow invariants

| # | invariant | how it is made structural |
| --- | --- | --- |
| S1 | **zero additional supplier requests** | the shadow runner receives a `DocumentView`, the source URL and the stored image references — no gateway, session, budget or egress handle; a differential test proves ledger reservations are equal with the shadow on and off |
| S2 | **canonical invariance** | with the shadow on vs off: identical run outcome, revision rows, fingerprints, job state, canonical audit events, ReviewItems and current source revision pointer (differential test) |
| S3 | **failure isolation** | every shadow exception becomes a `SHADOW_FAILED` record (or a log line if the shadow store write itself fails) and never reaches the run; the engine is step-bounded, so a pathological document cannot stall the run |
| S4 | **no canonical write** | the shadow package may not import the revision store, source-asset recorder, run store, review owner or pointer owner (repository rule) |
| S5 | **no body persistence** | the shadow store keeps comparison records only; `body` stays in memory (ADR-0010 §3) |
| S6 | **no AI** | the engine, profile runtime and shadow packages sit under the source-truth import rule |
| S7 | **off by default** | enabled per supplier by explicit configuration; disabled means zero engine calls |

### 5.3 Images in the shadow

The Adaptive engine proposes image candidates but **never fetches** (S1). Each adaptive candidate
is matched to the canonical run's references by the normalized reference identity (scheme + host +
path, the `ImageCandidate.identity` rule), and inherits the canonical SHA-256.

- adaptive-only candidate → `UNOBSERVED` (never fetched, no checksum claimed);
- canonical-only reference → `MISSED`.

**A SHA-256 equality in a shadow record is inherited from the canonical fetch, not independently
observed**, and the record says so.

### 5.4 The shadow store

A separate owner (proposed `app/collect/shadow/`) with its own tables, keyed by
`collection_run_id` and referencing `revision_id` one way. Nothing canonical references it, and no
canonical read joins it. It holds the D4 verdicts, the adaptive normalized values of mismatched
fields (local only, like the facts tables themselves), the identity of the bundle and engine used,
and the observations of D4. Committed evidence drawn from it follows ADR-0010 §6: counts and
statuses, keyed fingerprints, no plain digests of business values.

Phase B has **no** shadow store: its harness writes a report over fixtures only (§12).
Retention of shadow records is Q2.

---

## 6. D4 — What must match semantically, what may differ, what is only observed

Both sides produce `FieldFact`s and go through the same pure `evaluate` in memory, so statuses and
`facts_status` are computed by one owner.

| must match (a difference is a mismatch) | may legitimately differ (never a mismatch) | observed only |
| --- | --- | --- |
| identity: resolved vs unresolved; `source_product_id` | evidence `locator` | evidence count per field |
| per field: `FieldStatus` | evidence `digest` and `ordinal` | `EvidenceKind` distribution per field |
| per field: normalized value, canonical JSON, source order kept where order is a fact (price labels, option axes and values, configurations, notice items) | `field_fingerprint` | declared-alternative (fallback) usage |
| options: the exact ordered atomic configuration set, count/grade/weight distinctions, option-level sold-out evidence | `source_fingerprint` | hook invocations per hook point |
| stock availability (`ON_SALE` / `SOLD_OUT` / `REVIEW_REQUIRED`) | `extractor_revision`, `extractor_fingerprint`, `extraction_semantics_id` | template matched, and a template switch since the last capture |
| images: per role, ordered `(role, ordinal, sha256)` and each reference's status | image `provenance` text | engine steps used |
| `facts_status` (as a consistency check) | | |

### 6.1 Verdicts and severity

- Per field: `MATCH` | `STATUS_MISMATCH` | `VALUE_MISMATCH`. `ABSENT` vs `REVIEW_REQUIRED` is a
  mismatch.
- Per run: `MATCH` | `MISMATCH` | `IDENTITY_MISMATCH` | `TEMPLATE_UNMATCHED` | `TEMPLATE_AMBIGUOUS` |
  `SHADOW_FAILED`.
- Severity of a field mismatch, most severe first:
  1. `CONFIDENT_DISAGREEMENT` — both `CONFIRMED`, values differ;
  2. `ADAPTIVE_OVERCONFIDENT` — adaptive `CONFIRMED`, canonical not;
  3. `ADAPTIVE_CONSERVATIVE` — canonical `CONFIRMED`, adaptive not.

  The inherited M3 boundary applies to the engine as well: until an ADR accepts positive option and
  tier support, an adaptive `CONFIRMED` option or tier value is `ADAPTIVE_OVERCONFIDENT` by
  definition.

### 6.2 Mismatch resolution

The canonical extractor is **not** presumed correct. A human resolves each mismatch from source
evidence (the revision's stored evidence, the adaptive evidence locators) and approved fixtures,
recording one of `CURRENT_CORRECT` | `ADAPTIVE_CORRECT` | `BOTH_WRONG` | `SOURCE_AMBIGUOUS` with the
evidence it relied on.

- A resolution never edits a canonical revision (append-only). If the canonical extractor is wrong,
  that is a separate KM extractor fix under its own authorization, advancing `kmretail-*`.
- A shadow mismatch is **not a `ReviewItem`**. It is engineering evidence about an extractor, not a
  review condition of product truth, and `ReviewKind` stays closed.

---

## 7. D5 — Validation owner vs drift-detection owner

Three questions, three owners. This proposal adds the first two and changes nothing in the third.

| question | owner | when | inputs | output |
| --- | --- | --- | --- | --- |
| **Is this profile trustworthy enough to become VALIDATED?** | **profile validation** (proposed `app/collect/profiles/`) | on demand, offline, zero network | one SPR bundle, the engine identity, an approved sample set, the negative-control suite | an immutable `ValidationRun` bound to exact digests (D7) |
| **Does the page still fit the validated template?** | **structural drift** = template conformance, inside the engine | every extraction, shadow or (later) canonical | the document and the bundle | a conformance record; affected fields fail closed |
| **Did the source's values change?** | **source-value drift**, `docs/ARCHITECTURE.md` §11 and ADR-0013 §3 | a current source revision pointer move | two revisions with the same `extraction_semantics_id` | unchanged |

### 7.1 Template matching fails closed

- A template signature is a predicate: every required anchor present and no forbidden anchor
  present. There is no score and no closest match.
- Exactly one template must match. None → `TEMPLATE_UNMATCHED`; more than one →
  `TEMPLATE_AMBIGUOUS`. Neither guesses.
- In the shadow this is a verdict. What the canonical path does on an unmatched template belongs to
  the cutover ADR (Q3). Recommendation: when the SPR identity rule still resolves, append a revision
  whose fields are all `REVIEW_REQUIRED` with a structural reason, because keeping the pointer on the
  last good revision would present an unreadable page as current and unchanged.

### 7.2 Inside a matched template

- A field rule is a primary locator and, optionally, declared alternatives. All are evaluated.
- Two locators that hit with different normalized values → `REVIEW_REQUIRED`. Never pick one.
- Only an alternative hits → the value may stand only if that alternative was proven on the sample
  set; the conformance record carries `ALTERNATIVE_USED`, and it is a structural drift signal.
- An expected cardinality is violated (e.g. one price row expected, three found) →
  `REVIEW_REQUIRED`.
- `ABSENT` only when the rule's own absence condition is observed in the document (for example,
  the notice table is present and has no brand row). A missing anchor is never `ABSENT`.

### 7.3 What the drift owner never does

It never edits a profile, never learns, never proposes a replacement on its own, and never demotes a
profile silently. A profile's health (`HEALTHY` / `DEGRADED`) is derived from recent conformance
records for display. Re-onboarding is a new DRAFT through the normal path. A conformance failure
that leaves a field `REVIEW_REQUIRED` reaches review through the existing COLLECT producer as
`COLLECT_EVIDENCE`; no new `ReviewKind` is needed (whether a new owner reason code needs an ADR-0016
amendment is for the ADR to confirm).

---

## 8. D6 — The allowlisted Site Adapter hook contract and anti-growth guards

### 8.1 Hook points (closed; a new one needs an ADR amendment)

| hook point | input | output | example of a legitimate need |
| --- | --- | --- | --- |
| `identity_decode` | one located, source-stated text | a `source_product_id` candidate, or `CANNOT_PARSE` | an identity the source encodes in a form the generic rules cannot express |
| `value_parse` | a field key and one located observed text | a candidate of that field's `FIELD_REGISTRY` value model, or `CANNOT_PARSE` | a site-specific price or shipping text syntax |
| `option_decode` | a located option-control fragment | an ordered options candidate, or `CANNOT_PARSE` | a site-specific option-control encoding |
| `embedded_decode` | a declared embedded data block | a JSON-like tree for locators to address, or `CANNOT_PARSE` | an embedded format that is neither JSON nor JSON-LD |

Never a hook: fetching, building a URL to request, a host decision, the stock verdict, a
`FieldStatus`, the identity agreement decision, an image's role, or the choice between two
conflicting values. **A hook cannot emit a status or evidence.** The engine validates its output
strictly against the field's value model, decides the status from its own evidence rules, records
the `EvidenceKind` of the input the hook read and names the hook in provenance. `CANNOT_PARSE` is
`REVIEW_REQUIRED`.

### 8.2 Where hooks live

`integrations/suppliers/<key>/hooks/`, pure, under `test_supplier_packages_hold_site_knowledge_only`,
importing nothing but the value models. The supplier's extraction-identity manifest must cover
`hooks/` as it covers `collect/` today, and the SPR binds each hook with that manifest fingerprint
(D2 §4.4 guard 4).

### 8.3 Anti-growth guards

| # | guard | enforcement |
| --- | --- | --- |
| G1 | no supplier-specific branch in core | AST repository rule: no comparison against a registered `supplier_key` literal, and no `integrations.suppliers.<key>` import, in `app/` or the engine |
| G2 | no unknown profile key | strict schema at DRAFT save |
| G3 | closed hook points | an enum owned by the engine; an unknown binding refuses the bundle |
| G4 | measurable adapter use | hook invocations per supplier × hook point in every conformance and shadow record; the validation report counts bindings; profile-only = zero bindings |
| G5 | promotion review | when a second supplier binds the same hook point for the same purpose, validation records `HOOK_PROMOTION_REVIEW_REQUIRED`; ACTIVE (later) requires a recorded architect decision on promoting it into a generic extractor |
| G6 | per-supplier cap | more than two bound hook points blocks VALIDATED until an architecture review is recorded: the supplier is not fitting the generic model, and that is a design finding, not a hook to add |
| G7 | proven paths only | no VALIDATED unless every bound hook is exercised by an approved sample or fixture |

---

## 9. D7 — DRAFT save vs VALIDATED/ACTIVE promotion

| state | how it is entered | may be used for |
| --- | --- | --- |
| `DRAFT` | a strict schema parse passes; digest computed; lint findings recorded (an unmapped CORE field is allowed in a DRAFT). Costs no network. | offline evaluation against samples only |
| `VALIDATED` | **derived**: a `PASS` `ValidationRun` exists for this exact `(supplier_profile_digest, engine extractor_revision, profile_schema_version, sample-set digest, hook-manifest fingerprints)`. A change to any of them lapses it with no stored flag to forget. | shadow |
| `SHADOW` | a designation: VALIDATED + the per-supplier shadow switch | shadow comparison (D3) |
| `ACTIVE` | **not authorized in this track.** Requires a cutover ADR, Phase C evidence and the user's approval; at most one ACTIVE bundle per supplier; each switch is an append-only transition and an `EXTRACTOR_CHANGED` pointer move | canonical revisions (later) |
| `RETIRED` | explicit transition; never deleted | reading history |

### 9.1 Validation checks (all deterministic, zero network)

- **V1 referential** — every pinned PTR exists and recomputes to its digest; every hook binding
  resolves; manifests are current.
- **V2 coverage** — every CORE field has a rule in every template.
- **V3 sample agreement** — for each approved sample, the engine output equals the sample's
  expected facts on every must-match dimension of D4. At least one sample per template, at least
  two in total. **Expected facts are authored or verified by the operator from the source — never
  by AI and never by the profile under validation**, or validation would be circular.
- **V4 negative controls** — a login page, a non-product page, and a mutation suite generated
  deterministically from each sample (remove a required anchor, duplicate a price row, inject
  hidden sold-out text, add a second conflicting identity) must fail closed exactly as D5 says.
- **V5 determinism** — two evaluations give identical outputs and digests.
- **V6 safety** — the evidence sanitizer and the secret scan pass; observed fragments within
  4 KiB; nothing forbidden by ADR-0010 §8.
- **V7 hook guards** — G5–G7.

Where the approved samples themselves are stored is Q2.

---

## 10. D8 — Production AI = 0, with optional onboarding assistance

- **Production collection and the shadow: AI / OCR / vision calls = 0**, structurally. The engine,
  the profile runtime and the shadow package are source-truth roots under the existing import rule,
  and the campaign hard-zero list covers them.
- **Onboarding is a separate package** (proposed `app/onboarding/`) outside the source-truth roots.
  It may use the ADR-0012 port, with prompts only from the persisted `PromptTemplate` (Issue #30).
- **Deterministic candidates come first.** Generic detectors (label-row tables, JSON-LD, purchase
  and sold-out controls, image regions) propose candidate rules from a sample. AI may only rank
  them or propose a DRAFT. Onboarding works with AI disabled (ADR-0012 §9: a capability, never core
  readiness).
- **What AI never does:** author or verify expected sample facts, save a profile without an
  operator action, transition a state, trigger a read, or appear in any production path. A DRAFT
  from AI carries `origin = AI_PROPOSAL` with the AI profile and prompt revision.
- **Egress.** A sample shown to AI is the sanitized, product-scoped sample (Q2), never cookies,
  member data or page-wide private HTML. Sending it to a cloud provider is data egress under
  ADR-0012 and needs the user's approval; the default is the local capability or none.

---

## 11. D9 — Second-supplier profile-only proof criteria (deferred)

Nothing here runs until the first-vertical restriction is lifted or an architect-approved canonical
amendment authorizes a bounded proof.

**Preconditions.**
- a KRW supplier (`ROADMAP.md` §14.3: a second currency needs its own ADR first);
- its CONNECT definition (ADR-0007) and access envelope, reviewed after a bounded reconnaissance on
  the ADR-0010 §5 pattern, with the user's explicit go-ahead;
- server-rendered product HTML over HTTP (§2.1); otherwise it is not a profile-only candidate.

**Two steps.** Because an adaptive profile would be that supplier's only extractor, a canonical
write needs `ACTIVE`, and `ACTIVE` needs the cutover ADR. The proof therefore runs first as
**validation-only** (no `ProductFactsRevision`), then under that ADR as a canonical collection.

**Criteria (fixed before the run, never loosened during it).**

| # | criterion |
| --- | --- |
| P1 | the operator verifies 1–2 representative products; the profile goes DRAFT → VALIDATED |
| P2 | N further products, fixed in advance and chosen for diversity (options and sold-out where the supplier has them), are extracted automatically; the operator then verifies each against the source |
| P3 | **confident-wrong facts = 0**: no `CONFIRMED` value differs from the verified source truth; each non-`CONFIRMED` field is justified by its evidence; the CORE `CONFIRMED` rate is reported against a target the architect sets |
| P4 | zero supplier-specific Python in the extractor layer: the proof's diff adds no module under `integrations/suppliers/<key>/collect/` or `hooks/`, and no `app/` change names the supplier; hook bindings = 0 for "profile-only" |
| P5 | no core contract change: `FIELD_REGISTRY`, `FieldStatus`, `EvidenceKind`, Product and Pricing contracts untouched |
| P6 | production AI / OCR calls = 0; onboarding AI calls reported separately |
| P7 | drift negative control: a structural mutation of a sample yields `TEMPLATE_UNMATCHED` or `REVIEW_REQUIRED`, never a confident fact |
| P8 | every read inside frozen caps; a fresh-session repeat gives the same verdicts |

**Success metrics** (Issue #110): profile-only onboarding rate; hook bindings per supplier;
supplier-specific Python outside approved adapter paths = 0; drift detected before any incorrect
confident fact is emitted.

---

## 12. Phase plan and authorization boundaries

Each step needs its own authorization in GitHub.

| phase | scope | contract / schema | real reads |
| --- | --- | --- | --- |
| **A** (this) | this proposal | none | 0 |
| **ADR** | records D1, D2, D5, D6, D7 and the ADR-0010 §6/§12 and ADR-0013 §3 amendments | contract only, no implementation in the same PR | 0 |
| **B** prototype | generic engine, strict profile parser/validator, template matcher, the D4 comparison model and an offline harness over synthetic fixtures (KM-shaped synthetic pages included); a report file, no store | **no migration, no canonical wiring**, isolated package | 0 |
| **C** KM shadow | profile and shadow store migrations, the D3 hook in `collect`, a KM profile, the switch; shadow runs only on ordinary operator collections | per the ADR | only ordinary operator collections, with the user's go-ahead; no read exists for the shadow's sake |
| **D** second supplier | D9 | cutover ADR | only under D9 preconditions |

**Phase C exit evidence** (proposed): over K KM collections every verdict is `MATCH` or its mismatch
is resolved with evidence; S1 and S2 differential proofs; zero AI; ledger equality. Honest limit:
the accepted KM product states no options and no tiers (`docs/acceptance/M3.md` §2.1), so positive
options and tiers are proven only on synthetic fixtures, and the engine inherits the M3 boundary.

**Phase B negative controls** (proposed): a profile with an unknown key is refused; a hook binding
outside the enum is refused; a stale digest is refused; two conflicting locators give
`REVIEW_REQUIRED`; zero and two matching templates fail closed; a mutated anchor never yields
`CONFIRMED`; the engine imports no AI, network, DB or `app.collect` writer; the shadow runner has no
reachable gateway; identical input gives identical output.

---

## 13. Questions for the architect

| # | question | recommendation |
| --- | --- | --- |
| Q1 | **Names.** `SupplierProfile` is already the CONNECT profile and `CollectionProfile` the access envelope; `SupplierProfileRevision` would read as a revision of the CONNECT profile. | Name them `ExtractionProfileRevision` and `PageTemplateRevision`, and add all four to `docs/GLOSSARY.md`. This proposal keeps the Issue's working names until you rule. |
| Q2 | **Sample and shadow retention.** Validation replays need the sample documents; storing whole authenticated pages is forbidden (ADR-0010 §8). | Store only a sanitized, product-scoped sample snapshot, locally, never committed, with a bounded retention; the same bound for shadow records. Needs your ruling on the sanitizer's scope rule. |
| Q3 | **Unmatched template on the canonical path** (cutover ADR, not now). | A revision with every field `REVIEW_REQUIRED` and a structural reason when identity still resolves; no revision when it does not (ADR-0010 §11 unchanged). |
| Q4 | **Implementation fingerprint in `extraction_semantics_id`.** | Exclude it (ADR-0010's semantic/implementation split). |
| Q5 | **Is Phase B allowed before the ADR?** It changes no contract, wires nothing and adds no migration. | Allow Phase B after this proposal is accepted; require the ADR merged before Phase C. |
| Q6 | **Phase D sequencing** with respect to `CLAUDE.md` §12. | Keep it deferred until the first vertical closes; revisit only through a canonical amendment. |

---

## 14. What this proposal explicitly does not do

It changes no code, schema, ADR or canonical document; adds no `EvidenceKind`, `FieldStatus`,
`ReviewKind` or field; widens no host, path, budget or transport; makes no supplier, marketplace, AI
or OCR call; does not touch or migrate the KM통상 extractor; and authorizes no second-supplier read.

## References

- Issue #110 and kickoff `5811580104`
- ADR-0007, ADR-0010 §3–§12, ADR-0012 §9 §13 §14, ADR-0013 §3, ADR-0016 §2 §6
- `docs/ARCHITECTURE.md` §4, §5, §11; `ROADMAP.md` §9, §14.3; `docs/acceptance/M3.md` §2
- `integrations/suppliers/collection.py`, `integrations/suppliers/extraction.py`,
  `integrations/suppliers/kmretail/collection.py`, `app/collect/collection.py`, `app/collect/facts.py`,
  `tests/unit/test_repository_rules.py`
