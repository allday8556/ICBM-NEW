# Adaptive Collector — design proposal (Issue #110, Phase A)

Status: **ACCEPTED** — architect PASS `5302952567` on PR #111 exact head `1dbb1335` (revision 4), with
the Claude AI cross-audit PASS, as the input to [ADR-0017](../adr/0017-adaptive-collector-profile-extraction-and-shadow-validation.md).
The binding contract is ADR-0017, which also closes the six cross-audit items of Issue #110
`5812200650`; where this proposal and ADR-0017 differ, ADR-0017 is right. The body below is kept
as the reviewed revision 4 and is not edited further.
Author: Claude Code
Issue: #110; architect kickoff `5811580104` (DESIGN only)
Audit: PR #111 review `5302725919` on `eaa85aa` — direction accepted, four required fixes, rulings Q1–Q6;
re-audit `5302852218` on `63dd1d73` — those fixes applied, three further corrections and one precision fix;
re-audit `5302910552` on `a4ace6f9` — those applied, two remaining corrections
Base: main `02dd2a35819bdee0d4209b2c0fa1ba9f10156f2a`
Place at: `docs/review/ADAPTIVE-COLLECTOR-PROPOSAL-BY-CLAUDE.md`

This document is not binding. It changes nothing in ADR-0010, ADR-0013, ADR-0016, `docs/ARCHITECTURE.md`,
`docs/GLOSSARY.md` or the code. A decision here becomes binding only when an accepted ADR records it
(`CLAUDE.md` §1.1). Where this revision records an architect ruling, it records it as the input the
ADR must carry, not as a decision in force.

### Revision 2 — what changed after review `5302725919`

| review item | where | change |
| --- | --- | --- |
| Q1 names | throughout, §13 | `ExtractionProfileRevision` (EPR) and `PageTemplateRevision` (PTR); draft GLOSSARY entries (§13.1) land with the ADR |
| D2 fix | §4.2.1 | **no backfill or update of any existing `ProductFactsRevision`**; legacy comparability is a read-time derivation from `extractor_revision` |
| D3 fix | §5.5 | Phase C denominator = every eligible canonical collection with the shadow enabled; a missing shadow outcome is `INCOMPLETE`, a failed one `FAIL`, never omitted |
| D6 fix | §8.3, §8.4 | G5 triggers on a machine-derived promotion key `(hook_point, target)`, grouped by a closed engine-owned `format_class`, not on "same purpose" |
| D7 fix / Q2 | §9.2, §5.4 | `ValidationSample` retention separated from shadow retention |
| Q3, Q4 | §7.1, §4.2 | recorded as accepted for the cutover ADR / the identity ADR |
| Q5 | §12 | Phase B only as an isolated, disposable, fixture-only prototype outside production packages; not started |
| Q6 | §11, §12 | Phase D stays deferred |

### Revision 3 — what changed after re-audit `5302852218`

| re-audit item | where | change |
| --- | --- | --- |
| 1. hook implementation fingerprint leaked into semantic identity | §3.1, §4.1, §4.2, §4.4, §8.2, §9 | hooks get a **semantic `HOOK_REVISION`** in their own manifest; the EPR binds only that revision, never the hook fingerprint; the fingerprint is runtime provenance and a `ValidationRun` freshness input only; hook goldens force the revision to advance |
| 2. source-value drift compared stored `extraction_semantics_id` | §7, §4.1, §13.1 | every drift-comparability statement now names `comparability_key` (§4.2.1), the one owner of the amended ADR-0013 §3 rule |
| 3. `ValidationSample` scope defined by the bundle under validation | §9.2, §9.1 | the snapshot is cut by an **independent capture owner** — a versioned sanitizer plus an operator-approved product scope recorded at capture — never by an EPR/PTR; plus a conflict scan over the whole snapshot |
| precision: SHA-256 is not injective | §4.2, §4.2.1 | comparability compares a **tagged semantic tuple**; SHA-256 over a domain-separated, length-prefixed encoding is only its storage form and is described as collision-resistant, never collision-free |

### Revision 4 — what changed after re-audit `5302910552`

| re-audit item | where | change |
| --- | --- | --- |
| 1. the capture sanitizer blanket-removed forms and inputs | §9.2, §9.1 V3a | the sanitizer **keeps** sanitized product-scoped control structure and state (purchase and cart buttons, sold-out controls, option selectors, non-secret attributes and state) and strips only credentials, auth/session material, member/account fields, cart/account submission payloads and user-entered or private values; V3a scans the kept controls |
| 2. an ACTIVE switch was described as a pointer move | §9 D7, §4.2 | an ACTIVE switch writes **only** the profile lifecycle transition; the current source revision pointer moves only when a later canonical collection records a new eligible revision under a different `comparability_key`, and that move is the `EXTRACTOR_CHANGED` one; no new revision, no pointer move |

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
| a Phase B prototype | Q5 allows only an isolated, disposable, fixture-only one, and only after its own go-ahead (§12) |
| any backfill or update of an existing `ProductFactsRevision` | review `5302725919` D2; §4.2.1 |

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
  envelope. Q1 therefore names the new concepts `ExtractionProfileRevision` and
  `PageTemplateRevision` (§13).

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
| **`ExtractionProfileRevision`** | supplier-wide interpretation (D1) | canonical DB, immutable rows | a new revision only |
| **`PageTemplateRevision`** | one page shape: signature and per-field locators (D1) | canonical DB, immutable rows | a new revision only |
| **Site adapter hooks** | allowlisted pure functions (D6) | repository code in the supplier package, under its own hook manifest (§8.2) | no |

