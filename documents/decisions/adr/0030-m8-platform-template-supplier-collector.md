# ADR-0030 — M8 supplier expansion: platform templates and per-site configuration

Status: **ACCEPTED** 2026-10-10. This is the M8 kickoff contract for supplier expansion (ROADMAP §9, Phase 6). A new wholesale supplier is added by naming its storefront platform and writing one reviewed site configuration. It needs no supplier-specific parser code.

This ADR lands before its code. Each implementation slice of §11 cites it as canon.

Decision owners:
- **Product direction:** the owner, in Issue #219 comment `6085702239`:
  - M8 continues supplier expansion; marketplace expansion (Coupang) is Track C's work.
  - A supplier is added by *platform template + site configuration*, not by a per-site adapter, because there are tens to hundreds of wholesale sites.
  - "Only the data we actually need" is collected, in the shape the canonical structure already uses.
  - The first sites are U-PICK (`https://upickb2b.com/`, Cafe24) and 건강산 (Godomall). KM통상 is also Cafe24.
- **Already decided by canon:**
  - ADR-0007: the CONNECT `SupplierDefinition`.
  - ADR-0010: the COLLECT port, the access envelope, reconnaissance before freezing hosts and selectors (§5), the `ProductFactsRevision` contract (§6, §7), the evidence model (§8), source images (§9), extraction identity (§12).
  - ADR-0019: the extension-primary capture transport and the per-supplier browser capture policy.
  - ADR-0024 §2: owner-declared seller-code conventions.
  - ROADMAP §9 "Expansion gate": a new adapter cannot change the canonical Product, Pricing or Operation contracts.
- **Implementation choices:** the implementation agent, under ADR-0022 and rule 14.

What it authorizes:
- this contract;
- the provider-zero slices of §11;
- the bounded reconnaissance of §8 for U-PICK and 건강산, **each only after the owner's go-ahead for that site**.

What it does not authorize:
- **No change to a canonical contract.** The fact fields, `FieldStatus`, `EvidenceKind`, the Product, Pricing and Operation contracts and the `ProductFactsRevision` shape stay as they are.
- **No second currency.** Every site here is KRW (ROADMAP §14.3).
- **No AI or OCR** in extraction.
- **No change to KM통상's collection.** Its package, its extraction identity `kmretail-3` and its source identities stay as they are (§10).
- **No credential handling by the agent.** The owner types each supplier account in the settings UI.
- **No supplier order write.** Supplier ordering stays the operator's own act (ADR-0025).

Sources: ROADMAP §9, §14.3; ADR-0007; ADR-0010; ADR-0017 §1, §12; ADR-0019; ADR-0024 §2.

Recorded by: Claude Code (Track B). The number was confirmed free on main and on every open branch.

Date: 2026-10-10

---

## Context

The first vertical closed with M7 on one supplier, KM통상. Its collection knowledge is the package `integrations/suppliers/kmretail/`:
- about 1,100 lines of Python: identity, facts, images, DOM;
- a profile: hosts, paths and limits;
- a CONNECT definition;
- a browser capture policy.

Written that way, each further supplier needs a new reviewed package, a new reconnaissance and a new extraction identity. At tens to hundreds of wholesale sites, that never finishes.

Most Korean wholesale storefronts are not custom software. They run on a small number of hosted shop platforms: Cafe24, Godomall, MakeShop and a few others. A platform renders every shop the same way:
- the same login form and logged-on markers;
- the same product URL form;
- the same product-module class names;
- the same option widget;
- the same description container.

Shops on one platform differ mainly in their host names, in the labels of their information table (for example `공급가` against `도매가`), and in which skin regions they use.

KM통상's parser is already a Cafe24 parser in all but name: its docstrings cite `xans-product-*`, `ec-data-src`, `#prdDetail`, `xans-layout-statelogon` and `/exec/front/Member/login/`. These are Cafe24's tokens, not KM's.

ADR-0017 describes a more general route: an Adaptive Collector interpreting per-supplier profile revisions stored in the database, with shadow validation. Its production slices, its Phase C and its second-supplier Phase D are not authorized, and Phase D stays deferred. This ADR does not take that route. It needs no new schema and no shadow owner, and it leaves ADR-0017 unchanged as a possible later path.

## Decision

### 1. Three layers

| layer | holds | lives in | changes by |
| --- | --- | --- | --- |
| **Fixed data shape** | the existing fact fields (§2), `FieldFact`, `FieldStatus`, `EvidenceKind`, image roles | `app/stages/collect/facts.py`, `integrations/suppliers/collection.py` | an ADR only (unchanged here) |
| **Platform template** | the parser for one storefront platform: identity rule, field extractors, image-role rules, the CONNECT predicates and the default capture policy | `integrations/suppliers/platforms/<platform>/`, repository code with its own extraction identity | a reviewed PR that advances the platform revision |
| **Site configuration** | one supplier: key, name, platform, hosts, paths, limits, label and region overrides, status | `integrations/suppliers/sites/<supplier_key>.json`, reviewed data, no code | a reviewed PR that advances the site revision |

