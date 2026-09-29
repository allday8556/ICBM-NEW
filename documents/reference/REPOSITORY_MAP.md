# Repository map

Where each role lives after the repository restructure (Issue #151, ADR-0021 §2): large role →
middle area → detailed purpose. Old paths resolve through `documents/reference/PATH_MIGRATION_MAP.md`.
The root keeps only files that tools require there: `README.md`, `CLAUDE.md` (auto-loaded
bootstrap index), `pyproject.toml`, `constraints.txt`, `alembic.ini`, `.gitignore`, `.gitattributes`.

| path | role |
| --- | --- |
| `.github/` | CI workflow |
| `.github/workflows/` |  |
| `app/` | runtime application (composition root: `__init__`, `__main__`, `config`, `container`, `main`) |
| `app/capabilities/` | supporting capabilities: audit, jobs, review, live_safety |
| `app/interface/` | operator surfaces: HTTP api, screens, cli |
| `app/platform/` | platform services: core (errors, clock, ownership, egress, code identity), db, system |
| `app/stages/` | the product spine CONNECT → COLLECT → PRODUCT DB → REGISTER → OPERATE |
| `automation/` | tooling outside the runtime |
| `automation/acceptance/` | acceptance harnesses by campaign; `common/` is the shared offline core |
| `automation/adaptive/` | Adaptive Phase C campaign harness |
| `automation/agent-host/` | Agent Host scripts (sha256-pinned bytes) |
| `automation/archive/` | historical entry points, byte-identical |
| `design/` | UI prototypes (design reference; `archive/` holds superseded ones) |
| `design/prototypes/` |  |
| `documents/` | all canonical and historical documents |
| `documents/acceptance/` | acceptance records (milestones, gates, adaptive, issues) |
| `documents/architecture/` | architecture, glossary, frozen v3.1 reference |
| `documents/archive/` | historical documents, byte-identical |
| `documents/contracts/` | platform and UI contracts |
| `documents/decisions/` | ADRs and architect review records |
| `documents/evidence/` | external provider evidence catalog |
| `documents/reference/` | this map and the path migration map |
| `documents/reviews/` | Claude proposal channel |
| `documents/roadmap/` | roadmap and current milestone |
| `documents/rules/` | operating rules (bodies); `agent-host/` holds the Agent Host protocol |
| `integrations/` | adapters: suppliers and marketplaces |
| `integrations/marketplaces/` |  |
| `integrations/suppliers/` |  |
| `tests/` | tests |
| `tests/contracts/` | repository-rule and document-contract tests |
| `tests/fixtures/` | test fixtures (byte-pinned) |
| `tests/harness/` | tests of the acceptance harnesses |
| `tests/integration/` | integration tests by runtime owner |
| `tests/support/` | shared test support |
| `tests/unit/` | unit tests by runtime owner |
| `ui/` |  |
| `ui/web/` | the served web client |
