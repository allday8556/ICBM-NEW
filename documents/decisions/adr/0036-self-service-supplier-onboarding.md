# ADR-0036 — Self-service supplier onboarding: add, analyse, set words and approve a template-platform supplier on screen

Status: **ACCEPTED** 2026-10-11. This is the contract for operator-driven onboarding of a wholesale supplier that runs on a platform ICBM already has a template for (ADR-0030: Cafe24, Godomall). It lands before its code. Each slice of §11 cites it as canon.

Decision owners:
- **Product direction:** the owner, in Issue #219.
  - `6100405361`: self-service onboarding is Track A's work. The inert v29 controls of 수집관리 → 공급처 관리 (「+ 공급처 추가」, 「사이트 분석」, 「수집 규칙」) become working, so a supplier can be added without an agent working on each site.
  - `6100495552`: the design choices D1–D4 of §2.
- **Already decided by canon:**
  - ADR-0007: CONNECT, the credential record in the OS secret store, encrypted sessions, the egress grant per supplier.
  - ADR-0010: COLLECT, the access envelope, the `ProductFactsRevision` contract, the evidence model, pacing.
  - ADR-0017: profile data as immutable DB revisions; validation on operator-verified samples; operator-saved drafts; AI only ranks or proposes; samples kept locally while referenced.
  - ADR-0019: the extension capture transport and its per-supplier capture policy.
  - ADR-0030: platform templates, the site configuration and its word rules (PT-01), `RECON` and `ACTIVE`.
  - ADR-0035: the reading rules learned on U-PICK and 건강산, and the line between word fixes and structural fixes.
- **Implementation choices:** the implementation agent, under ADR-0022 and rule 14.

What it authorizes:
- this contract;
- the slices of §11;
- the amendments of §10, each effective when the slice it names lands.

What it does not authorize:
- **No new platform.** A site on any platform other than the template platforms stops at "a template is needed". A new template remains an agent PR under ADR-0030 §4.
- **No change to repository sites.** KM통상, U-PICK and 건강산 keep their reviewed site configurations, statuses and identities.
- **No AI.** Candidate words come from rules (D4). AI proposals are a later stage under ADR-0026.
- **No change to a canonical contract.** The fact fields, `FieldStatus`, `EvidenceKind`, `ProductFactsRevision` and the Product, Pricing and Operation contracts stay as they are.
- **No raised limit.** Pacing and limits are the template defaults; raising one still needs the owner's recorded decision (ADR-0030 PT-12).
- **No LIVE.** Onboarding reads suppliers only, as COLLECT does. It never writes to a supplier or a marketplace.

Recorded by: Claude Code (Track A). The number was confirmed free on main and agreed with Track B.

Date: 2026-10-11

---

## 1. Two kinds of sites

- **A repository site.** It is defined by a reviewed `integrations/suppliers/sites/*.json`, exactly as today: ADR-0030, with acceptance records and `ACTIVE` flip PRs.
- **An onboarded site.** The operator creates it in the UI. Its configuration lives in the database as append-only revisions (§3).

Both bind to the same templates through the same `SiteConfig` shape, so collection, facts and identity work identically. A key, a host or a supplier name is never shared between the two kinds.

## 2. The owner's choices (`6100495552`)

- **D1. Hosts are added from the UI, with guards.**
  - Only an exact `https` host; no wildcard, IP literal, `localhost`, private or link-local address, port or path.
  - Only hosts observed while analysing that site (§5).
  - Confirmed by the operator and audited; disable-able at any time.
  - Limits stay at the template defaults.
- **D2. `ACTIVE` by on-screen approval.**
  - It needs **3** distinct sample products read under the current words revision, each with every core field read (no core field `REVIEW_REQUIRED`).
  - The operator verifies each sample's facts as correct.
- **D3. Chrome optional host permissions.** When a site is approved, the extension asks Chrome for that site's origin, so no reinstall is needed.
- **D4. No AI in this version.** Candidate words are proposed by rule, and the operator confirms every word.

## 3. Records (one migration, `0060`)

All writes go through one owner store with an injected clock and an audit event per write. Every table refuses UPDATE and DELETE except where a pointer column is stated.

