# Repository map

Where each role lives after the repository restructure (Issue #151, ADR-0021 §2): large role →
middle area → detailed purpose. Old paths resolve through `documents/reference/PATH_MIGRATION_MAP.md`.
The root keeps only files that tools require there: `README.md`, `CLAUDE.md` (auto-loaded
bootstrap index), `pyproject.toml`, `constraints.txt`, `alembic.ini`, `.gitignore`, `.gitattributes`.

| path | files | role |
| --- | --- | --- |
| `.github/` | 1 | CI workflow |
| `.github/workflows/` | 1 |  |
| `app/` | 233 | runtime application (composition root: `__init__`, `__main__`, `config`, `container`, `main`) |
| `app/capabilities/` | 40 | supporting capabilities: audit, jobs, review, live_safety |
| `app/interface/` | 19 | operator surfaces: HTTP api, screens, cli |
| `app/platform/` | 59 | platform services: core (errors, clock, ownership, egress, code identity), db, system |
| `app/stages/` | 110 | the product spine CONNECT → COLLECT → PRODUCT DB → REGISTER → OPERATE |
| `automation/` | 94 | tooling outside the runtime |
| `automation/acceptance/` | 68 | acceptance harnesses by campaign; `common/` is the shared offline core |
| `automation/adaptive/` | 10 | Adaptive Phase C campaign harness |
| `automation/agent-host/` | 13 | Agent Host scripts (sha256-pinned bytes) |
| `automation/archive/` | 2 | historical entry points, byte-identical |
| `design/` | 3 | UI prototypes (design reference; `archive/` holds superseded ones) |
| `design/prototypes/` | 3 |  |
| `documents/` | 154 | all canonical and historical documents |
| `documents/acceptance/` | 63 | acceptance records (milestones, gates, adaptive, issues) |
| `documents/architecture/` | 3 | architecture, glossary, frozen v3.1 reference |
| `documents/archive/` | 9 | historical documents, byte-identical |
| `documents/contracts/` | 10 | platform and UI contracts |
| `documents/decisions/` | 23 | ADRs and architect review records |
| `documents/evidence/` | 27 | external provider evidence catalog |
| `documents/reference/` | 3 | this map and the path migration map |
| `documents/reviews/` | 1 | Claude proposal channel |
| `documents/roadmap/` | 2 | roadmap and current milestone |
| `documents/rules/` | 13 | operating rules (bodies); `agent-host/` holds the Agent Host protocol |
| `integrations/` | 35 | adapters: suppliers and marketplaces |
| `integrations/marketplaces/` | 15 |  |
| `integrations/suppliers/` | 19 |  |
| `tests/` | 214 | tests |
| `tests/contracts/` | 7 | repository-rule and document-contract tests |
| `tests/fixtures/` | 10 | test fixtures (byte-pinned) |
| `tests/harness/` | 30 | tests of the acceptance harnesses |
| `tests/integration/` | 80 | integration tests by runtime owner |
| `tests/support/` | 12 | shared test support |
| `tests/unit/` | 73 | unit tests by runtime owner |
| `ui/` | 44 |  |
| `ui/web/` | 44 | the served web client |