- A **site** is one supplier on one platform. It has no Python of its own.
- A **template** is shared by every site on its platform. Fixing a template fixes every site on it.
- A site whose pages a template cannot read is **not** fixed with site code. Either the template gains a generic, platform-wide rule, or the site waits for its own template (§9).

The first two templates are `cafe24` and `godomall`.

### 2. The fixed data shape

A template returns exactly the fields KM통상's parser returns today, with the same value types, statuses and evidence. It returns no other fields.

| field | level | value |
| --- | --- | --- |
| `original_name` | CORE | `TextValue` |
| `prices` | CORE | `PricesValue`: sale, consumer, list and supply prices as the page labels them |
| `options` | CORE | `OptionsValue` |
| images (`IMAGES_FIELD`) | CORE | `ImagesValue`, through the role rules |
| `stock` | CORE | `StockValue` |
| `shipping` | COVERAGE | `ShippingValue` |
| `minimum_sale_price` | COVERAGE | `MoneyValue`; a row that says the price is free (`자율`) is `ABSENT`, as in `kmretail-3` |
| `quantity_tiers` | COVERAGE | `QuantityTiersValue` |
| `brand`, `manufacturer`, `origin` | COVERAGE | `TextValue` from a labelled row |
| `notice` | COVERAGE | `NoticeValue` |
| `detail_description` | COVERAGE | `TextValue` |

These rules hold for every template:
- **Fail closed.** A value the page does not state is `ABSENT`. A value the template reads but cannot trust is `REVIEW_REQUIRED`. A template never guesses a value to fill a field.
- **Same evidence.** Every confirmed value carries the page's own words as evidence, exactly as KM통상's facts do.
- **One identity rule per platform**, frozen in the template (§4). A site cannot replace it.

Everything after COLLECT reads the same revisions it reads for KM통상: the canonical Product DB, pricing, the registration preparation, REGISTER and OPERATE. Nothing downstream learns which platform a revision came from.

### 3. The site configuration

A site configuration is one JSON file, schema `icbm-supplier-site/v1`, read and validated at start-up. An invalid file stops the build from offering that site; it never stops the application.

| key | meaning | rule |
| --- | --- | --- |
| `supplier_key` | the stable key, for example `upick` | lower-case ASCII; never reused; equals the file name |
| `display_name` | the name the screens show, for example `U-PICK` | |
| `platform` | `cafe24` or `godomall` | a template that exists |
| `base_url` | the storefront origin | `https` only |
| `storefront_host` | the product-page host | must be the host of `base_url` |
| `image_hosts` | the hosts product images may be fetched from | explicit names only, each **observed** in reconnaissance; never a wildcard |
| `extra_egress_hosts` | any host CONNECT needs besides the template's own | observed in reconnaissance |
| `product_path_form` | which of the template's named product-path forms this site uses | from the template's closed list; never a free regular expression |
| `label_overrides` | extra row labels for a field, for example `{"prices.supply": ["도매가"]}` | keys from the template's closed list of label slots; values are plain words, not patterns |
| `region_overrides` | where this skin puts a region the template looks for, for example the description container | `{"by": "id" \| "class", "token": "..."}` only, from the template's closed list of region slots |
| `limits` | per-image bytes, image references, requests and bytes per run, same-product interval, queue bounds | defaults are the template's; a site may lower them; raising one above the template default needs the owner's decision |
| `seller_code_convention` | the owner-declared seller management code form (ADR-0024 §2), for example `UP{source_product_id}` | optional; only the owner declares it |
| `status` | `RECON` or `ACTIVE` (§7) | |
| `recon_record` | the reconnaissance record that justified the hosts and overrides | required for `ACTIVE` |
| `revision` | the site revision, for example `upick-1` | advances on every semantic change |

What a site configuration **cannot** hold:
- code, a regular expression, a script or a selector language beyond the `by`/`token` form;
- a credential, a cookie or an account name;
- a fact value;
- a host that reconnaissance did not observe.

### 4. Platform templates

A template is a supplier package under the existing repository rules: site knowledge only, pure functions over a captured document, no network, browser or logging import, and an extraction identity pinned by `extraction_identity.py` (ADR-0010 §12). It provides:

- the identity rule;
- the field extractors, reading its label slots and region slots;
- the image-role rules (positive selection; anything unrecognised is `UNKNOWN` and never fetched);
- the closed lists of product-path forms, label slots and region slots;
- the CONNECT login form and the authenticated and login-required predicates;
- the default limits and the default browser capture policy (ADR-0019), which a site specialises only by host and region overrides.