| table | holds |
| --- | --- |
| `onboarded_suppliers` | Identity: the supplier key (`site-<slug>`, never a repository key), display name, `https` origin, template (`cafe24` / `godomall`), created_at. |
| `onboarding_transitions` | Append-only state log: `DRAFT` → `RECON` → `ACTIVE`; `DISABLED` from any state; `ACTIVE` → `RECON` on a words change (§7). Each row records the actor, the reason and the evidence ids. The current state is the newest row. |
| `onboarding_hosts` | One row per confirmed host: host, role (storefront / login / image), the trial read that observed it, confirmed_by, confirmed_at. A disable is a new row. |
| `onboarding_word_revisions` | `seq`-numbered words per template slot, checked by ADR-0030 PT-01 and the `site_config` word rules, plus a fingerprint. |
| `onboarding_policy_reads` | The robots.txt and terms reads shown at add time, and the operator's acknowledgement. |
| `onboarding_trials` | A trial read (§5): sample URL key, template revision, words revision, the local sample capture's SHA-256, per-field read status, the unread rows and the observed hosts. **Never a `ProductFactsRevision`.** |
| `onboarding_verifications` | The operator's per-sample verification of a trial's facts, with the fingerprint of what was shown. |

**Credentials** stay the existing `supplier:<key>:credentials` record in the OS secret store, and sessions stay encrypted (ADR-0007). They are never stored in these tables.

## 4. 「+ 공급처 추가」

1. **Entry.** The operator enters the name, the storefront origin (`https://…`) and the login ID and password. The origin's host is the first confirmed host (D1). The operator's entry is the per-site go-ahead (ADR-0030 PT-08). The agent never types a credential.
2. **Platform detection.** One read of the storefront's home page plus each template's login path decides `cafe24`, `godomall` or *unsupported*. It reads markers only, never facts. The operator confirms the detected platform. *Unsupported* stops here with "a template is needed".
3. **Policy reads.** robots.txt and the template's terms path are read (ADR-0010 recon caps: at most 3 policy reads). They are shown, and the operator acknowledges them.
4. **CONNECT.** The connection test runs on the template's login and probe paths, exactly as for a repository site.
5. **State.** The site is now `RECON`: it can be collected, but registration preparation refuses it with `SUPPLIER_NOT_ACTIVE` (ADR-0030 PT-06).

**Runtime binding.**
- The supplier registry merges the repository sites with the onboarded sites when the process starts.
- An onboarding change (a new site, a host, words or state) rebinds that one supplier in the running process. No restart is needed.
- The extraction identity is `<template revision>+<site key>-w<words seq>`, so every words revision is a distinct identity (ADR-0030 PT-05).

## 5. 「사이트 분석」 (trial reads)

1. **Samples.** The operator gives up to 5 sample product URLs on the site's confirmed storefront host.
2. **Reading.** Each sample is read once, as one product page. The read uses the site's CONNECT session, the template's path form and the template's pacing (60 s for the same product, 10 s between reads).
3. **Local capture.** The page is kept as a local sample capture (ADR-0017: local data root, kept while a trial references it, no member-identifying text kept).
4. **Extraction.** The template extracts facts with the current words revision. Images are not downloaded; their URLs and hosts are recorded.
5. **The report shows:**
   - each field: read, absent or not read;
   - the **unread rows**: labels or phrases on the page that no slot matched, with the candidate slot proposed by rule (D4);
   - the **observed hosts** (image, login and asset) for confirmation (D1).
6. **Re-analysis after a words change** runs offline on the stored captures. It makes no new supplier read.

**A trial never writes canonical product truth.** It writes an `onboarding_trials` row, not a `ProductFactsRevision`, so a product appears in 통합DB only through a normal collection after approval.

**Structural problems** are flagged "a template change is needed". These are problems that words cannot fix (ADR-0035): an unreadable price block, a duplicated order list, a script-rendered page. Such a site cannot be approved until a template PR lands.

## 6. 「수집 규칙」 (words)

