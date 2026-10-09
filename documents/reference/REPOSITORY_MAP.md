# Repository map

Where each role lives after the repository restructure (Issue #151, ADR-0021 §2): large role →
middle area → detailed purpose. Old paths resolve through `documents/reference/PATH_MIGRATION_MAP.md`.
The root keeps only files that tools require there: `README.md`, `CLAUDE.md` (auto-loaded
bootstrap index), `pyproject.toml`, `constraints.txt`, `alembic.ini`, `.gitignore`, `.gitattributes`.
The files column counts the tracked files under each path; `tests/contracts/test_repository_rules.py`
(`test_the_repository_map_counts_match_the_tree`) checks every count against `git ls-files`.
Note: until PR #157 the map under-counted `integrations/` and `integrations/marketplaces/` by one,
because `integrations/marketplaces/base.py` was restored after the counts were taken; with that file
removed by PR #157, 35 and 15 were the tree's counts. The CREATE adoption (PR #158) added
`integrations/marketplaces/smartstore/create.py` and its unit suite (36 and 16), and the SEARCH
positive-only reconcile slice added `integrations/marketplaces/smartstore/search.py`, its unit suite
and migration `0031` (37 and 17). The authoring-revision owners slice added
`app/stages/register/authoring_revisions.py`, migration `0032` and one integration suite. The canary-eligibility owner slice added
`app/capabilities/live_safety/eligibility.py`, migration `0033` and one integration suite. The ASSET duplicate-evidence fix (Issue #89 resolution `5915900049` D4) added one integration suite. The protected operator commands (D3) added one integration suite. The application-freeze inputs fix (Issue #89 follow-up `5919917893` §3) added one integration suite.
The Agent Host operating-authority correction (ADR-0022) added `documents/rules/14-operating-authority.md`,
the ADR, the fixture configuration `automation/agent-host/tests/fx-config.json` and the fixture test under
`tests/harness/agent_host/`. No file moved.
The extension capture transport, slice E1 (ADR-0019; Issue #126), added the client under
`ui/extension/`, the ingest owner under `app/stages/collect/extension/`, its router, the KM
capture policy beside the KM collect package, migration `0034`, the test-browser owner
`tests/support/browser.py` and their tests. Slice E2 removed the E1 dry run
(`app/stages/collect/adaptive/shadow/dry_run.py`) and added the extension tests' `conftest.py`
and `documents/acceptance/adaptive/EXTENSION-E2.md`. No file moved, so `PATH_MIGRATION_MAP` is unchanged.
The registration read state (ADR-0014 §28.5) added `app/stages/register/read_state.py`, the
status card `ui/web/js/components/registration-status.js`, one unit and one contract suite.
The Extension Collector UI added the side panel's approved prototype
`design/prototypes/icbm_extension_collector.html`.
The E3 list queue's server half (ADR-0019 §8.1) added the queue owner
`app/stages/collect/extension/queue.py`, migration `0035` and one integration suite.
Its extension half added the list discovery the side panel injects,
`ui/extension/lib/discover.js`, and its acceptance record
`documents/acceptance/adaptive/EXTENSION-E3.md`.
The residual-risk acceptance proof (ADR-0018 §6.1, G3-30) added
`app/capabilities/live_safety/residual_risk.py`, migration `0036` and one integration suite.
The committed-session bearer seam (ROADMAP §14 item 4) added one integration suite.
The bounded LIVE runtime transition (ROADMAP §14 item 5) added one integration suite.
The deletion of one ICBM-confirmed registration (ADR-0018 §3.5) added
`app/stages/register/deletion.py`, `integrations/marketplaces/smartstore/deletion.py`, migration
`0037`, one integration suite and one unit suite.
The operator image API (ADR-0013 §9 amendment note) added
`app/interface/api/routes/product_images.py` and one integration suite.
The operator's synthetic test products (owner decision 2026-10-03) added
`app/stages/collect/synthetic.py`, `app/interface/api/routes/synthetic_products.py`, migration
`0038` and one integration suite.
Issue #219 (supplier common images) added `app/stages/products/common_images.py`, migration `0039`
and one integration suite.
Issue #219 (image auto-selection) added `app/stages/products/auto_images.py`, migration `0040`, one
integration suite and two unit suites.
Notice coverage S0 (owner directive 2026-10-03) added
`integrations/marketplaces/smartstore/notice_catalog.py` and one unit suite.
Its capture (2026-10-03) added the S0 inventory `NOTICE_SMARTSTORE_S0.md` and the retained
responses `smartstore-notice-capture-2026-10-03.json` under `documents/evidence/marketplace-apis/`.
The current 2.90.0 reference's notice children are retained beside them as
`smartstore-notice-schema-2.90.0-2026-10-03.json`.
Notice coverage S1 added `integrations/marketplaces/smartstore/notice_schema.py`, the derived data
`notice_schema.json` beside it and one unit suite.
Notice coverage S2 (typed notice values and field rules) added one unit suite under `tests/unit/register/`.
Notice coverage S3 (one notice contract for metadata, preflight and wire) added one unit suite under
`tests/unit/integrations/marketplaces/smartstore/`.
Notice coverage S4 added the contract matrix of every documented SmartStore notice child, with its pinned
wire forms `notice_children.golden.json`, beside them.
The Collect Truth Inspector (A-UX1, owner decision 2026-10-04) added the 수집관리 component
`ui/web/js/components/collect-facts.js`.
The supplier common-image screen (A-NEXT2a, Issue #231) added the 수집관리 component
`ui/web/js/components/common-images.js`.
The registration editor (owner decision 2026-10-08, phase 1) added the 등록관리 page
`ui/web/js/pages/register-editor.js` and one integration suite.
The AI authoring foundation's AIF-1 (ADR-0026) added the AI capability `app/capabilities/ai/` (the v29 prompt
registry catalog and its seed `seed_v29.json`, the PromptTemplate and PlatformPolicy stores and their
models), the routes
`app/interface/api/routes/ai.py`, migration `0054`, the Settings component
`ui/web/js/components/prompt-registry.js` and two integration suites under `tests/integration/ai/`.
AIF-2 added the provider port `provider.py`, the request `composer.py` and `execution.py` to
`app/capabilities/ai/`, and one integration suite.
AIF-3 added PRODUCT DB's enrichment owner `app/stages/products/enrichment.py` and its model, the
routes `app/interface/api/routes/enrichment.py`, migration `0055` and one integration suite.
AIF-4 added the Preparation's AI apply command (in the register owner) and one integration suite.
ADR-0027 AIS-1 added the provider profile `profiles.py` and its model, the adapters
`integrations/ai/` (the CLIProxyAPI call and the serving-process probe), the Settings component
`ui/web/js/components/ai-provider.js`, migration `0056`, one unit and one integration suite.
ADR-0027 AIS-2 added the product-name task `app/stages/products/tasks.py`, the seed upgrade
`v29-ai1`, the recommendation component `ui/web/js/components/ai-name.js` and two integration suites.
ADR-0028 T2 added the read-only tag source `integrations/marketplaces/smartstore/tags.py` and
one unit suite.
ADR-0028 T3 added the tag task's context and filter `app/capabilities/ai/platform_tags.py`, the
task-context types `task_context.py`, the provider-zero SearchSignal port `search_signal.py`, the
seed upgrade `v29-ai2` and one integration suite.
ADR-0028 T4 added the tag provenance and apply to the register owner, the component
`ui/web/js/components/ai-tags.js` and two integration suites.
The official SmartStore leaf-category catalog and local bulk-registration orchestration added
`app/stages/register/category_catalog.py`, migration `0041`, the SmartStore category adapter and
one integration plus one unit suite.
Sequential bulk registration now has no application-level total-count ceiling: migration `0046`
and `app/stages/register/bulk.py` persist the ordered run, queue exactly one ordinary CREATE job at
a time, expose durable `current/total` progress, and retain a classified reason for each failed
product while later products continue.
The Common Sales Option C1/C2 owner added three PRODUCT DB modules, migration `0042` and one
integration and one unit suite. Its reviewed Product Fact correspondence slice added three more
PRODUCT DB modules, migration `0043` and one integration and one unit suite.
The source-proven Atomic SKU slice added three PRODUCT DB modules, migration `0044` and one
integration and one unit suite.
The AtomicSKU-qualified Product Item identity slice added two PRODUCT DB modules and migration
`0045`; it leaves the legacy no-option Item and `registration-item-key/v1` contracts unchanged.
C-P1 adds one provider-neutral REGISTER module plus contract and unit suites for structure-only
marketplace compatibility and canonical read-only rendering. It has no migration, price, provider
payload or external call.
C-P2 adds `atomic_sku_economics_models.py`, `atomic_sku_economics_store.py` and migration `0047`.
The existing pricing/readiness services expose the additive AtomicSKU-qualified path; the store
does not calculate. Its integration suite covers per-configuration cost/price, missing-delta and
composed-fulfillment refusal, stale propagation and immutable snapshot pinning. It has no provider
payload or external call and leaves legacy Item economics unchanged.

| path | files | role |
| --- | --- | --- |
| `.github/` | 1 | CI workflow |
| `.github/workflows/` | 1 |  |
| `app/` | 335 | runtime application (composition root: `__init__`, `__main__`, `config`, `container`, `main`) |
| `app/capabilities/` | 57 | supporting capabilities: audit, jobs, review, live_safety, ai |
| `app/interface/` | 27 | operator surfaces: HTTP api, screens, cli |
| `app/platform/` | 86 | platform services: core (errors, clock, ownership, egress, code identity), db, system |
| `app/stages/` | 160 | the product spine CONNECT → COLLECT → PRODUCT DB → REGISTER → OPERATE |
| `automation/` | 94 | tooling outside the runtime |
| `automation/acceptance/` | 67 | acceptance harnesses by campaign; `common/` is the shared offline core |
| `automation/adaptive/` | 10 | Adaptive Phase C campaign harness |
| `automation/agent-host/` | 14 | Agent Host scripts (sha256-pinned bytes) |
| `automation/archive/` | 2 | historical entry points, byte-identical |
| `design/` | 4 | UI prototypes (design reference; `archive/` holds superseded ones) |
| `design/prototypes/` | 4 |  |
| `documents/` | 171 | all canonical and historical documents |
| `documents/acceptance/` | 67 | acceptance records (milestones, gates, adaptive, issues) |
| `documents/architecture/` | 3 | architecture, glossary, frozen v3.1 reference |
| `documents/archive/` | 9 | historical documents, byte-identical |
| `documents/contracts/` | 9 | platform and UI contracts |
| `documents/decisions/` | 30 | ADRs and architect review records |
| `documents/evidence/` | 30 | external provider evidence catalog |
| `documents/reference/` | 3 | this map and the path migration map |
| `documents/reviews/` | 4 | Claude proposal channel |
| `documents/roadmap/` | 2 | roadmap and current milestone |
| `documents/rules/` | 14 | operating rules (bodies); `agent-host/` holds the Agent Host protocol |
| `integrations/` | 51 | adapters: suppliers and marketplaces |
| `integrations/marketplaces/` | 27 |  |
| `integrations/suppliers/` | 20 |  |
| `tests/` | 306 | tests |
| `tests/contracts/` | 10 | repository-rule and document-contract tests |
| `tests/fixtures/` | 11 | test fixtures (byte-pinned) |
| `tests/harness/` | 30 | tests of the acceptance harnesses |
| `tests/integration/` | 131 | integration tests by runtime owner |
| `tests/support/` | 14 | shared test support |
| `tests/unit/` | 108 | unit tests by runtime owner |
| `ui/` | 66 | operator clients: the served web client and the capture extension |
| `ui/extension/` | 9 | the Chrome MV3 capture extension (ADR-0019): transport and capture UX only, plain ES modules, no build step |
| `ui/web/` | 57 | the served web client |
