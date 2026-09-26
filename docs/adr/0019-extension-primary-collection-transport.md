# ADR-0019 — Extension-primary COLLECT transport, the direct-URL fallback and one server-owned pipeline

Status: **ACCEPTED** — decided by the architect decision `5844537419` on Issue #126 (2026-09-26),
with the E0 scope, seven further rulings and the Claude AI cross-audit PASS relayed with it, on
canonical main `4ba99fbeec01553fb3d046953e40a87706d8e40b`. This is E0 of the browser-first
Collector: a contract-only, runtime-zero step.
- It records the decision's rulings (D1–D7) and binding rules as contract, before any code. Where
  the decision asked the ADR to define a rule, the rule is defined here and is open to the
  exact-head audit.
- Amended before merge by the architect ruling `5845080203`, which binds the UI ownership and state
  semantics of the Issue #126 follow-up `5845042878` into this contract (§12). The exact-head audit
  `5325435055` on `406549a6` was superseded for audit by that ruling. It also confirmed three
  points: the §3 security boundary stays; transport alone is never drift, while observed evidence
  differences still give `EVIDENCE_DRIFT`; and list-queue caps are mandatory.
- It amends ADR-0010 and ADR-0017 **by amendment notes only**. Their text, their rulings and the
  ADR-0017 invariants AC-01 to AC-29 are neither rewritten nor renumbered.
- **Its implementation authority becomes effective only after this exact contract PR is audited,
  independently cross-audited and merged**, and even then only slice by slice (§10, §11).

**It authorizes nothing to run** (§11).

Decision owner: Architect (ChatGPT). Sources:
- the architect decision `5844537419` on Issue #126 (D1–D7, the binding rules, the E0–E3 sequence);
- the Issue #126 proposal, recording the operator's requirement and the old extension's failures;
- the Phase C C1 STOP report `5844496942` and its disposition `5844538783` on Issue #110;
- the Issue #126 UI follow-up `5845042878` and the ruling `5845080203` that binds it here (§12);
- the contracts this ADR builds on and does not replace: ADR-0006 (one owner per data root),
  ADR-0007 (CONNECT), ADR-0010 (COLLECT and `ProductFactsRevision`), ADR-0013 (the canonical
  Product and its current source revision pointer), ADR-0017 (the Adaptive Collector).

Recorded by: Claude Code. The number was confirmed free in `docs/adr/`, on `main` and in every open
PR immediately before writing; ADR-0018 is Gate 3 and no competing ADR draft exists.
Date: 2026-09-26
Related:
- **Amends by note** ADR-0010 §3, §4, §5, §8, §9, §12 and §13.
- **Amends by note** ADR-0017 §2, §5, §7.3, §10 and §15.
- Unchanged: ADR-0006, ADR-0007, ADR-0013, ADR-0014, ADR-0016 and ADR-0018.

---

## Context

The operator's intended Collector was a Chrome extension, like the pre-ICBM-NEW ICBM "COLLECTOR",
rebuilt because the old one failed. It worked in two ways:
- **single product:** open a product page, click, collect, send to ICBM;
- **list:** discover a list page's product links and collect them all.

It failed in three ways:
1. frequent extraction errors;
2. one shared adapter for all sites, where each fix broke another site;
3. about 3–4 hours for 500 products.

Issue #110 built the Adaptive Collector behind the existing server-side gateway: P1–P3, C0,
C1 PREP-0 and PREP-1. In Phase C C1 the two authorized collections were recorded, but both capture
candidates were refused fail-closed (`5844496942`):
- the candidate was cut from the whole document;
- one shared, non-product anchor reached the private-material final scan before any product
  scoping.

The architect ruled on Issue #126 (`5844537419`):
- the Chrome extension becomes the primary operator-facing capture transport;
- Collection Management stays, as the run/status surface and as the explicit direct-URL fallback;
- everything after acquisition stays one server-owned pipeline;
- the Adaptive work is kept as the extraction and validation core. Failures 1 and 2 are what it was
  designed to remove.

## Decision

### 1. Two acquisition transports, and their priority