**What "profile-only onboarding" means.** A new supplier needs its access envelope and CONNECT
definition — reviewed declarations that ADR-0007 and ADR-0010 require anyway — plus profile data.
It needs **no supplier-specific parser or extractor Python and no hook**. A supplier that needs a
hook is reported as *profile + hooks*, never as profile-only.

**Honest v1 limit.** `CollectionProfile` is HTTP-only and `EvidenceKind` has no network/XHR kind. A
supplier whose product facts arrive only through a script-loaded API response, or only after
browser rendering, **cannot** be profile-only in v1. Widening that is a gateway and EvidenceKind
decision for an ADR, and this proposal does not ask for it (default: no new EvidenceKind).

---

## 3. D1 — `ExtractionProfileRevision` vs `PageTemplateRevision`: ownership and immutable revisions

### 3.1 Ownership

| owns | `ExtractionProfileRevision` (EPR) | `PageTemplateRevision` (PTR) |
| --- | --- | --- |
| scope | one supplier, all its product pages | one page shape of that supplier |
| identity rule | **yes** — which source statements declare the product identity and how they must agree (the ADR-0010 §5 "exact rule"); never a name or content hash | no |
| vocabularies | label sets per field (e.g. price labels), money and number formats, sold-out vocabulary, purchase-control anchors | may narrow them for its own shape, never add a field |
| image role rules | supplier-wide role rules | per-template region locators the rules apply to |
| template set | **a closed set of PTR digests it pins**; no precedence order (matching is exclusive, D5) | no |
| signature | no | required anchors, forbidden anchors (D5) |
| field rules | no | per field: a primary locator rule, optional declared alternatives, expected cardinality |
| hook bindings | hook point, target, format class → hook name + the supplier's **semantic** `HOOK_REVISION` (D6, §8.2); never the hook implementation fingerprint | no |

**Activation and validation happen on the EPR only.** Because the EPR pins its templates by digest,
the EPR digest covers the whole bundle. Rejected alternative: independently activated templates.
Mixing template revisions freely would run combinations no validation ever saw.

A PTR is content-addressed and may be pinned by several EPRs of the same supplier. A PTR is never
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
| `extractor_fingerprint` | the engine manifest fingerprint (ADR-0010 §12 mechanism, a generic package) — implementation provenance | unchanged |
| `extraction_profile_revision_id` | the EPR used | `NULL` |
| `extraction_profile_digest` | its digest; covers every pinned PTR and every bound hook's **semantic** `HOOK_REVISION` — no implementation fingerprint | `NULL` |
| `hook_fingerprint` | the supplier hook manifest's implementation fingerprint at extraction time (`NULL` when no hook is bound) — implementation provenance, **outside every semantic identity** | `NULL` |
| `profile_schema_version` | the profile schema the EPR was parsed under | `NULL` |
| `page_template_revision_id` | the PTR this document matched (provenance; see §4.3) | `NULL` |
| `extraction_semantics_id` | the stored form of a new Adaptive row's semantic tuple (§4.2); persisted only on new rows written after the ADR and its schema are authorized. Drift comparability is decided by `comparability_key` (§4.2.1), never by this column alone | **not stored on any existing row**; its `comparability_key` is derived on read from `extractor_revision` (§4.2.1) |

Exact column names and schema are the ADR's decision; the table fixes the semantics.

### 4.2 The semantic tuple and `extraction_semantics_id`

The semantic identity of an Adaptive extraction is a **tagged tuple** of semantic parts only:

```text
adaptive_semantics = ("ADAPTIVE",
                      extractor_revision,           # engine semantic revision
                      profile_schema_version,
                      extraction_profile_digest)    # covers PTR digests and hook HOOK_REVISIONs,
                                                    # no implementation fingerprint of any kind

extraction_semantics_id = SHA-256( "icbm-extraction-semantics/v1" ‖ LP(tag) ‖ LP(extractor_revision)
                                   ‖ LP(profile_schema_version) ‖ LP(extraction_profile_digest) )
                          # LP(x) = 4-byte big-endian UTF-8 length of x, then x
```

`extraction_semantics_id` is only the **stored form** of the tuple: SHA-256 over a
domain-separated, length-prefixed encoding. It is collision-resistant, not collision-free, and
nothing in this design relies on it being injective. Comparability compares tuples (§4.2.1).

