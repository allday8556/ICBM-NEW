# Adaptive Collector — Phase B prototype (disposable)

Status: **PROTOTYPE, revision 2. It lives only on a draft PR: it is never merged and never
promoted.**
Author: Claude Code
Issue: #110. Phase B kickoff `5820294346`, under ADR-0017 §13. Revised for architect audit
`5309150430`.
Base: main `c509c6236aeaddd76f7553071bcc1ee0799eeb19`

This directory proves the core ADR-0017 behaviours on **synthetic fixtures only**. It is not
production code, and it is never copied into production. Production code is written fresh, after
its own authorization.

## Isolation (ADR-0017 §13)

| rule | how it holds | proof |
| --- | --- | --- |
| every file lives under `prototypes/adaptive_collector/` | there is no `prototypes/__init__.py`: `prototypes` is a namespace package, so the PR adds nothing outside this directory | the PR file list against base `c509c62` |
| nothing imports it | a scan of `app/`, `integrations/`, `scripts/` and `tests/` | `test_isolation.py::test_nothing_in_production_scripts_or_tests_imports_the_prototype` |
| it imports only the pure `app.collect.facts` value models, the stdlib and pydantic | an AST allowlist; `urllib.request`, `http.client`, DB, gateway, AI and OCR modules are not on it | `test_isolation.py::test_the_prototype_imports_only_value_models_stdlib_and_itself` |
| zero network | every test runs with `socket` connect, `create_connection` and `getaddrinfo` refused | `tests/conftest.py::no_network`, `test_isolation.py::test_the_network_is_refused_inside_every_prototype_test` |
| no DB, schema, migration, route, job, COLLECT wiring, `ProductFactsRevision` or ReviewItem | the in-memory `ProfileStore` is the only store | none of those modules is importable (allowlist above) |
| synthetic only | an invented supplier `synthetic` (and `synhook`), invented pages, hand-written expectations | `fixtures/` |

