# Adaptive Collector — Phase B prototype (disposable)

Status: **PROTOTYPE — draft PR only, never merged, never promoted.**
Author: Claude Code
Issue: #110 — Phase B kickoff `5820294346`, under ADR-0017 §13
Base: main `c509c6236aeaddd76f7553071bcc1ee0799eeb19`

This directory proves the core ADR-0017 behaviours on **synthetic fixtures only**. It is not
production code and is never copied into production. Production code is written fresh, after its
own authorization.

## Isolation (ADR-0017 §13)

| rule | how it holds | proof |
| --- | --- | --- |
| lives only under `prototypes/adaptive_collector/` | the whole diff of the draft PR | the PR file list |
| nothing imports it | a scan of `app/`, `integrations/`, `scripts/`, `tests/` | `test_isolation.py::test_nothing_in_production_scripts_or_tests_imports_the_prototype` |
| it imports only the pure `app.collect.facts` value models, stdlib and pydantic | an AST allowlist; no `urllib.request`, `http.client`, DB, gateway, AI or OCR module | `test_isolation.py::test_the_prototype_imports_only_value_models_stdlib_and_itself` |
| zero network | every test runs with `socket` connect, `create_connection` and `getaddrinfo` refused | `conftest.py::no_network`, `test_isolation.py::test_the_network_is_refused_inside_every_prototype_test` |
| no DB, schema, migration, route, job, COLLECT wiring, `ProductFactsRevision` or ReviewItem | the in-memory `ProfileStore` is the only store | none of those modules is importable (allowlist above) |
| synthetic only | an invented supplier `synthetic` (and `synhook`); invented pages; hand-written expectations | `fixtures/` |

CI's `pytest` has `testpaths = ["tests"]`, so these tests do not run in CI. They are run explicitly
(below). `ruff` covers the directory as part of `ruff check .`.

## The eight proofs (kickoff `5820294346`) → ADR-0017 → tests

| # | kickoff question | ADR-0017 | proven by |
| --- | --- | --- | --- |
| 1 | strict EPR/PTR parsing, immutable content-digest semantics | §3, §5.2 | `test_profiles.py` (13): unknown keys, statuses, source values, expressions, pseudo-classes and unbounded selectors are refused; digest is content-only and key-order independent; the store has no update/delete and refuses a tampered row; an EPR pins PTRs by digest; templates are never shared across suppliers; the semantic tuple holds semantic parts only and is length-prefixed |
| 2 | exactly-one template; unmatched and ambiguous fail closed | §8.1 | `test_templates.py` (4): each sample matches its template; login and listing pages match none and produce no identity, field or image; two matches are `TEMPLATE_AMBIGUOUS`, never "closest" |
| 3 | deterministic CORE/COVERAGE extraction, conflicts, ABSENT | §8.2, §4 | `test_fields.py` (10): every field equals the operator expectation; `ABSENT` only for COVERAGE with its absence condition observed; a missing anchor is `REVIEW_REQUIRED`, never `ABSENT`; disagreeing locators and repeated labels are `REVIEW_REQUIRED`; alternative-only use is signalled; the M3 boundary holds for options and tiers; a conditional shipping text is never flattened; stock follows the control rules (active purchase beats sold-out text, hidden text is no statement) |
| 4 | V2 image-region / image-role coverage without bytes | §7.2 V2 | `test_images.py` (7): ordered `(role, ordinal, reference)` equals expectations with the network refused; signed queries never reach the sample; V2 fails for a PTR without an image region and for an EPR that cannot assign a representative; `images` is not a parser field; a missing representative is never complete |
| 5 | closed hooks and `(hook_point, target)` counting | §6 | `test_hooks.py` (10): unknown hook points, targets and format classes are refused; hooks propose candidates and the engine decides; `CANNOT_PARSE`, an invalid candidate and a smuggled `status` are all `REVIEW_REQUIRED`; the bound `HOOK_REVISION` must be the running one; the promotion key counts bindings, not hook points (G6); G5 flags a key shared by two suppliers whatever the format class. `test_validation.py`: G6 over the cap blocks until an architecture review; G7 fails an unexercised hook |
| 6 | ValidationSample replay: embedded literals, non-authoritative exclusion, `SAMPLE_TRUNCATED → INCOMPLETE` | §7.3, V3a, V8 | `test_samples.py` (10): the capture takes no profile; product controls, JSON-LD and a pure-literal assignment are kept; secrets, member data, user values, hidden security fields, signed queries and non-literal scripts are stripped and recorded; review/recommend/member regions are excluded by boundary and class only; an unconfirmed region refuses the capture; operator reasons are closed; samples are immutable and content-addressed; V3a flags the unread `productData` block; an oversized block makes the sample `SAMPLE_TRUNCATED` and the run `INCOMPLETE` without its bytes; a provenance naming a profile is refused |
| 7 | determinism | §7.2 V5 | `test_determinism.py` (4): repeated extraction is byte-equivalent; fresh stores and captures give the same digests; the raw page and its sample agree where nothing was stripped; a validation run is deterministic |
| 8 | V4 synthetic negative controls | §7.2 V4 | `test_validation.py` (8): every mutation (remove a required anchor, duplicate a price row, inject hidden sold-out text, add a conflicting identity, remove the image region) and both negative pages fail closed; an open V3a finding blocks PASS until the operator resolves it; VALIDATED is derived from a PASS for the exact freshness tuple and lapses when an implementation fingerprint changes; a wrong profile fails V3; a bundle that matches a login page fails V4; one sample per template and two in total are required |

## Observed results (the recorded run)

- The three hand-written operator expectations (`fixtures/expected/*.json`) agreed with the engine
  on the first run: identity, all twelve supplied fields, and images.
- The first `validate()` over the synthetic bundle is **`INCOMPLETE`**, by design. Every check
  passes except V3a: the page carries a `productData` literal assignment (`sku`, `stock`) that no
  rule reads. After the operator records that finding as resolved, the run is **`PASS`**, and
  `VALIDATED` holds only for that run's exact freshness tuple.

## What this prototype deliberately does not do

- It does not implement shadow storage, windows, the event ledger, `shadow_enabled_for_run`,
  bundle freeze, retention, Phase C reconciliation, or any canonical run-store field. These are the
  kickoff non-goals, and the Phase C carry-forward (Issue #110 `5818794101`) stays binding.
- It does not resolve URLs, fetch images or compute image checksums. Image references are
  candidates only.
- It has no JavaScript evaluation of any kind. A non-JSON literal (for example, unquoted keys) is
  inadmissible and stripped, never interpreted. That is a v1 limit.
- It makes no acceptance claim.

## Run it

```bash
PYTHONPATH=. .venv/Scripts/python.exe -m pytest -q -p no:cacheprovider prototypes/adaptive_collector/tests
```

```bash
PYTHONPATH=. .venv/Scripts/python.exe -m mypy --strict --python-version 3.12 prototypes/adaptive_collector
```