**`cafe24`** is derived from KM통상's parser, generalised: its label tables become the template's default slots, and its region tokens become default regions.
- Identity: `<meta property="product:productId">`, corroborated by `product:retailer_item_id`, the canonical link and the URL path, all equal. This is KM통상's rule, unchanged.
- CONNECT: the form `/exec/front/Member/login/`, logged on only with both `xans-layout-statelogon` and the logout action. Also unchanged.
- Product paths: `/product/<name>/<number>/` with the optional listing segments.

**`godomall`** is written from 건강산's reconnaissance captures (§8). Its identity rule, its CONNECT predicates and its regions are frozen only from what those captures show. Godomall product pages are normally addressed as `/goods/goods_view.php?goodsNo=<number>`. If the captures confirm it, the template's single safe query key is `goodsNo`, digits only, and every other query key fails closed.

**A site's extraction identity** is the pair *(platform revision, site revision)*. A change to either advances it, so a revision records exactly which rules produced it.

### 5. CONNECT

CONNECT uses the template's login form and predicates with the site's `base_url`, as KM통상 does today:
- The **credentials** are the owner's. The owner types them in the settings UI under the site's `supplier_key`. They go to the operating system's secret store, as KM통상's do. The agent never enters, reads or relays a credential.
- **One account per site.** A site that also needs an approval or a membership grade from the supplier says so in the connection test's result. It is never bypassed.
- A site in `RECON` may connect, so the owner can test the account before any collection.

### 6. COLLECT

COLLECT is unchanged:
- the gateway, pacing, request budget, image fetch, asset store, revision store, job, audit and the extension transport;
- the operator's one click and the list queue (ADR-0019).

What changes is only where the answers come from: `COLLECTIONS` is built from the site configurations, each bound to its template.

The browser extension learns the site hosts from the server's registry, as it already learns the capture policy, instead of from its own constant. Its manifest's host permissions list each configured `storefront_host`, and a repository test keeps the two lists equal. Adding a site therefore means reloading the extension once in Chrome.

### 7. Site status

| status | CONNECT | COLLECT | registration preparation |
| --- | --- | --- | --- |
| `RECON` | yes | yes, revisions are written as usual | **refused**: `SUPPLIER_NOT_ACTIVE` |
| `ACTIVE` | yes | yes | yes |

A `RECON` site's revisions are real, immutable history. They cannot reach a marketplace while the site is in `RECON`. When the configuration or the template is corrected, the next collection appends a new revision under the new extraction identity, and the old one stays as history.

A site becomes `ACTIVE` only through a reviewed PR that flips `status` and cites its acceptance record (§8.3).

### 8. Reconnaissance and acceptance, per site

#### 8.1 Reconnaissance (owner go-ahead per site)

Reconnaissance needs the owner's go-ahead for that site. It runs on the pattern of ADR-0010 §5:
- **Who signs in:** the owner, in a browser the agent can read: the desktop app's built-in browser, or the owner's Chrome through the extension. The agent never types the credential.
- **What is read:** at most 6 product pages, chosen by the owner or from one listing page, plus `robots.txt` and the terms page. Images are fetched only within the template's default caps.
- **What is kept:** the product-page captures, scrubbed of anything that names the member (the greeting, the member name, the grade, points and coupons). These become the template's or site's test fixtures. No order or member page is kept.
- **What is recorded:** the observed hosts, the product-path form, the label wording, the regions, and whatever does not fit the template. The record is `documents/acceptance/suppliers/<supplier_key>-recon.md`.

#### 8.2 Provider-zero proof

The site's configuration, and for 건강산 the `godomall` template, are written from the captures. They are proven offline on those captures:
- every CORE field `CONFIRMED`, or a stated reason why it cannot be;
- no fact is confidently wrong against the page as the owner sees it;
- a structurally broken capture yields `REVIEW_REQUIRED` or an unresolved identity, never a confident fact.

#### 8.3 Acceptance

After the owner's go-ahead, the site is collected through the application's own route, in `RECON`:
- 5 products the owner names, chosen for diversity: at least one product with options, and a sold-out product if one exists;
- each fact is checked against the source page;
- the bar: **confidently wrong facts = 0**; every CORE field `CONFIRMED` or explicitly `REVIEW_REQUIRED`; every read within the caps; the same verdicts on a repeat from a fresh server process.

The record is `documents/acceptance/suppliers/<supplier_key>.md`. The status flip to `ACTIVE` cites it.

### 9. What a template does not cover

These are not added through a site configuration:
- a site whose product facts arrive only through a script-loaded API response, or only after browser rendering;
- a site in a currency other than KRW;
- a site whose identity cannot be read in the template's rule.

