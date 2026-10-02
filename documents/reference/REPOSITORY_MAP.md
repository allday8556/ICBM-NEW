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

| path | files | role |
| --- | --- | --- |
| `.github/` | 1 | CI workflow |
| `.github/workflows/` | 1 |  |
| `app/` | 254 | runtime application (composition root: `__init__`, `__main__`, `config`, `container`, `main`) |
| `app/capabilities/` | 42 | supporting capabilities: audit, jobs, review, live_safety |
| `app/interface/` | 20 | operator surfaces: HTTP api, screens, cli |
| `app/platform/` | 66 | platform services: core (errors, clock, ownership, egress, code identity), db, system |
| `app/stages/` | 121 | the product spine CONNECT → COLLECT → PRODUCT DB → REGISTER → OPERATE |
| `automation/` | 94 | tooling outside the runtime |
| `automation/acceptance/` | 67 | acceptance harnesses by campaign; `common/` is the shared offline core |
| `automation/adaptive/` | 10 | Adaptive Phase C campaign harness |
| `automation/agent-host/` | 14 | Agent Host scripts (sha256-pinned bytes) |
| `automation/archive/` | 2 | historical entry points, byte-identical |
| `design/` | 4 | UI prototypes (design reference; `archive/` holds superseded ones) |
| `design/prototypes/` | 4 |  |
| `documents/` | 158 | all canonical and historical documents |
| `documents/acceptance/` | 66 | acceptance records (milestones, gates, adaptive, issues) |
| `documents/architecture/` | 3 | architecture, glossary, frozen v3.1 reference |
| `documents/archive/` | 9 | historical documents, byte-identical |
| `documents/contracts/` | 9 | platform and UI contracts |
| `documents/decisions/` | 24 | ADRs and architect review records |
| `documents/evidence/` | 27 | external provider evidence catalog |
| `documents/reference/` | 3 | this map and the path migration map |
| `documents/reviews/` | 1 | Claude proposal channel |
| `documents/roadmap/` | 2 | roadmap and current milestone |
| `documents/rules/` | 14 | operating rules (bodies); `agent-host/` holds the Agent Host protocol |
| `integrations/` | 38 | adapters: suppliers and marketplaces |
| `integrations/marketplaces/` | 17 |  |
| `integrations/suppliers/` | 20 |  |
| `tests/` | 244 | tests |
| `tests/contracts/` | 9 | repository-rule and document-contract tests |
| `tests/fixtures/` | 11 | test fixtures (byte-pinned) |
| `tests/harness/` | 30 | tests of the acceptance harnesses |
| `tests/integration/` | 96 | integration tests by runtime owner |
| `tests/support/` | 14 | shared test support |
| `tests/unit/` | 82 | unit tests by runtime owner |
| `ui/` | 54 | operator clients: the served web client and the capture extension |
| `ui/extension/` | 9 | the Chrome MV3 capture extension (ADR-0019): transport and capture UX only, plain ES modules, no build step |
| `ui/web/` | 45 | the served web client |
