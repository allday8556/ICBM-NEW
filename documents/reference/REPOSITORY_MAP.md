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
`app/capabilities/live_safety/eligibility.py`, migration `0033` and one integration suite.
The Agent Host operating-authority correction (ADR-0022) added `documents/rules/14-operating-authority.md`,
the ADR, the fixture configuration `automation/agent-host/tests/fx-config.json` and the fixture test under
`tests/harness/agent_host/`. No file moved.

| path | files | role |
| --- | --- | --- |
| `.github/` | 1 | CI workflow |
| `.github/workflows/` | 1 |  |
| `app/` | 239 | runtime application (composition root: `__init__`, `__main__`, `config`, `container`, `main`) |
| `app/capabilities/` | 41 | supporting capabilities: audit, jobs, review, live_safety |
| `app/interface/` | 19 | operator surfaces: HTTP api, screens, cli |
| `app/platform/` | 63 | platform services: core (errors, clock, ownership, egress, code identity), db, system |
| `app/stages/` | 111 | the product spine CONNECT → COLLECT → PRODUCT DB → REGISTER → OPERATE |
| `automation/` | 94 | tooling outside the runtime |
| `automation/acceptance/` | 67 | acceptance harnesses by campaign; `common/` is the shared offline core |
| `automation/adaptive/` | 10 | Adaptive Phase C campaign harness |
| `automation/agent-host/` | 14 | Agent Host scripts (sha256-pinned bytes) |
| `automation/archive/` | 2 | historical entry points, byte-identical |
| `design/` | 3 | UI prototypes (design reference; `archive/` holds superseded ones) |
| `design/prototypes/` | 3 |  |
| `documents/` | 155 | all canonical and historical documents |
| `documents/acceptance/` | 63 | acceptance records (milestones, gates, adaptive, issues) |
| `documents/architecture/` | 3 | architecture, glossary, frozen v3.1 reference |
| `documents/archive/` | 9 | historical documents, byte-identical |
| `documents/contracts/` | 9 | platform and UI contracts |
| `documents/decisions/` | 24 | ADRs and architect review records |
| `documents/evidence/` | 27 | external provider evidence catalog |
| `documents/reference/` | 3 | this map and the path migration map |
| `documents/reviews/` | 1 | Claude proposal channel |
| `documents/roadmap/` | 2 | roadmap and current milestone |
| `documents/rules/` | 14 | operating rules (bodies); `agent-host/` holds the Agent Host protocol |
| `integrations/` | 37 | adapters: suppliers and marketplaces |
| `integrations/marketplaces/` | 17 |  |
| `integrations/suppliers/` | 19 |  |
| `tests/` | 219 | tests |
| `tests/contracts/` | 7 | repository-rule and document-contract tests |
| `tests/fixtures/` | 10 | test fixtures (byte-pinned) |
| `tests/harness/` | 30 | tests of the acceptance harnesses |
| `tests/integration/` | 82 | integration tests by runtime owner |
| `tests/support/` | 12 | shared test support |
| `tests/unit/` | 76 | unit tests by runtime owner |
| `ui/` | 44 |  |
| `ui/web/` | 44 | the served web client |