- **`TransportKind`** is closed: `EXTENSION` or `DIRECT_URL`.
  - **`EXTENSION`** is the **primary** transport and the normal operator capture UX. The operator's
    own Chrome, through a first-party MV3 extension, captures the current product page on a click,
    and later (§8) a list page's discovered products.
  - **`DIRECT_URL`** is the **fallback**, for pages or sites the extension cannot capture reliably.
    It is the existing server-side collection gateway (ADR-0010 §3), reached from Collection
    Management's direct-URL entry.
- **Collection Management owns the whole picture.** It is kept, never removed:
  - the run, status, error and review history of both transports;
  - the direct-URL fallback submission.

  The extension may show its own compact progress. Canonical run and result state comes from the
  application.
- **The two browsers are different things** (code fact F1).
  - ADR-0010 §3's "HTTP or browser execution" is **server-side** browser execution: the gateway
    driving a browser it owns.
  - `EXTENSION` is the **operator's own browser**, running a first-party extension.
  - Nothing in this ADR changes the meaning of server-side browser execution.

### 2. One pipeline after capture

```text
EXTENSION (operator's Chrome) ─┐
                               ├→ DocumentView → canonical extractor (+ Adaptive shadow)
DIRECT_URL (server gateway) ───┘      → ProductFactsRevision → Product DB
```

- Both transports produce **the same normalized `DocumentView` contract**. The canonical extractor
  and the Adaptive engine consume it unchanged.
- A field of the contract a transport cannot supply is refused, never defaulted.
- Both transports create and settle **the same durable collection run** with the same outcomes.
  There is no extension-only hidden database and no duplicate truth model.

### 3. The extension is transport only

- The extension **never writes** the database, a `ProductFactsRevision`, a current source revision
  pointer or a source asset.
- **Revision writer during the transition.** The existing KM canonical extractor stays the only
  `ProductFactsRevision` writer. The Adaptive engine stays shadow.
- **`ACTIVE`** is forbidden until a separate cutover ADR. ADR-0017 §7.4 is its starting point.
- Transport and canonical interpretation authority never change in the same slice.
- **The security boundary** (D4):
  - Ingest is loopback-only.
  - The extension is authorized by an explicit pairing that can be revoked and rotated, in addition
    to the existing loopback client and CSRF checks (`X-ICBM-Client`), never replacing them.
  - The extension never sends cookies or request headers; local or session storage; credentials or
    authentication forms; account, member or cart payloads.
  - Its Chrome host permissions are limited to reviewed supplier hosts.
  - Server-side request, node, byte and image ceilings bind every ingest, however the extension
    behaves. An ingest over a ceiling is refused whole.

### 4. `TransportKind` and provenance

- Every collection run and revision written after this ADR's implementation records its
  `TransportKind`. For `EXTENSION`, it also records the `BrowserCapturePolicy` revision and digest
  (§5).
  - These are additive, nullable provenance fields added by the implementing slice.
  - Nothing is backfilled; a row without them predates this ADR.
- **This is a new concept** (code fact F3). It is not aligned to any `acquisition_mode` field or
  enum; ADR-0017 has none.
- It follows ADR-0017 §5's principle: acquisition and implementation information never enter
  semantic identity.
- **Transport enters none of the following:** the evidence digest; the field fingerprint; the
  source fingerprint; the extraction semantics (`extraction_semantics_id` and its tuple); the
  comparability decision (`comparability_key`). A change of transport is therefore never, by
  itself, a drift event.
- **But observed evidence still counts.** If the two transports observe different locators or
  different observed content for the same product, the resulting `EVIDENCE_DRIFT` arises under the
  existing rules, unchanged.

### 5. `BrowserCapturePolicy` — an independent capture-topology owner

- A supplier-scoped **`BrowserCapturePolicy`** is an owner **separate from EPR and PTR**. It is
  repository-reviewed, versioned and digested, and never widened at run time.
- **It owns only the capture topology:**
  - the product root;
  - the allowed and the excluded DOM regions (global, account, cart, contact, navigation);
  - the allowed attributes;
  - the node and byte upper bounds.
- **It does not own the access envelope** (code fact F4). `CollectionProfile` keeps owning host,
  path, query, pacing and transport: `product_path`, `policy_paths`, `image_hosts`,
  `safe_query_keys`, `limits` and `transport`. The two owners never overlap.