- **Profile changes never masquerade as source drift.** Any semantic profile edit is a new EPR
  digest, so a new tuple. A profile edit or activation moves no pointer by itself (§9); when a
  later canonical collection records a revision under the new tuple and that revision becomes
  current, that pointer move (ADR-0013 §3) is recorded as `EXTRACTOR_CHANGED` and no drift is
  inferred. No new move reason is needed.
- **Engine semantic change.** The engine's golden guard (§4.4) forces `extractor_revision` to
  advance; every profile's tuple changes with it, and every VALIDATED status lapses (D7).
- **No implementation fingerprint in the tuple** (Q4, accepted in review `5302725919`, and re-audit
  `5302852218` item 1): neither `extractor_fingerprint` nor any hook implementation fingerprint
  enters the tuple, directly or through the EPR digest. An implementation-only change — of the
  engine or of a hook — keeps comparability; it may lapse VALIDATED and force revalidation (§9),
  but it is **never by itself `EXTRACTOR_CHANGED`**.
- **Semantic changes are mechanically forced.** Because no fingerprint is in the tuple, a behaviour
  change must advance a semantic revision that is: the engine goldens force `extractor_revision`,
  and the hook goldens force `HOOK_REVISION`, which forces a new EPR (§4.4 guards 2 and 4).

### 4.2.1 Legacy rows: a read-time compatibility rule, never a backfill (review `5302725919` D2)

**No existing `ProductFactsRevision` row is backfilled, updated or rewritten** — not to add a
column value, not to "normalize" provenance. Revisions are append-only source truth (ADR-0010 §6),
and a migration that touched them would be exactly the historical mutation that rule forbids.

Comparability is instead decided by one pure function over what a row already carries, and it
returns a **tagged tuple**, not a hash:

```text
comparability_key(row) =
    ("ADAPTIVE", row.extractor_revision, row.profile_schema_version, row.extraction_profile_digest)
                                            when the row carries profile provenance (new Adaptive rows)
    ("CODE", row.extractor_revision)        otherwise (every row that exists today)
```

Two revisions are drift-comparable exactly when their `comparability_key` tuples are equal.

- **Deterministic and read-only.** It is computed on read by the comparison owner (the ADR-0013
  pointer-move logic). Nothing is stored for a legacy row.
- **Identical answers for every existing row.** For a row without profile provenance the key is
  `("CODE", extractor_revision)`, so two legacy rows are equal exactly when their
  `extractor_revision` strings are equal — the ADR-0013 §3 rule today. No hash is involved.
- **The two populations are separated by the tag.** A `"CODE"` tuple and an `"ADAPTIVE"` tuple are
  never equal because their tags and arities differ; this is a structural property of the
  comparison, not a claim about hash collisions.
- **New columns are nullable.** Schema that adds the §4.1 provenance adds nullable columns and
  never a default that the database would materialize into old rows. A code-extractor row written
  after the ADR may leave them `NULL`; the rule above then gives the same answer it gives today.
- **Integrity check, not repair.** Where a new row stores `extraction_semantics_id`, a read-time
  check recomputes it from the row's own semantic tuple; a mismatch makes that row non-comparable
  (fail closed) and is reported. The row is never corrected in place. The stored id is an integrity
  and indexing aid; equality of stored ids alone never decides comparability.
- **Only after authorization.** New Adaptive revisions persist the §4.1 provenance only after the
  ADR and its schema are authorized. Nothing in Phase B stores any of it.

**Required amendment:** ADR-0013 §3 compares on `comparability_key` instead of on
`extractor_revision`. For every row that exists today the two rules give identical answers.

### 4.3 The matched template is provenance, not semantics

One bundle may match template A on one capture and template B on the next. That is a real change in
the source's shape; the values extracted under one bundle remain comparable, so value drift is
still evaluated. The template switch itself is recorded as a structural observation (D5).

### 4.4 Guards that replace "reproducible from the repository"

ADR-0010 §12 promises reproduction from the repository. With data-backed interpretation the
promise becomes: **reproducible from the repository at the engine and hook fingerprints the
revision records, plus the stored immutable profile rows it names.** The fingerprints serve
reproduction and validation freshness; they are not semantic identity (§4.2). The guards:

1. **Engine and hook pins** — the existing manifest mechanism over the engine package, and a
   separate hook manifest per supplier (§8.2).
2. **Engine goldens** — synthetic fixtures with expected outputs per engine `extractor_revision`,
   so an engine semantic change without a revision advance fails the build. This is what makes the
   Q4 exclusion safe: the fingerprint is not in the identity, but a semantic change cannot land
   without advancing the revision that is.
3. **Digest recomputation** on every load (§3.3).
4. **Hook goldens and the hook revision check.** Every bound hook has synthetic golden cases with
   expected outputs recorded under its supplier's `HOOK_REVISION`; a behaviour change without a
   revision advance fails the build, as the engine goldens do. At load, the `HOOK_REVISION` an EPR
   binds must equal the running hook manifest's, or the bundle is refused. A semantic hook change
   therefore forces a new EPR and a new tuple, while an implementation-only hook change (same
   goldens, same `HOOK_REVISION`, new fingerprint) keeps the tuple and only lapses VALIDATED (§9).
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
| S7 | **off by default** | enabled per supplier by explicit configuration whose changes are an append-only, timestamped history (§5.5 needs it); disabled means zero engine calls |

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

