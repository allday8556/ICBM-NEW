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

Recorded by: Claude Code. The number was confirmed free in `docs/adr/` (the ADR directory then; `documents/decisions/adr/` since Issue #151), on `main` and in every open
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
- **The server always runs its own structural check and security gate** on exactly what arrived.
  Browser sanitization is defense in depth, never the only guard.
- **Forbidden:** whole-page capture followed by exceptions for `href` or anchor text. A final-scan
  finding is never cleared by such an allowance.
- **Security material inside the product scope still refuses fail-closed** (§6.1).
- **The C1 regression pair** is required of the first extension slice:
  - the shared non-product anchor that blocked both C1 candidates must be absent, because it lies
    outside the scope;
  - security material placed inside the scope must still refuse.

### 6.1 Broad product capture, security-only gate (the user's decision, 2026-10-01)

The first real KM통상 E1 acceptances (`documents/acceptance/adaptive/EXTENSION-E1.md` §5.1) were
refused because the server's final gate refused anything the Adaptive capture owner's sanitizer
would have cleaned: a member-named element, a business phone number, an image query. The user
decided, in the Track A session on 2026-10-01, that collection works this way:

```text
broad product capture → security-only hard filter → ICBM canonical extraction
→ what ICBM does not need is dropped → only canonical facts and evidence are stored
```

- **Inside the product scope, product data and its evidence are collected as broadly as
  possible.** The capture goes on as it arrived.
- **Only what must never be collected refuses, fail-closed, at the capture step:**
  credentials, tokens, cookies, sessions, login information, user input values and obvious
  personal information. The server's security gate (`app/stages/collect/extension/gate.py`)
  refuses:
  - a secret: an attribute named for a token, session, cookie, credential or signature; a value
    shaped like a JWT, a bearer token, a secret `key=value` parameter or a long hex secret; a URL
    carrying credentials; an image reference with a secret query key;
  - the signed-in member's own account and identity: a Cafe24 member variable
    (`xans-member-var-*`), a Cafe24 my-shop module (`xans-myshop-*`), or an account, my-page,
    login or user-info region.
- **User input values never arrive.** The `BrowserCapturePolicy` keeps no `value` or `name`
  attribute and no `textarea`, and one that arrives is a policy violation.
- **A supplier's or maker's business contact is product data** (the user's decision). A phone
  number or e-mail in the product information is collected; only the signed-in member's own
  contact is personal.
- **A member price is product data.** A `회원가` label, or an element whose class begins with
  `member`, is collected. The private-region naming rule of the Adaptive capture owner does not
  decide what the extension may collect.
- **Everything else that only looks private** — an image reference with an ordinary query,
  fragment or odd shape — goes on as well. Each such item is recorded as a note (a kind and a
  boundary, never a value) in the run's log.
- **What ICBM does not need is dropped at extraction.** The supplier's canonical extractor takes
  only the fields ICBM defines. Only canonical facts and their evidence are ever stored, in the
  `ProductFactsRevision` the extractor writes (E2), never the capture.
- **An empty image reference is no reference**, as the supplier's image owner reads it (a Cafe24
  lazy-load `<img>` leaves `src` empty); it is neither a finding nor a note.
- A known identity widget is better cut in the browser than refused on the server: the KM policy
  `kmretail-capture-2` cuts the member benefit box.
- The Adaptive capture owner's own sanitizer and final scan
  (`app/stages/collect/adaptive/engine/capture.py`) are unchanged for the Adaptive Phase C path.

This supersedes owner amendment `5909645067` §1 (a capture the sanitizer had to clean fails its
run) for the extension transport.

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

### 8.1 The E3 queue contract (the user's instruction, 2026-10-02)

The user ordered E3 after the security gate (§6.1) merged. This section is the contract E3 is built
and audited against. It adds no read, host, path or query to any supplier and changes no E1 or E2
rule.

**Discovery reads nothing.**
- The operator opens a supplier list page in their own Chrome and asks the side panel to find its
  products. The extension reads only that already-loaded page: its anchors, in document order.
- **Only a product URL leaves the browser.** The server gives the extension the supplier's reviewed
  product path form from its `CollectionProfile`. The extension sends a link only when it is on the
  storefront host and its path fully matches that form, and it sends only its scheme, host and
  path. No other link, and nothing of the list page itself (its URL, HTML or anchor text), is sent
  or kept, and no credentials, query or fragment ever are. A product link carries what a single
  click on that product would already send (E1), and nothing more. A product named only in a query
  is not discovered.
- **The server judges every link again.** A link is a queue candidate only if `check_target`
  accepts it as a product read of that supplier and the §6.1 secret rules find nothing in it.
  Links are deduplicated by the product the URL names. A refused link is counted, never stored or
  logged by value. The browser filter keeps material in the page; it never decides what is read.
- No list-page topology is added to a supplier. Reconnaissance never observed one, and a value is
  never added because it seems likely. A later, observed list region may narrow discovery; it may
  never widen it.

**The bounds are declared, twice, and never defaulted.**
- The supplier's `CollectionProfile` declares its queue limits (it owns pacing, AC-11):
  - the most links one discovery may submit;
  - the most products one queue may hold;
  - the shortest interval between two queue reads. It is never below the supplier's request
    interval or the extension ingest interval;
  - how long an issued read may stay open.

  A supplier without them has no list queue: discovery and every queue call refuse fail-closed.
- The operator declares each queue's own bounds in the side panel: the number of products and the
  interval between them. An absent or out-of-range value refuses the queue before it exists. There
  is no "unlimited", and nothing falls back to a code default.
- Concurrency is one: a supplier has at most one issued, unsettled queue read. It is serialized
  with the single-click path by the existing one-pending-extension-run rule.

**Every read is server-issued work, reserved durably before it happens.**
- The extension asks the server for the next item. The server answers with exactly one of:
  - **wait**, with the seconds left: the queue interval, the same-product interval or an unsettled
    run;
  - **issue**: one item, its product URL and a random, single-use ticket. The issue is written
    before the answer is sent, and it counts against the queue's budget;
  - **done**: the queue is finished, cancelled or stopped.

  The extension's clock never decides.
- **The same-product interval of ADR-0010 §4 holds for queue reads.** Before an issue it counts
  every read of the product: the server's own reads, issued queue reads and extension captures. A
  single click stays the operator's own read under the E1 rules and is never refused by it.
- One item is one read. An item is never reissued or retried. An issued item that is never captured
  expires after the declared time and still counts.
- **The queue stops, and never skips forward, at the first item that does not end `RECORDED` or
  `NO_REVISION`.** That covers a refused capture, an expired ticket and a `FAILED` run. The operator
  decides what follows; a new queue is a new declaration.
- The operator may pause, by not asking, or cancel. Cancelling settles every unissued item.

**Every product is an ordinary run.**
- The extension navigates the operator's own tab to the issued URL, captures it with the unchanged
  E1 capture, and sends it through the unchanged ingest with its ticket. The server accepts a
  ticketed capture only for that item's exact URL, once, before the ticket expires.
- From there it is the E2 pipeline: the §6.1 security gate, the KM extractor and a
  `ProductFactsRevision`. An E3 run differs from a single-click run only in naming its queue item.
- **"Skip collected products"** is an operator choice per queue. It skips, without a read, a product
  that already has a `RECORDED` run for the supplier.

**State and UI.**
- The queue and its items are durable server state, so a restart loses no issued read. An item has
  its own state axis: waiting, issued, captured, skipped, expired or cancelled. This axis is never a
  run outcome. A captured item names its run, and the run keeps its own outcome, facts status and
  code (§12.3).
- The side panel shows discovery and queue progress (§12.1) as the approved prototype board
  `확장 — 목록 발견과 대기열` draws them, with two corrections where a canonical rule wins:
  - a row's chip is the item state or the run outcome, never `REVIEW` (AC-21);
  - there is no supplier-session indicator the extension cannot know.
- Collection Management shows every E3 run as it shows any run.

**The real acceptance needs its own grant.** It needs one list page, a queue no larger than the
user grants, and the declared interval, with the user's own click. Until then the slice runs only
against local fixtures, with zero supplier reads.

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
separately: **`documents/contracts/ui/UI_SOURCE_OF_TRUTH.md` is not changed by this ADR**. It switches only when the
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
AC-13  A final-scan finding is never cleared by an href or anchor-text exception after a whole-page capture; security material (a secret, or the signed-in member's own account and identity) inside the product scope still refuses fail closed, and other product data is captured broadly (§6.1)
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
AC-26  E1 exposes no general BrowserCapturePolicy editor, only the diagnostics the bounded KM single-click acceptance requires; documents/contracts/ui/UI_SOURCE_OF_TRUTH.md changes only when an approved prototype revision and its fingerprint are recorded under its own process
AC-27  List discovery reads only the operator's already-loaded page and sends only the scheme, host and path of links that fully match the supplier's reviewed product path form, nothing of the list page itself; the server judges every link with check_target and the secret rules, and no list-page topology is added without reconnaissance (§8.1)
AC-28  A list queue exists only when the supplier's CollectionProfile declares its queue limits and the operator declares the queue's size and interval within them; a missing or out-of-range bound refuses before any read
AC-29  Every queue read is server-issued work, written durably before the read and counted against the queue budget, never reissued or retried; the same-product interval counts server reads, issued queue reads and extension captures
AC-30  A queue stops, never skipping forward, at the first item that does not end RECORDED or NO_REVISION; every queued product is an ordinary EXTENSION run through the unchanged ingest, security gate and extractor
AC-31  A queue item's state is its own axis and never a run outcome; the side panel shows no REVIEW chip and no supplier-session indicator the extension cannot know
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
- `documents/architecture/ARCHITECTURE.md` §4 COLLECT; `documents/architecture/GLOSSARY.md` §3a; `documents/roadmap/ROADMAP.md` §14.3.
- Code facts, at main `4ba99fbe`:
  - F1: ADR-0010 §3 names server-side browser execution;
  - F2: `integrations/suppliers/collection.py` `CollectionProfile.__post_init__` and
    `integrations/suppliers/base.py` `SupplierTransport`;
  - F3: ADR-0017 has no `acquisition_mode`;
  - F4: the `CollectionProfile` fields.