- **It is not a profile.**
  - It holds no source fact, no field rule and no extraction rule.
  - It is never an EPR or PTR.
  - No Adaptive bundle can narrow its own `ValidationSample` (ADR-0017 AC-11).

### 6. The capture order is fixed

```text
product-scope cut → browser sanitize → loopback → server final scan → DocumentView
```

- **Scope first.** The product-scope cut happens first, in the browser, under the
  `BrowserCapturePolicy`. **A whole authenticated page is never sent or retained.**
- **The server always runs its own sanitizer and final scan** on exactly what arrived. Browser
  sanitization is defense in depth, never the only guard.
- **Forbidden:** whole-page capture followed by exceptions for `href` or anchor text. A final-scan
  finding is never cleared by such an allowance.
- **Private material inside the product scope still refuses fail-closed.**
- **The C1 regression pair** is required of the first extension slice:
  - the shared non-product anchor that blocked both C1 candidates must be absent, because it lies
    outside the scope;
  - private material placed inside the scope must still refuse.

### 7. Images

- The server's policed fetch is the **default**. It is also the owner of the canonical checksum and
  the source asset (ADR-0010 §9).
- The extension may name sanitized image candidate references found in the product scope.
- An extension-provided hash or role is never canonical.
- **A browser byte relay is not authorized by this ADR.** If a browser-session-bound or blob image
  ever requires one, it is a separate, bounded slice with its own authorization, under the existing
  per-image and per-attempt caps.

### 8. List pages and the bounded queue

- A list page leads to discovered product links, which lead to a **bounded queue**. Every
  discovered product becomes an ordinary collection run through the same owners.
- **The bounds must be declared.** A missing cap refuses fail-closed, before any read, under
  ADR-0010 §4's cap principle and no-default rule. The caps cover the queue size, the concurrency,
  the per-host pacing and the per-run budgets.
- Every automated read obeys ADR-0010 §4's per-host pacing and server limits. These are enforced by
  server-issued work, never by the extension's own clock alone.
- **Performance** is a later benchmark, never an acceptance promise; correctness and bounded
  behaviour come first. "500 products in under an hour" is an example of such a target.

### 9. No legacy extension code

The old ICBM extension implementation is **not inspected, copied or transplanted** (CLAUDE.md §2).
Only the operator-confirmed behaviour is inherited:
- single-click product collection;
- list-page link discovery into a bounded queue.

A narrow legacy reference needs its own architect authorization, for a concrete behaviour that
cannot be reconstructed from the requirement.

### 10. `EXTENSION` is a reviewed transport by contract only, in E0

E0 fixes `EXTENSION` as a reviewed transport **in this contract only**. Changing the actual
`CollectionProfile` or access envelope, and activating the transport, are **E1**.

That is forced by the code (code fact F2):
- `CollectionProfile.__post_init__` refuses any `transport` other than
  `SupplierTransport.HTTP` ("collection over HTTP only").
- `SupplierTransport` is `{HTTP, BROWSER}` and has no `EXTENSION` member.
- Admitting `EXTENSION` means extending that enum and relaxing that check. Both are
  `integrations/` changes, so they are impossible in E0.

The steps:
- **E0:** this ADR and its notes.
- **E1:** new extension code; one approved KM통상 product page and a click; product-scoped capture;
  loopback ingest; server final scan; the same `DocumentView`; the KM canonical extractor and the
  Adaptive engine as dry-run / compare only. No `ProductFactsRevision`, no pointer change, no list
  crawl.
- **E2:** the existing KM extractor consumes the extension's `DocumentView` as the revision writer.
- **E3:** the list-page bounded queue (§8).

Each step needs its own authorization. E1 follows only after this ADR merges.

### 11. What this ADR does not authorize

- extension code, an ingest endpoint or route, a manifest, a pairing flow, a model or a migration;
- any change to `CollectionProfile`, `SupplierTransport` or any other `integrations/` code;
- any supplier or provider read, and any AI, OCR or vision call;
- resuming C1, a third read, or reusing the preserved campaign `phase-c-kmretail-01` or its data
  root. They stay stopped and preserved (`5844538783`), and the old-transport C2–C4 plan stays
  superseded for execution;
- `ACTIVE` or any cutover;
- a browser image byte relay;
- a new `EvidenceKind`.

### 12. UI ownership and state semantics (ruling `5845080203`)