**Shadow retention is its own rule, not the sample rule** (Q2 as modified in review
`5302725919`). Shadow records are non-canonical engineering evidence, so they are bounded by an
explicit **age bound and count bound** (for example: at most N records per supplier and none older
than D days, whichever is reached first). The ADR fixes both values **before Phase C**; Phase C
cannot start with either left open, and there is no code default (the ADR-0010 §4 no-default rule).
Pruning a shadow record never touches canonical rows, and it never prunes a record that an open
Phase C evidence window still counts (§5.5). `ValidationSample` retention is separate (§9.2).

Phase B has **no** shadow store: its harness writes a report over synthetic fixtures only (§12).

### 5.5 Phase C evidence: the denominator is every eligible collection (review `5302725919` D3)

A shadow result that is missing must never shrink the evidence into a success.

- **Eligible collection.** A canonical collection attempt that read a product document (so either a
  revision was appended or the identity was unresolved, §5.1) for a supplier whose shadow switch
  was enabled at the time of the read, inside the declared Phase C evidence window.
- **Denominator.** Every eligible collection, derived from the canonical run records and the
  switch history (S7) — **not** from the shadow store. The shadow store cannot define its own
  denominator, because a lost write would then disappear from both sides.
- **Outcome per eligible collection.**

  | shadow outcome | counts as |
  | --- | --- |
  | a verdict of `MATCH` | success |
  | `MISMATCH` / `IDENTITY_MISMATCH` resolved `ADAPTIVE_CORRECT` (§6.2) | success for the Adaptive side; the canonical defect is filed separately |
  | resolved `SOURCE_AMBIGUOUS` | success only if the Adaptive side failed closed (`REVIEW_REQUIRED`) on every ambiguous field; an Adaptive `CONFIRMED` there is a failure |
  | resolved `CURRENT_CORRECT` or `BOTH_WRONG` | failure |
  | `MISMATCH` / `IDENTITY_MISMATCH` not yet resolved | `INCOMPLETE` |
  | `TEMPLATE_UNMATCHED` / `TEMPLATE_AMBIGUOUS` | failure (the validated bundle did not cover a real page) unless resolved as `SOURCE_AMBIGUOUS` |
  | `SHADOW_FAILED` | **failure** |
  | **no shadow record at all** | **`INCOMPLETE`** |

- **Evidence verdict.** Phase C evidence is `PASS` only when **every** eligible collection counts as
  a success. Any `INCOMPLETE` makes the evidence `INCOMPLETE`, and any failure makes it `FAIL`.
  An eligible collection is never omitted, excluded as "noise" or retried away; the evidence record
  lists the denominator and each collection's outcome by `collection_run_id`.
- **A window belongs to one bundle.** A profile fix is a new EPR and a new
  semantic tuple and `comparability_key` (§4.2, §4.2.1), so it opens a new evidence window with a new denominator;
  successes under the previous bundle do not carry over.

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
| **Is this profile trustworthy enough to become VALIDATED?** | **profile validation** (proposed `app/collect/profiles/`) | on demand, offline, zero network | one EPR bundle, the engine identity, an approved sample set, the negative-control suite | an immutable `ValidationRun` bound to exact digests (D7) |
| **Does the page still fit the validated template?** | **structural drift** = template conformance, inside the engine | every extraction, shadow or (later) canonical | the document and the bundle | a conformance record; affected fields fail closed |
| **Did the source's values change?** | **source-value drift**, `docs/ARCHITECTURE.md` §11 and ADR-0013 §3 | a current source revision pointer move | two revisions with equal `comparability_key` (§4.2.1), the same owner the amended ADR-0013 §3 rule uses | unchanged, except that the comparability condition is `comparability_key` |

### 7.1 Template matching fails closed

- A template signature is a predicate: every required anchor present and no forbidden anchor
  present. There is no score and no closest match.
- Exactly one template must match. None → `TEMPLATE_UNMATCHED`; more than one →
  `TEMPLATE_AMBIGUOUS`. Neither guesses.
- In the shadow this is a verdict only. The canonical behaviour is recorded here as the input the
  **future cutover ADR** must carry (Q3, accepted in review `5302725919`), and nothing implements it
  before that ADR:
  - stable source identity still resolves, but no unique template matches → **append a new current
    revision** whose affected facts fail closed as `REVIEW_REQUIRED` with a structural reason;
    keeping the pointer on the last good revision would present an unreadable page as current and
    unchanged;
  - identity unresolved or contradictory → **no revision** (ADR-0010 §11, unchanged).

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
importing nothing but the value models.