CI's `pytest` uses `testpaths = ["tests"]`, so these tests do not run in CI; they are run
explicitly (see [Run it](#run-it)). `ruff` covers the directory as part of `ruff check .`.

## The eight proofs (kickoff `5820294346`) → ADR-0017 → tests

| # | question | ADR-0017 | proven by |
| --- | --- | --- | --- |
| 1 | strict EPR/PTR parsing; immutable content-digest semantics | §3, §5.2 | `test_profiles.py` (13) |
| 2 | exactly one template matches; unmatched and ambiguous fail closed | §8.1 | `test_templates.py` (4). The template a page must match is test-level knowledge (`TEMPLATE_OF`), never part of an operator expectation |
| 3 | deterministic CORE/COVERAGE extraction, conflicts, ABSENT | §8.2, §4 | `test_fields.py` (13) |
| 4 | V2 image-region and image-role coverage, without bytes | §7.2 V2 | `test_images.py` (7) |
| 5 | closed hooks; `(hook_point, target)` counting | §6 | `test_hooks.py` (10); G6 and G7 also in `test_validation.py` |
| 6 | ValidationSample replay: product boundary, embedded literals, non-authoritative exclusion, `SAMPLE_TRUNCATED → INCOMPLETE` | §7.3, V3a, V8 | `test_samples.py` (18) |
| 7 | determinism | §7.2 V5 | `test_determinism.py` (4) |
| 8 | V4 synthetic negative controls, with mutation coverage | §7.2 V4 | `test_validation.py` (11) |

What each suite covers:

- **1 `test_profiles.py`.**
  - These are refused: unknown keys, statuses, source values, expressions, pseudo-classes and
    unbounded selectors.
  - The digest depends on content only, not on key order.
  - The store has no update and no delete, and it refuses a tampered row.
  - An EPR pins its PTRs by digest.
  - A template is never shared across suppliers.
  - The semantic tuple holds semantic parts only and is length-prefixed.
- **2 `test_templates.py`.**
  - Each sample matches its template.
  - Login and listing pages match no template and produce nothing.
  - Two matching templates give `TEMPLATE_AMBIGUOUS`, never "the closest".
- **3 `test_fields.py`.**
  - Every field equals the operator expectation.
  - `ABSENT` is given only for a COVERAGE field whose absence condition is observed. A missing
    anchor is never `ABSENT`.
  - Disagreeing locators and repeated labels give `REVIEW_REQUIRED`.
  - Use of an alternative locator alone is signalled.
  - The M3 boundary holds for options and quantity tiers.
  - Conditional shipping text is never flattened.
  - Stock follows the control rules.
  - **Control-state precision:**
    - A purchase control hidden by `visibility:hidden`, `opacity:0`, `aria-hidden="true"` or
      `hidden` is never stock authority.
    - So is a purchase control disabled by `disabled`, `aria-disabled="true"` or a disabled
      `fieldset`.
    - Hidden sold-out text is never a statement.
- **4 `test_images.py`.**
  - Ordered `(role, ordinal, reference)` equals the expectation, with the network refused.
  - Signed queries never reach the sample.
  - V2 fails a PTR without an image region, and an EPR that cannot assign a representative image.
  - `images` is not a parser field.
- **5 `test_hooks.py`.**
  - Unknown hook points, targets and format classes are refused.
  - A hook proposes a candidate; the engine decides.
  - `CANNOT_PARSE`, an invalid candidate and a smuggled `status` each give `REVIEW_REQUIRED`.
  - The bound `HOOK_REVISION` must be the running one.
  - The promotion key counts bindings, not hook points (G6).
  - G5 flags a key shared by two suppliers, whatever the format class.
- **6 `test_samples.py`.**
  - **Product boundary:**
    - The operator records one product boundary, and only that subtree is captured. Nothing
      outside it appears, not even as an exclusion.
    - A missing, empty or ambiguous boundary refuses the sample, and so does a boundary that is
      itself non-authoritative.
    - Member, review and recommendation regions inside the boundary are excluded by boundary and
      class only.
    - An unconfirmed detected region refuses the capture.
    - Operator reasons are a closed set.
  - **Sanitizer:**
    - A non-secret hidden input (`product_no`) is kept with its value.
    - A security hidden input (`csrf_token`) is kept as a control, with its value removed.
    - A user-entered value is removed; the control and its bounds stay.
    - Secret-bearing URL material inside embedded data is stripped (`…?token=` becomes the bare
      URL).
    - `on*` event-handler attributes are stripped.
    - Non-literal scripts are stripped.
  - **Samples, V3a and V8:**
    - Samples are immutable and content-addressed.
    - V3a flags the unread `productData` block.
    - An oversized block makes the sample `SAMPLE_TRUNCATED` and the run `INCOMPLETE`, without
      its bytes.
    - A provenance that names a profile is refused.
- **7 `test_determinism.py`.**
  - Repeated extraction is byte-equivalent.
  - Fresh stores and fresh captures give the same digests.
  - A validation run is deterministic.
- **8 `test_validation.py`.**
  - **Mutation coverage:** all five mutations run and fail closed: remove a required anchor,
    duplicate a price row, inject hidden sold-out text, add a conflicting identity, remove the
    image region. A mutation that cannot be constructed is recorded as `MUTATION_NOT_EXERCISED`,
    and V4 is then never `PASS`. An embedded first identity source still gets its conflict
    mutation.
  - An open V3a finding blocks `PASS`.
  - VALIDATED is derived from a PASS run for the exact freshness tuple.
  - A wrong profile fails V3.
  - A bundle that matches a login page fails V4.
  - One sample per template and two samples in total are required.

## Observed results (the recorded run)

- The operator expectations (`fixtures/expected/*.json`) carry identity, the twelve supplied fields
  and the images, and **no template key**. They agree with the engine.
- The first `validate()` over the synthetic bundle ends **`INCOMPLETE`**, by design. Every other
  check passes, and V4 reports all five mutations exercised. V3a flags a `productData` literal
  assignment (`sku`, `stock`) that no rule reads. Once the operator records that finding as
  resolved, the run ends **`PASS`**, and `VALIDATED` holds only for that run's exact freshness
  tuple.

## What this prototype deliberately does not do

- It implements none of the following kickoff non-goals: shadow storage, windows, the event
  ledger, `shadow_enabled_for_run`, the bundle freeze, retention, Phase C reconciliation, or any
  canonical run-store field. The Phase C carry-forward (Issue #110 `5818794101`) stays binding.
- It resolves no URLs, fetches no images and computes no image checksums. Image references are
  candidates only.
- It evaluates no JavaScript. A literal that is not valid JSON (for example, one with unquoted keys)
  is inadmissible and is stripped, never interpreted. This is a v1 limit.
- It makes no acceptance claim.

## Run it

```bash
PYTHONPATH=. .venv/Scripts/python.exe -m pytest -q -p no:cacheprovider prototypes/adaptive_collector/tests
```

```bash
MYPYPATH=. .venv/Scripts/python.exe -m mypy --strict --python-version 3.12 --explicit-package-bases prototypes/adaptive_collector
```