Each needs a new or extended template, under a reviewed PR that cites this ADR. A third template (for example MakeShop) is an implementation choice under ADR-0022, once a site that needs it has been reconnoitred with the owner's go-ahead.

### 10. KM통상

> **Amendment note (2026-10-10, owner decisions).**
> - The owner set the per-image byte limit to 5 MiB for every supplier, KM통상 included (Issue #219 `6086299406`). It is a profile limit, not a parser rule. KM통상's extraction revision and source identities are unchanged by it.
> - ADR-0031 C1 and ADR-0032 P1 later advance KM통상's parser to `kmretail-4`, by their own decisions.
> - PT-10 is read with these owner decisions.

KM통상 keeps its own package, its revision `kmretail-3` and its 40 source identities. Its adopted listings, its common-image decisions (Issue #219 §1, 2026-10-03) and its seller-code convention `KM{source_product_id}` keep their supplier key `kmretail`.

The `cafe24` template is proven against KM통상's retained captures as a fixture test: the same identities, and the same facts or a stated difference. A later slice may move KM통상 onto the template only if every retained capture yields identical identities and facts. That move is optional and is not part of M8.

### 11. Slices

| # | slice | real reads |
| --- | --- | --- |
| S0 | this ADR | 0 |
| S1 | site-configuration owner, schema and validation; the template seam; `cafe24` derived from KM통상's parser; `COLLECTIONS` and `SUPPLIERS` built from configurations; the KM통상 fixture equivalence test | 0 |
| S2 | KM-specific constants generalised to the registry: the container's image-slot table, the adoption seller-code conventions, the extension's host map and manifest, the screens' supplier lists, the acceptance guard; the `RECON` refusal in registration preparation | 0 |
| S3 | U-PICK: reconnaissance (§8.1), then its site configuration in `RECON` and the offline proof | the reconnaissance, after the owner's go-ahead |
| S4 | 건강산: reconnaissance (§8.1), the `godomall` template, its site configuration in `RECON` and the offline proof | the reconnaissance, after the owner's go-ahead |
| S5 | acceptance of each site (§8.3) and its `ACTIVE` flip | the acceptance collections, after the owner's go-ahead |

S3 and S4 may run in either order. Their reconnaissance may run as soon as the owner signs in.

### 12. ROADMAP §14.3

ROADMAP §14.3 says a second supplier "is meant to be onboarded through the Adaptive Collector". With this ADR, a supplier on a platform that has a template is onboarded through the template and a site configuration instead. ADR-0017 is not amended: its Adaptive route, its Phase C and its Phase D stay as written, deferred, and unauthorized.

## Invariants

- **PT-01** A site configuration contains no code, regular expression, credential or fact value.
- **PT-02** Every host a site may reach was observed in its reconnaissance record. No wildcard host exists.
- **PT-03** A template returns only the fields of §2, with the existing value types; no canonical contract changes for a site.
- **PT-04** A site cannot replace its template's identity rule.
- **PT-05** A revision's extraction identity names both the platform revision and the site revision.
- **PT-06** A `RECON` site's product is refused by registration preparation with `SUPPLIER_NOT_ACTIVE`.
- **PT-07** `ACTIVE` is set only by a reviewed PR that cites the site's acceptance record.
- **PT-08** No real supplier read happens without the owner's go-ahead for that site; the agent never types a credential.
- **PT-09** Reconnaissance captures kept as fixtures carry no member-identifying text and no order data.
- **PT-10** KM통상's package, revision and source identities are unchanged by M8 unless a later slice proves identical output.
- **PT-11** The extension's host permissions equal the configured storefront hosts.
- **PT-12** A raised limit above the template default exists only with the owner's recorded decision.

## Consequences

- Adding a Cafe24 or Godomall wholesale site is a reconnaissance plus one JSON file. It needs no parser code and no new audit of extraction logic.
- A template fix reaches every site on its platform at once. A template change is therefore reviewed as carefully as a parser change, and it advances every dependent site's extraction identity.
- Sites that render facts by script, or that use an unsupported platform, wait for a template. This is stated, not hidden.
- KM-specific constants in the application become registry-driven (S2). This touches the screens and the extension, but no canonical contract.

## References

- ROADMAP §9, §12, §14.3
- ADR-0007 (CONNECT), ADR-0010 (COLLECT), ADR-0017 (Adaptive Collector), ADR-0019 (extension transport), ADR-0022 (operating authority), ADR-0024 §2 (seller-code conventions), ADR-0025 (fulfillment)
- Issue #219 `6085702239` (M8 owner direction)
- `integrations/suppliers/kmretail/` (the parser the `cafe24` template is derived from)