- **Their own manifest.** `integrations/suppliers/<key>/hook_identity.py` holds exactly three
  constants on the ADR-0010 §12 pattern: `HOOK_REVISION` (semantic), `HOOK_INPUTS` (every module of
  `hooks/`, never the manifest) and `HOOK_FINGERPRINT` (implementation), with the same acyclic,
  recomputed pin. It is separate from the `collect/` manifest, so a code extractor's identity
  (`kmretail-1`) is untouched.
- **What binds where.** The EPR binds `HOOK_REVISION` only, so it enters the EPR digest and the
  semantic tuple. `HOOK_FINGERPRINT` is recorded on each Adaptive revision as `hook_fingerprint`
  (§4.1) and in each `ValidationRun`; it never enters a digest that feeds the tuple.
- **Forcing.** Hook goldens (§4.4 guard 4) make a behaviour change without a `HOOK_REVISION` advance
  a failing build.

### 8.3 Anti-growth guards

| # | guard | enforcement |
| --- | --- | --- |
| G1 | no supplier-specific branch in core | AST repository rule: no comparison against a registered `supplier_key` literal, and no `integrations.suppliers.<key>` import, in `app/` or the engine |
| G2 | no unknown profile key | strict schema at DRAFT save |
| G3 | closed hook points | an enum owned by the engine; an unknown binding refuses the bundle |
| G4 | measurable adapter use | hook invocations per supplier × hook point in every conformance and shadow record; the validation report counts bindings; profile-only = zero bindings |
| G5 | promotion review | keyed on the machine-defined **promotion key** of §8.4, never on a prose notion of "same purpose": when bindings of two or more suppliers share a promotion key, validation computes `HOOK_PROMOTION_REVIEW_REQUIRED` for every one of them; ACTIVE (later) is refused until an architect decision record names that key |
| G6 | per-supplier cap | more than two bound hook points blocks VALIDATED until an architecture review is recorded: the supplier is not fitting the generic model, and that is a design finding, not a hook to add |
| G7 | proven paths only | no VALIDATED unless every bound hook is exercised by an approved sample or fixture |

### 8.4 The promotion key (review `5302725919` D6)

Every hook binding in an EPR carries three machine-readable parts, and the engine refuses a
binding that lacks any of them:

| part | domain | where it comes from |
| --- | --- | --- |
| `hook_point` | the closed enum of §8.1 | the binding |
| `target` | `identity` for `identity_decode`; a `FIELD_REGISTRY` key for `value_parse`; `options` for `option_decode`; `embedded` for `embedded_decode` | the binding, checked against the hook point (a `value_parse` binding for an unknown field key is refused) |
| `format_class` | a **closed, engine-owned enum per hook point** — proposed v1: `identity_decode`: `PATH_CODE`, `ENCODED_TOKEN`, `COMPOSITE_CODE`; `value_parse`: `MONEY_TEXT`, `QUANTITY_TEXT`, `CONDITIONAL_POLICY_TEXT`, `LABELLED_TEXT`; `option_decode`: `SELECT_CONTROL`, `BUTTON_GROUP`, `SCRIPT_MATRIX`; `embedded_decode`: `KEY_VALUE_BLOCK`, `SCRIPT_ASSIGNMENT` | declared on the binding; a value outside the enum is refused; a new class is an engine change with its own review |

```text
promotion_key = (hook_point, target)                      # the trigger: derived, cannot be mis-declared
promotion_group = (hook_point, target, format_class)       # reported beside it, for the promotion decision
```

- **The trigger uses only derived parts.** `hook_point` and `target` follow from what the binding
  does, so a supplier cannot avoid G5 by declaring a different `format_class`. The class groups
  the evidence the architect reviews; it never suppresses the flag.
- **Computed over all suppliers.** At every validation run, over the bindings of every
  non-`RETIRED` EPR of every supplier. The result is a count per `promotion_key` and the list of
  suppliers sharing it. It is derived on each run, and no operator action clears it.
- **What resolves it.** Only an architect decision recorded against the `promotion_key`: promote
  into a generic extractor or profile rule (then the hooks are retired through new EPRs), or keep
  site-specific with a reason. G4 metrics are reported per `promotion_key`.

---

## 9. D7 — DRAFT save vs VALIDATED/ACTIVE promotion

| state | how it is entered | may be used for |
| --- | --- | --- |
| `DRAFT` | a strict schema parse passes; digest computed; lint findings recorded (an unmapped CORE field is allowed in a DRAFT). Costs no network. | offline evaluation against samples only |
| `VALIDATED` | **derived**: a `PASS` `ValidationRun` exists for this exact freshness tuple `(extraction_profile_digest, profile_schema_version, engine extractor_revision, engine extractor_fingerprint, hook_fingerprint, sample-set digest, capture revision)`. A change to any of them lapses it with no stored flag to forget. The freshness tuple deliberately includes implementation fingerprints; the semantic tuple (§4.2) deliberately does not, so an implementation-only change forces revalidation without being `EXTRACTOR_CHANGED`. | shadow |
| `SHADOW` | a designation: VALIDATED + the per-supplier shadow switch | shadow comparison (D3) |
| `ACTIVE` | **not authorized in this track.** Requires a cutover ADR, Phase C evidence and the user's approval; at most one ACTIVE bundle per supplier; each switch writes **only** an append-only profile lifecycle transition (§9.3) | canonical revisions (later) |
| `RETIRED` | explicit transition; never deleted | reading history |