These rules bind ownership and state semantics, not visual styling. The visual prototype is promoted
separately: **`docs/UI_SOURCE_OF_TRUTH.md` is not changed by this ADR**. It switches only when the
operator approves a concrete prototype revision and its file name, fingerprint and repository copy
are recorded under that file's own process.

1. **The Chrome extension side panel owns only the capture UX:**
   - single-product click capture;
   - list-link discovery and the progress of the bounded queue (§8);
   - the supplier/session and ICBM connection indication;
   - the field value and evidence preview;
   - extension-local exception and transport states.
2. **ICBM Collection Management owns canonical run management:**
   - the `DIRECT_URL` fallback submission;
   - the history, progress and results of both `EXTENSION` and `DIRECT_URL` runs;
   - the canonical review entry points;
   - run and job failures.
3. **The state axes are separate and are never collapsed in any UI:**
   - the **run outcome**: `RECORDED`, `NO_REVISION` or `FAILED`;
   - the **facts status and field truth**, with the existing canonical semantics: `CONFIRMED`,
     `ABSENT`, `REVIEW_REQUIRED`;
   - the **failure class and code**, for example `AUTH`.

   **`REVIEW` is not a run outcome, and `AUTH` is not a field state.** `AUTH` is a run- or
   job-level stop or failure condition.
4. **The extension's acknowledgement:**
   - Before the server-owned canonical result exists, the extension shows only transport states
     such as `전송됨` / `처리 중`.
   - `RECORDED`, `NO_REVISION` or `FAILED` appears only after the server-owned result or its
     read-back. `ICBM에 전달됨 · RECORDED` is such a read-back.
   - A JSON download is never the primary handoff. Extension results go to ICBM, and canonical
     result state comes back from the application.
5. **Images.**
   - The extension may preview a sanitized candidate reference, its role and its order only.
   - It has no image download or source-asset action.
   - The canonical checksum and the source asset stay server-owned (§7), and a browser byte relay
     stays unauthorized.
6. **A disconnected ICBM.**
   - The extension never persists an authenticated whole DOM locally because ICBM is unreachable.
   - Any future retry buffer needs its own, separately defined capture-envelope, sanitization and
     retention contract. Without that contract the extension fails closed and keeps no page
     material.
7. **The Collection Management start screen.**
   - Its `EXTENSION` area is connection and entry guidance and recent intake/status. It is never an
     in-app capture button; the capture action happens in Chrome.
   - `DIRECT_URL` is the actionable fallback form inside ICBM.
8. **The `BrowserCapturePolicy` UI.**
   - E1 has no general operator policy editor.
   - E1 may expose only the diagnostics the bounded KM single-click acceptance requires.

## Invariants