- The operator confirms or edits the words of each template slot. A save appends a words revision, and only words pass: no code, regex, credential, host or fact value (ADR-0030 PT-01 and the `site_config` word rules).
- The screen shows the template's default words beside the site's words, plus the unread rows each word now matches when the stored captures are re-analysed.

## 7. Approval to start (D2)

**Conditions.** 「사용 시작」 is enabled only when all of these hold:
- the site is `RECON`;
- at least **3 distinct** samples have trials under the **current** words revision with every core field read;
- the operator has verified each of those 3 trials' facts as correct;
- no structural problem is open.

**On approval.** It appends `ACTIVE` with the evidence ids, audited.

**After approval.**
- A later words change moves the site back to `RECON` until 3 samples are re-verified.
- 「비활성화」 appends `DISABLED`: no collection, no registration preparation, and the egress grant is withdrawn.

## 8. The extension (D3)

- **The manifest.** `host_permissions` stay exactly the repository storefront hosts. The manifest declares `optional_host_permissions: ["https://*/*"]`, which are never requested in bulk.
- **Requesting a host.** When an onboarded site is `ACTIVE` and listed by the server, the extension's own page asks Chrome for **that origin only**, on the operator's click (`chrome.permissions.request`). The capture policy covers the origin only once it is granted.
- **Disabling.** A disabled site's origin is removed (`chrome.permissions.remove`).

## 9. Safety

- **Egress.** The egress grant for an onboarded supplier is exactly its confirmed, non-disabled hosts. The guard in §2 D1 refuses any other address before a grant exists.
- **Pacing and budgets.** These are the template defaults. A collection still reads exactly one product page.
- **Audit.** Every write is audited, and audit rows never carry credentials or page text.
- **Data from a trial.** It stays local and non-canonical.

## 10. Amendments, each effective when its slice lands

- **ADR-0010** "The host allowlist is never widened at run time" and **ADR-0017 AC-02** stay true for repository sites. An onboarded site's allowlist is widened only by an operator-confirmed host record under §2 D1 (slice O2).
- **ADR-0030 PT-02.** An onboarded site's hosts are those observed in its trials and confirmed by the operator (O2/O3).
- **ADR-0030 PT-07 and §7.** An onboarded site becomes `ACTIVE` by the on-screen approval of §7. Repository sites keep the reviewed-PR flip (O5).
- **ADR-0030 PT-11 and ADR-0019 E1 (`test_extension_e1_contract`).** The extension additionally declares `optional_host_permissions` and holds an onboarded origin only after the operator grants it (O5).

## 11. Slices

| slice | scope |
| --- | --- |
| O1 | The owner store and migration `0060`. The registry merges repository and onboarded sites, with a runtime rebind. No UI. |
| O2 | 「+ 공급처 추가」: the host guard, platform detection, the policy reads, credentials and the connection test. The site becomes `RECON`. |
| O3 | 「사이트 분석」: trial reads, local captures, the read/unread report, observed-host confirmation, and offline re-analysis. |
| O4 | 「수집 규칙」: the words editor and its revisions. |
| O5 | Approval and disabling: verifications, `ACTIVE`, the D3 extension permissions and the §10 amendments. |
| O6 | Acceptance: one real onboarding on a Cafe24 or Godomall site that the owner names, with the owner's go-ahead. |

## Invariants

- **OB-01** An onboarded host is an exact `https` host that a trial observed and the operator confirmed. It is never a wildcard, an IP literal, a loopback, private or link-local address, a port or a path.
- **OB-02** A trial never writes a `ProductFactsRevision` or any canonical product truth.
- **OB-03** An onboarded site's configuration holds words only, and every words revision is a distinct extraction identity.
- **OB-04** `ACTIVE` needs 3 distinct samples with every core field read, each verified by the operator, under the current words revision. A words change returns the site to `RECON`.
- **OB-05** Credentials stay in the OS secret store, typed by the operator, and never appear in a table, log or audit row.
- **OB-06** Pacing and limits are the template defaults, and a collection reads exactly one product page.
- **OB-07** Repository sites, their identities and their review rules are unchanged.
- **OB-08** The extension holds an onboarded origin only after the operator grants it, and never requests a host in bulk.
- **OB-09** Onboarding works without AI.