### 9.1 Validation checks (all deterministic, zero network)

- **V1 referential** — every pinned PTR exists and recomputes to its digest; every hook binding
  resolves to a running hook manifest whose `HOOK_REVISION` equals the bound one; manifests are
  current.
- **V2 coverage** — every CORE field has a rule in every template.
- **V3 sample agreement** — for each approved sample, the engine output equals the sample's
  expected facts on every must-match dimension of D4. At least one sample per template, at least
  two in total. **Expected facts are authored or verified by the operator from the source — never
  by AI and never by the profile under validation**, or validation would be circular.
- **V3a conflict scan** — generic, profile-independent detectors (price-like label rows, purchase,
  cart and sold-out controls, option selectors and their option states, identity declarations,
  JSON-LD offers) run over the **whole** captured snapshot, including every product control the
  capture sanitizer kept (§9.2). Any statement they find that the bundle's locators neither read nor explicitly
  dispose of is a finding the operator must resolve before PASS, so evidence outside the
  bundle's attention cannot pass silently.
- **V4 negative controls** — a login page, a non-product page, and a mutation suite generated
  deterministically from each sample (remove a required anchor, duplicate a price row, inject
  hidden sold-out text, add a second conflicting identity) must fail closed exactly as D5 says.
- **V5 determinism** — two evaluations give identical outputs and digests.
- **V6 safety** — the evidence sanitizer and the secret scan pass; observed fragments within
  4 KiB; nothing forbidden by ADR-0010 §8.
- **V7 hook guards** — G5–G7.

### 9.2 `ValidationSample` — its own retention, separate from shadow records (Q2, review `5302725919`)

A `ValidationSample` is what V3 and V4 replay. It is **proof material for a profile**, not a
shadow observation, so it has its own rule.

- **What it is.** A sanitized, product-scoped **structured** snapshot — an element tree with its
  text, attributes and image references — plus the operator-verified expected facts.
- **Who decides what it contains: an independent capture owner, never the bundle under
  validation** (re-audit `5302852218` item 3). The snapshot is cut at capture time by:
  - a **versioned capture sanitizer** (`capture_revision`) with its own parser and its own generic,
    profile-independent rules, which **keep product evidence and strip private material**
    (re-audit `5302910552` item 1):

    | kept, sanitized (product-scoped source evidence, ADR-0010 §8 `CONTROL_STATE` among it) | stripped |
    | --- | --- |
    | purchase, buy and cart buttons and their enabled/disabled/hidden state | credentials, passwords, tokens, CSRF and other hidden security fields |
    | sold-out and restock controls and markers | authorization, session and cookie material in any attribute or value |
    | option selectors (`select`/`option`, radio and button groups) with their labels, order, selected/disabled state and non-secret value attributes | member and account fields (names, IDs, grades, points, addresses, contacts) |
    | quantity inputs' structure and bounds (`min`, `max`, `step`), not a user-entered value | cart and account submission payloads, and every user-entered or private value |
    | forms that carry the above, reduced to their structure; the `action` kept only as a sanitized path under the ADR-0010 §9 URL rules | scripts other than JSON-LD; account, member and navigation regions; every forbidden category below |

    Stripping is by what a value **is**, never by element type: a `form`, `input`, `select` or
    `button` is not removed for being a control. A kept control whose attribute is secret-bearing
    keeps the control with that attribute removed, and the removal is recorded; and
  - an **operator-approved product scope**, chosen on the captured page by the operator and recorded
    with the sample (who, when, the scope boundary) **before** any candidate profile evaluates it.
    The default scope is the whole document body after the sanitizer's exclusions; the operator may
    only exclude further regions for privacy, with each exclusion recorded.

  No EPR or PTR, candidate or validated, may define, narrow or filter the snapshot. A candidate
  bundle only **reads** from it. The sample's provenance names the `capture_revision` and the
  scope decision, never a profile, and a `ValidationRun` refuses a sample whose provenance names
  one. A too-narrow operator scope is visible in the recorded boundary, and the V3a conflict scan
  runs over everything the scope kept, product controls included.
- **What it never is or holds.** Never whole authenticated HTML; never cookies, headers, session or
  authorization material; never account, member or page-wide data; never secret-bearing URL
  material (ADR-0010 §8, §9). A snapshot that fails the sanitizer or the secret scan is not saved.
  These exclusions belong to the capture owner and are unchanged by the scope rule above.
- **Immutable and content-addressed.** Its digest is over its canonical serialization, and the
  sample-set digest of a `ValidationRun` is over the ordered sample digests. It is never edited;
  a corrected expectation is a new sample.