```text
AC-01  COLLECT has exactly two acquisition transports, EXTENSION (primary) and DIRECT_URL (fallback); Collection Management is kept and owns the run, status and review history of both and the direct-URL submission
AC-02  EXTENSION is the operator's own browser running a first-party extension; it is never the server-side browser execution of ADR-0010 section 3
AC-03  Both transports produce the same DocumentView contract and the same durable collection run and outcomes; a field a transport cannot supply is refused, never defaulted
AC-04  The extension is transport only: it never writes the database, a ProductFactsRevision, a source pointer or a source asset
AC-05  During the transition the existing KM extractor stays the only ProductFactsRevision writer and Adaptive stays shadow; ACTIVE is forbidden until a separate cutover ADR
AC-06  Ingest is loopback-only and requires an explicit, revocable and rotatable pairing in addition to the existing loopback client and CSRF checks; server-side ceilings bind every ingest and an over-ceiling ingest is refused whole
AC-07  The extension never sends cookies, request headers, local or session storage, credentials, auth forms or account or cart payloads; its host permissions are limited to reviewed supplier hosts
AC-08  TransportKind is provenance first introduced by this ADR; it enters no evidence digest, field fingerprint, source fingerprint, extraction semantics or comparability_key, and is never by itself a drift event
AC-09  A difference in observed locators or content between transports still yields EVIDENCE_DRIFT under the existing rules
AC-10  BrowserCapturePolicy is an owner separate from EPR and PTR; it owns only the capture topology (product root, allowed and excluded regions, attributes, node and byte bounds) and holds no source fact or extraction rule
AC-11  CollectionProfile keeps owning host, path, query, pacing and transport; BrowserCapturePolicy never overlaps it
AC-12  The capture order is product-scope cut, browser sanitize, loopback, server final scan, DocumentView; a whole authenticated page is never sent or retained
AC-13  A final-scan finding is never cleared by an href or anchor-text exception after a whole-page capture; private material inside the product scope still refuses fail closed
AC-14  The server policed fetch is the image default and the owner of the canonical checksum and source asset; a browser byte relay is not authorized by this ADR
AC-15  A list-page queue is bounded by declared caps; a missing cap refuses fail closed before any read
AC-16  No legacy ICBM extension code is inspected, copied or transplanted; only single-click collection and list-link discovery are inherited as requirements
AC-17  In E0 EXTENSION is a reviewed transport by contract only; changing CollectionProfile or SupplierTransport and activating the transport belong to E1
AC-18  This ADR authorizes no extension code, endpoint, migration, supplier read, C1 resumption, ACTIVE, byte relay or new EvidenceKind; each later slice needs its own authorization
AC-19  The Chrome extension side panel owns only the capture UX: single-product click capture, list-link discovery and bounded queue progress, supplier/session and ICBM connection indication, field value and evidence preview, and extension-local exception and transport states
AC-20  ICBM Collection Management owns canonical run management: the DIRECT_URL fallback submission, the history, progress and results of both transports, the canonical review entry points and run and job failures
AC-21  Run outcome (RECORDED, NO_REVISION, FAILED), facts status and field truth (CONFIRMED, ABSENT, REVIEW_REQUIRED) and failure class or code are separate axes, never collapsed in any UI; REVIEW is not a run outcome and AUTH is not a field state
AC-22  Before the server-owned canonical result the extension shows only transport states; RECORDED, NO_REVISION and FAILED appear only after the server-owned result or read-back; a JSON download is never the primary handoff
AC-23  The extension may preview a sanitized candidate image reference, role and order only; it has no image download or source-asset action
AC-24  A disconnected ICBM never causes authenticated whole-DOM local persistence; a retry buffer needs its own separately defined capture-envelope, sanitization and retention contract, and without it the extension fails closed
AC-25  The EXTENSION area of the Collection Management start screen is connection and entry guidance and recent intake or status, never an in-app capture button; DIRECT_URL is the actionable fallback form
AC-26  E1 exposes no general BrowserCapturePolicy editor, only the diagnostics the bounded KM single-click acceptance requires; docs/UI_SOURCE_OF_TRUTH.md changes only when an approved prototype revision and its fingerprint are recorded under its own process
```

## Consequences

- The operator gets the Collector they asked for: the extension as the normal capture UX, and
  Collection Management as the run surface and fallback.
- The Adaptive work is kept. Its engine, profiles, validation, sanitizer and harness become the
  extraction core behind a new transport, and not a parallel system.
- The server gains a new inbound boundary (ingest, pairing, ceilings). It is designed and audited
  in E1, before any code runs against a supplier.
- The C1 refusal becomes a design requirement: the capture scope is cut before the final scan.
  The scanner is not weakened.
- Transport and parser authority change in separate slices, so no single slice changes both where a
  document comes from and who interprets it.

## References

- Issue #126 — the proposal and the architect decision `5844537419`.
- Issue #110 — the C1 STOP report `5844496942` and its disposition `5844538783`.
- Issue #126 — the UI follow-up `5845042878` and the ruling `5845080203` (§12); the superseded audit
  `5325435055`.
- ADR-0010 §3, §4, §5, §8, §9, §12, §13 (amendment notes).
- ADR-0017 §2, §5, §7.3, §10, §15 (amendment notes).
- `docs/ARCHITECTURE.md` §4 COLLECT; `docs/GLOSSARY.md` §3a; `ROADMAP.md` §14.3.
- Code facts, at main `4ba99fbe`:
  - F1: ADR-0010 §3 names server-side browser execution;
  - F2: `integrations/suppliers/collection.py` `CollectionProfile.__post_init__` and
    `integrations/suppliers/base.py` `SupplierTransport`;
  - F3: ADR-0017 has no `acquisition_mode`;
  - F4: the `CollectionProfile` fields.