- **Retained while referenced.** A sample is kept as long as any `ValidationRun` or recorded profile
  proof references it, and it becomes eligible for removal only when nothing does. It is never
  pruned by the shadow age/count bound (§5.4), and a shadow bound never reaches it.
- **Local only.** Kept in the data root, never committed. Repository fixtures for Phase B are
  synthetic (§12), never a captured sample.

### 9.3 An ACTIVE switch is not a pointer move (re-audit `5302910552` item 2)

ADR-0013 §3 advances a `SourceProduct`'s current source revision only to a newly recorded,
eligible `ProductFactsRevision`. Activation records none, so it moves no pointer.

- **What an ACTIVE switch writes:** one append-only profile lifecycle transition (who, when, from,
  to, the EPR, the reason, the correlation). Nothing else — no `ProductFactsRevision`, no pointer
  row, no derived-result invalidation, no ReviewItem.
- **When the pointer moves:** only when a **later** canonical collection records a new eligible
  revision under the newly active bundle, and that revision becomes current by the unchanged
  ADR-0013 §3 advance rule. Because its `comparability_key` differs from the previous current
  revision's, **that** pointer move is recorded as `EXTRACTOR_CHANGED`, with the usual
  invalidation of dependent derived results.
- **No new revision, no pointer move.** A product that is not collected again after the switch
  keeps its current source revision, with its original provenance, indefinitely. Its current
  revision is never re-labelled, re-evaluated or backfilled because a different bundle is now
  ACTIVE.
- The same holds for leaving ACTIVE (a switch to another bundle or back to a code extractor): a
  transition only, and pointer moves only through later recorded revisions.

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
- **Egress.** A sample shown to AI is the sanitized, product-scoped `ValidationSample` (§9.2), never cookies,
  member data or page-wide private HTML. Sending it to a cloud provider is data egress under
  ADR-0012 and needs the user's approval; the default is the local capability or none.

---

## 11. D9 — Second-supplier profile-only proof criteria (deferred)

Nothing here runs until the first-vertical restriction is lifted or an architect-approved canonical
amendment authorizes a bounded proof (Q6, accepted in review `5302725919`: Phase D stays deferred).

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
| **B** prototype (Q5) | an **isolated, disposable, fixture-only** prototype of the engine, strict profile parser/validator, template matcher, D4 comparison model and an offline harness over synthetic fixtures (KM-shaped synthetic pages included); a report file, no store. **Not started**: it needs its own go-ahead after this proposal passes | none: outside every production and runtime package; no `app/collect` or supplier-integration wiring; no DB, schema or migration; nothing at runtime imports it; no canonical acceptance claim | 0 |
| **ADR → production** | production-intended engine, profile and shadow code | only after the ADR is merged | 0 |
| **C** KM shadow | profile and shadow store migrations, the D3 hook in `collect`, a KM profile, the switch; shadow runs only on ordinary operator collections | per the ADR | only ordinary operator collections, with the user's go-ahead; no read exists for the shadow's sake |
| **D** second supplier | D9 | cutover ADR | only under D9 preconditions |

**Phase B isolation (Q5, conditional yes in review `5302725919`).** Before the ADR, Phase B may exist
only as a disposable prototype. Proposed shape: a top-level `prototypes/adaptive_collector/`
directory on its own draft PR, kept as review evidence and **not merged to main**; its fixtures are
synthetic; it imports nothing from `app/` or `integrations/` that acts (at most the pure
`app.collect.facts` value models, read-only), and nothing imports it. Its outputs claim nothing for
any acceptance. Production-intended implementation is written fresh after the ADR, never by
promoting the prototype in place. If the architect prefers the prototype on main, a repository
rule must forbid every import of it from `app/`, `integrations/` and `scripts/`.

**Phase C exit evidence** (proposed): the §5.5 rule — the denominator is every eligible KM
collection with the shadow enabled, and the evidence is `PASS` only if each one counts as a
success; any missing shadow outcome is `INCOMPLETE`, any `SHADOW_FAILED` a `FAIL`. Plus S1 and S2
differential proofs, zero AI, ledger equality, and the shadow retention bounds fixed by the ADR
(§5.4). Honest limit:
the accepted KM product states no options and no tiers (`docs/acceptance/M3.md` §2.1), so positive
options and tiers are proven only on synthetic fixtures, and the engine inherits the M3 boundary.

**Phase B negative controls** (proposed): a profile with an unknown key is refused; a hook binding
outside the enum is refused; a stale digest is refused; two conflicting locators give
`REVIEW_REQUIRED`; zero and two matching templates fail closed; a mutated anchor never yields
`CONFIRMED`; the engine imports no AI, network, DB or `app.collect` writer; the shadow runner has no
reachable gateway; identical input gives identical output.

---

## 13. Architect rulings (review `5302725919`) and what they require

| # | question | ruling | carried by |
| --- | --- | --- | --- |
| Q1 | names | **ACCEPT**: `ExtractionProfileRevision` (EPR) and `PageTemplateRevision` (PTR); CONNECT `SupplierProfile` and COLLECT `CollectionProfile` stay distinct; add glossary entries | this revision (throughout); §13.1 entries land with the ADR |
| Q2 | sample and shadow retention | **MODIFY**: two rules, not one. `ValidationSample` = sanitized, product-scoped structured snapshot, immutable, content-addressed, retained while referenced. Shadow records = non-canonical, explicit age + count bounds fixed by the ADR before Phase C. Phase B stays synthetic/fixture-only | §9.2, §5.4 |
| Q3 | unmatched template on the canonical path | **ACCEPT** for the future cutover ADR; not implemented before it | §7.1 |
| Q4 | implementation fingerprint in `extraction_semantics_id` | **ACCEPT**: excluded; semantic code changes mechanically forced to advance `extractor_revision` — and, per re-audit `5302852218`, no hook implementation fingerprint either, directly or through the EPR digest; hooks are forced through `HOOK_REVISION` | §4.2, §4.4, §8.2 |
| Q5 | Phase B before the ADR | **CONDITIONAL YES**: isolated, disposable, fixture-only, outside production/runtime packages, no wiring, no DB/schema/migration, no runtime import, no acceptance claim; production-intended code waits for the ADR | §12 |
| Q6 | Phase D | **ACCEPT**: deferred under `CLAUDE.md` §12 until the first vertical closes or a canonical amendment authorizes it | §11, §12 |

No question remains open in this proposal. The retention **values** of Q2 and the Phase C
evidence window size are for the ADR.

### 13.1 Draft `docs/GLOSSARY.md` entries (land with the ADR, not before)

GLOSSARY points every name at the contract that owns it, and that contract does not exist yet, so
these entries are drafted here and added by the ADR PR:

| name | what it is | owner |
| --- | --- | --- |
| `SupplierProfile` | the **CONNECT** profile of a supplier: key, display name, base URL, auth flag, egress hosts, request policy. Not an extraction profile | ADR-0007 |
| `CollectionProfile` | the **COLLECT access envelope**: product path form, policy paths, explicit image hosts, safe query keys, frozen limits, transport. Repository-reviewed, never widened at run time | ADR-0010 §3, §9 |
| `ExtractionProfileRevision` (EPR) | an immutable, content-addressed revision of one supplier's **interpretation** — identity rule, vocabularies, image-role rules, hook bindings — pinning a closed set of PTRs by digest. Validated and activated as one bundle | the Adaptive Collector ADR |
| `PageTemplateRevision` (PTR) | an immutable, content-addressed revision of **one page shape** of a supplier: its signature and per-field locator rules. Never activated alone | the Adaptive Collector ADR |
| `comparability_key` | the tagged semantic tuple that decides drift comparability (ADR-0013 §3 as amended): `("CODE", extractor_revision)` for a row without profile provenance, derived on read and never stored or backfilled; `("ADAPTIVE", …)` for an Adaptive row | the Adaptive Collector ADR; ADR-0013 §3 as amended |
| `extraction_semantics_id` | the stored, collision-resistant digest of an Adaptive row's semantic tuple; an integrity and indexing aid, never the comparability decision by itself | the Adaptive Collector ADR |
| `HOOK_REVISION` / `HOOK_FINGERPRINT` | a supplier hook manifest's semantic revision (enters the EPR) and implementation fingerprint (provenance and validation freshness only) | the Adaptive Collector ADR |
| `ValidationSample` | an operator-verified, sanitized, product-scoped structured snapshot replayed by profile validation, cut by the independent capture owner and never by the profile it validates; never a whole authenticated page | the Adaptive Collector ADR |
| promotion key | `(hook_point, target)` of a hook binding; shared by two or more suppliers, it requires an architect promotion decision | the Adaptive Collector ADR |

---

## 14. What this proposal explicitly does not do

It changes no code, schema, ADR or canonical document (the GLOSSARY entries of §13.1 are drafts);
backfills or updates no existing `ProductFactsRevision`; adds no `EvidenceKind`, `FieldStatus`,
`ReviewKind` or field; widens no host, path, budget or transport; makes no supplier, marketplace, AI
or OCR call; does not touch or migrate the KM통상 extractor; starts no Phase B prototype; and
authorizes no second-supplier read.

## References

- Issue #110 and kickoff `5811580104`; PR #111 architect review `5302725919` and re-audits `5302852218`, `5302910552`
- ADR-0007, ADR-0010 §3–§12, ADR-0012 §9 §13 §14, ADR-0013 §3, ADR-0016 §2 §6
- `docs/ARCHITECTURE.md` §4, §5, §11; `ROADMAP.md` §9, §14.3; `docs/acceptance/M3.md` §2
- `integrations/suppliers/collection.py`, `integrations/suppliers/extraction.py`,
  `integrations/suppliers/kmretail/collection.py`, `app/collect/collection.py`, `app/collect/facts.py`,
  `tests/unit/test_repository_rules.py`
