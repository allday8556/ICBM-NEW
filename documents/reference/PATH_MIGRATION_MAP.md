# PATH_MIGRATION_MAP — repository restructure (Issue #151, ADR-0021)

Status: **PERMANENT.** Authority: ADR-0021 §6 and Issue #151 §4–§5. Every path of the
pre-migration tree (`e72a5cad5303057945b547f7a2e480b398d5bc2d`, 716 tracked files) is listed exactly once with its new path.
A past issue, PR, review, comment, acceptance record or evidence file that names an old path is
**not** rewritten (ADR-0021 §5); it is traced to the current location through this map.
The same table is in `PATH_MIGRATION_MAP.csv` for tools.

Dispositions: KEEP 99, MOVE 602, MERGE 2, ARCHIVE 12, REMOVE 1.

- `KEEP`: the path is unchanged (its content may still carry path-only edits).
- `MOVE`: a structural move; path-only edits only (ADR-0021 §4).
- `ARCHIVE`: a historical file moved under an `archive/` folder; byte-identical.
- `MERGE`: folded into the named owner with zero content loss; the old file is gone.
- `REMOVE`: deleted after a fresh scan found no import, runtime, path or current-document reference.

## 1. Former `CLAUDE.md` sections

The root `CLAUDE.md` is now the auto-loaded bootstrap index (ADR-0021 §3). Section numbers are kept
inside the new files, so `CLAUDE.md §N` still resolves.

| former | canonical location |
| --- | --- |
| intro, §13 Canonical file index and read order | `documents/rules/README.md` |
| §1 Roles, §1.1 Repository exchange protocol | `documents/rules/01-roles-and-exchange.md` |
| §2 Absolute no-legacy rule | `documents/rules/02-no-legacy.md` |
| §3 UI source rule | `documents/rules/03-ui-source.md` |
| §4 Pinned runtime stack | `documents/rules/04-runtime-stack.md` |
| §5 Architectural rules | `documents/rules/05-architectural-rules.md` |
| §6 Immutable domain rules | `documents/rules/06-immutable-domain-rules.md` |
| §7 Execution safety | `documents/rules/07-execution-safety.md` |
| §8 Git conventions | `documents/rules/08-git-conventions.md` |
| §9 Definition of Done | `documents/rules/09-definition-of-done.md` |
| §10 Working style expected of Claude | `documents/rules/10-working-style.md` |
| §11 Current milestone | `documents/roadmap/CURRENT-MILESTONE.md` |
| §12 First vertical | `documents/rules/12-first-vertical.md` |

## 2. Identities that changed with the move

| identity | old | new | note |
| --- | --- | --- | --- |
| KM통상 `EXTRACTOR_FINGERPRINT` | `c622da511c86d35d263ba873d56fa013d4f71b00649af6a1f065ceb63138c41b` | `7055eac566872047afbfb4e961605915ea96fb5e56e5e77eb90f0d0fb1684ffa` | two hashed modules changed only an import path (`app.collect.facts` → `app.stages.collect.facts`); `EXTRACTOR_REVISION` stays `kmretail-1` |
| Adaptive `EXTRACTOR_FINGERPRINT` | `90594482f3fb0e9661eec66bb018017670e509aaf72aee1a0d1447040301ec03` | `c44f0007a990062141b69272895a91560c85d69bb2538ee5fdd73ed88cf544ed` | the hashed input paths moved to `app/stages/collect/adaptive/engine/`; `EXTRACTOR_REVISION` stays `adaptive-engine-1` (goldens unchanged) |
| Gate 3 running-code digest | per commit | per commit | every `app/` path label changed; the visual acceptance recorded before the migration is stale (G3-31) and is re-run on the merged main |
| console entry point | `icbm = app.cli:main` | `icbm = app.interface.cli:main` | `pyproject.toml` |
| Alembic `script_location` | `app/db/migrations` | `app/platform/db/migrations` | revision ids and file names unchanged, so no `alembic_version` impact |
| served UI URLs | `/js/pages/<panel>.js`, `/js/core/capability.js` | `/js/pages/settings/<panel>.js`, `/js/platforms/smartstore/capability.js` | the six Settings panels and the SmartStore capability labels |
| M2 harness keyring backend (operator env) | `scripts.m2harness.keyrings.*` | `automation.acceptance.m2.harness.keyrings.*` | a future M2 harness run must use the new dotted name |
| CI docs-mode suite | `pytest tests/unit` | `pytest tests/unit tests/contracts tests/harness -m "not integration"` | the same 2030 tests plus the new rule-migration tests (auto-load, former-section proof and its detector) |

Job types, review producer names, table names and Alembic revision ids are unchanged.

## 3. Python modules

450 dotted module names changed (`from`/`import`, `python -m`, monkeypatch strings).

| old module | new module |
| --- | --- |
| `app.api` | `app.interface.api` |
| `app.api.deps` | `app.interface.api.deps` |
| `app.api.errors` | `app.interface.api.errors` |
| `app.api.middleware` | `app.interface.api.middleware` |
| `app.api.routes` | `app.interface.api.routes` |
| `app.api.routes.collect` | `app.interface.api.routes.collect` |
| `app.api.routes.connect` | `app.interface.api.routes.connect` |
| `app.api.routes.diagnostics` | `app.interface.api.routes.diagnostics` |
| `app.api.routes.products` | `app.interface.api.routes.products` |
| `app.api.routes.register` | `app.interface.api.routes.register` |
| `app.api.routes.review` | `app.interface.api.routes.review` |
| `app.api.routes.screens` | `app.interface.api.routes.screens` |
| `app.api.routes.settings` | `app.interface.api.routes.settings` |
| `app.api.routes.system` | `app.interface.api.routes.system` |
| `app.audit` | `app.capabilities.audit` |
| `app.audit.models` | `app.capabilities.audit.models` |
| `app.audit.service` | `app.capabilities.audit.service` |
| `app.cli` | `app.interface.cli` |
| `app.collect` | `app.stages.collect` |
| `app.collect.adaptive` | `app.stages.collect.adaptive.engine` |
| `app.collect.adaptive.canonical` | `app.stages.collect.adaptive.engine.canonical` |
| `app.collect.adaptive.capture` | `app.stages.collect.adaptive.engine.capture` |
| `app.collect.adaptive.document` | `app.stages.collect.adaptive.engine.document` |
| `app.collect.adaptive.engine` | `app.stages.collect.adaptive.engine.engine` |
| `app.collect.adaptive.extraction_identity` | `app.stages.collect.adaptive.engine.extraction_identity` |
| `app.collect.adaptive.hooks` | `app.stages.collect.adaptive.engine.hooks` |
| `app.collect.adaptive.lint` | `app.stages.collect.adaptive.engine.lint` |
| `app.collect.adaptive.locator` | `app.stages.collect.adaptive.engine.locator` |
| `app.collect.adaptive.profiles` | `app.stages.collect.adaptive.engine.profiles` |
| `app.collect.adaptive.validation` | `app.stages.collect.adaptive.engine.validation` |
| `app.collect.adaptive_capture` | `app.stages.collect.adaptive.phase_c_capture` |
| `app.collect.adaptive_capture.accounting` | `app.stages.collect.adaptive.phase_c_capture.accounting` |
| `app.collect.adaptive_capture.commands` | `app.stages.collect.adaptive.phase_c_capture.commands` |
| `app.collect.adaptive_capture.controls` | `app.stages.collect.adaptive.phase_c_capture.controls` |
| `app.collect.adaptive_capture.models` | `app.stages.collect.adaptive.phase_c_capture.models` |
| `app.collect.adaptive_capture.runner` | `app.stages.collect.adaptive.phase_c_capture.runner` |
| `app.collect.adaptive_capture.store` | `app.stages.collect.adaptive.phase_c_capture.store` |
| `app.collect.adaptive_shadow` | `app.stages.collect.adaptive.shadow` |
| `app.collect.adaptive_shadow.compare` | `app.stages.collect.adaptive.shadow.compare` |
| `app.collect.adaptive_shadow.evidence` | `app.stages.collect.adaptive.shadow.evidence` |
| `app.collect.adaptive_shadow.models` | `app.stages.collect.adaptive.shadow.models` |
| `app.collect.adaptive_shadow.runner` | `app.stages.collect.adaptive.shadow.runner` |
| `app.collect.adaptive_shadow.store` | `app.stages.collect.adaptive.shadow.store` |
| `app.collect.adaptive_shadow.switch` | `app.stages.collect.adaptive.shadow.switch` |
| `app.collect.adaptive_store` | `app.stages.collect.adaptive.store` |
| `app.collect.adaptive_store.gate` | `app.stages.collect.adaptive.store.gate` |
| `app.collect.adaptive_store.models` | `app.stages.collect.adaptive.store.models` |
| `app.collect.adaptive_store.store` | `app.stages.collect.adaptive.store.store` |
| `app.collect.assets` | `app.stages.collect.assets` |
| `app.collect.collection` | `app.stages.collect.collection` |
| `app.collect.contracts` | `app.stages.collect.contracts` |
| `app.collect.facts` | `app.stages.collect.facts` |
| `app.collect.imagedecode` | `app.stages.collect.imagedecode` |
| `app.collect.models` | `app.stages.collect.models` |
| `app.collect.readback` | `app.stages.collect.readback` |
| `app.collect.revisions` | `app.stages.collect.revisions` |
| `app.collect.runs` | `app.stages.collect.runs` |
| `app.collect.service` | `app.stages.collect.service` |
| `app.collect.shadow` | `app.stages.collect.shadow` |
| `app.collect.sourceassets` | `app.stages.collect.sourceassets` |
| `app.collect.urls` | `app.stages.collect.urls` |
| `app.connect` | `app.stages.connect` |
| `app.connect.account_models` | `app.stages.connect.account_models` |
| `app.connect.accounts` | `app.stages.connect.accounts` |
| `app.connect.contracts` | `app.stages.connect.contracts` |
| `app.connect.credentials` | `app.stages.connect.credentials` |
| `app.connect.marketplace` | `app.stages.connect.marketplace` |
| `app.connect.marketplace.attestation` | `app.stages.connect.marketplace.attestation` |
| `app.connect.marketplace.attestation_contracts` | `app.stages.connect.marketplace.attestation_contracts` |
| `app.connect.marketplace.attestation_service` | `app.stages.connect.marketplace.attestation_service` |
| `app.connect.marketplace.capability` | `app.stages.connect.marketplace.capability` |
| `app.connect.marketplace.contracts` | `app.stages.connect.marketplace.contracts` |
| `app.connect.marketplace.models` | `app.stages.connect.marketplace.models` |
| `app.connect.marketplace.revision` | `app.stages.connect.marketplace.revision` |
| `app.connect.marketplace.service` | `app.stages.connect.marketplace.service` |
| `app.connect.marketplace.sources` | `app.stages.connect.marketplace.sources` |
| `app.connect.models` | `app.stages.connect.models` |
| `app.connect.proof` | `app.stages.connect.proof` |
| `app.connect.service` | `app.stages.connect.service` |
| `app.connect.sessions` | `app.stages.connect.sessions` |
| `app.connect.singleflight` | `app.stages.connect.singleflight` |
| `app.connect.smartstore` | `app.stages.connect.smartstore` |
| `app.connect.smartstore.credentials` | `app.stages.connect.smartstore.credentials` |
| `app.connect.smartstore.models` | `app.stages.connect.smartstore.models` |
| `app.connect.smartstore.service` | `app.stages.connect.smartstore.service` |
| `app.connect.state` | `app.stages.connect.state` |
| `app.core` | `app.platform.core` |
| `app.core.clock` | `app.platform.core.clock` |
| `app.core.code_identity` | `app.platform.core.code_identity` |
| `app.core.correlation` | `app.platform.core.correlation` |
| `app.core.egress` | `app.platform.core.egress` |
| `app.core.errors` | `app.platform.core.errors` |
| `app.core.execution` | `app.platform.core.execution` |
| `app.core.logging` | `app.platform.core.logging` |
| `app.core.net` | `app.platform.core.net` |
| `app.core.ownership` | `app.platform.core.ownership` |
| `app.core.safe_payload` | `app.platform.core.safe_payload` |
| `app.core.secrets` | `app.platform.core.secrets` |
| `app.core.send_guard` | `app.platform.core.send_guard` |
| `app.db` | `app.platform.db` |
| `app.db.base` | `app.platform.db.base` |
| `app.db.database` | `app.platform.db.database` |
| `app.db.metadata` | `app.platform.db.metadata` |
| `app.db.migrate` | `app.platform.db.migrate` |
| `app.db.migrations.env` | `app.platform.db.migrations.env` |
| `app.db.migrations.versions.0001_m0_foundation` | `app.platform.db.migrations.versions.0001_m0_foundation` |
| `app.db.migrations.versions.0002_m1_supplier_connections` | `app.platform.db.migrations.versions.0002_m1_supplier_connections` |
| `app.db.migrations.versions.0003_m2_marketplace_capabilities` | `app.platform.db.migrations.versions.0003_m2_marketplace_capabilities` |
| `app.db.migrations.versions.0004_m2_permission_attestations` | `app.platform.db.migrations.versions.0004_m2_permission_attestations` |
| `app.db.migrations.versions.0005_error_class_taxonomy` | `app.platform.db.migrations.versions.0005_error_class_taxonomy` |
| `app.db.migrations.versions.0006_m2_marketplace_connections` | `app.platform.db.migrations.versions.0006_m2_marketplace_connections` |
| `app.db.migrations.versions.0007_m3_product_facts_revisions` | `app.platform.db.migrations.versions.0007_m3_product_facts_revisions` |
| `app.db.migrations.versions.0008_m3_collection_runs` | `app.platform.db.migrations.versions.0008_m3_collection_runs` |
| `app.db.migrations.versions.0009_m3_one_revision_per_run` | `app.platform.db.migrations.versions.0009_m3_one_revision_per_run` |
| `app.db.migrations.versions.0010_m3_same_product_pacing` | `app.platform.db.migrations.versions.0010_m3_same_product_pacing` |
| `app.db.migrations.versions.0011_m3_image_reference_diagnostics` | `app.platform.db.migrations.versions.0011_m3_image_reference_diagnostics` |
| `app.db.migrations.versions.0012_m4_product_foundation` | `app.platform.db.migrations.versions.0012_m4_product_foundation` |
| `app.db.migrations.versions.0013_m4_pricing_snapshots` | `app.platform.db.migrations.versions.0013_m4_pricing_snapshots` |
| `app.db.migrations.versions.0014_m4_derived_image_lineage` | `app.platform.db.migrations.versions.0014_m4_derived_image_lineage` |
| `app.db.migrations.versions.0015_m4_quantity_offers` | `app.platform.db.migrations.versions.0015_m4_quantity_offers` |
| `app.db.migrations.versions.0016_m5_registration_foundation` | `app.platform.db.migrations.versions.0016_m5_registration_foundation` |
| `app.db.migrations.versions.0017_m5_registration_execution_scope` | `app.platform.db.migrations.versions.0017_m5_registration_execution_scope` |
| `app.db.migrations.versions.0018_m5_registration_preparation` | `app.platform.db.migrations.versions.0018_m5_registration_preparation` |
| `app.db.migrations.versions.0019_g1_registration_target_policy` | `app.platform.db.migrations.versions.0019_g1_registration_target_policy` |
| `app.db.migrations.versions.0020_g1_registration_category_metadata` | `app.platform.db.migrations.versions.0020_g1_registration_category_metadata` |
| `app.db.migrations.versions.0021_g2_review_items` | `app.platform.db.migrations.versions.0021_g2_review_items` |
| `app.db.migrations.versions.0022_g2_review_coverage` | `app.platform.db.migrations.versions.0022_g2_review_coverage` |
| `app.db.migrations.versions.0023_g2_review_coverage_fence` | `app.platform.db.migrations.versions.0023_g2_review_coverage_fence` |
| `app.db.migrations.versions.0024_adaptive_profile_validation` | `app.platform.db.migrations.versions.0024_adaptive_profile_validation` |
| `app.db.migrations.versions.0025_adaptive_shadow_foundation` | `app.platform.db.migrations.versions.0025_adaptive_shadow_foundation` |
| `app.db.migrations.versions.0026_g3_live_authority` | `app.platform.db.migrations.versions.0026_g3_live_authority` |
| `app.db.migrations.versions.0027_adaptive_capture_seam` | `app.platform.db.migrations.versions.0027_adaptive_capture_seam` |
| `app.db.migrations.versions.0028_phase_c_read_accounting` | `app.platform.db.migrations.versions.0028_phase_c_read_accounting` |
| `app.db.migrations.versions.0029_g3_restore_retention` | `app.platform.db.migrations.versions.0029_g3_restore_retention` |
| `app.db.migrations.versions.0030_g3_visual_acceptance` | `app.platform.db.migrations.versions.0030_g3_visual_acceptance` |
| `app.db.schema_contract` | `app.platform.db.schema_contract` |
| `app.db.types` | `app.platform.db.types` |
| `app.jobs` | `app.capabilities.jobs` |
| `app.jobs.diagnostic` | `app.capabilities.jobs.diagnostic` |
| `app.jobs.models` | `app.capabilities.jobs.models` |
| `app.jobs.policy` | `app.capabilities.jobs.policy` |
| `app.jobs.records` | `app.capabilities.jobs.records` |
| `app.jobs.registry` | `app.capabilities.jobs.registry` |
| `app.jobs.runner` | `app.capabilities.jobs.runner` |
| `app.jobs.service` | `app.capabilities.jobs.service` |
| `app.jobs.worker` | `app.capabilities.jobs.worker` |
| `app.live` | `app.capabilities.live_safety` |
| `app.live.assets` | `app.capabilities.live_safety.assets` |
| `app.live.authority` | `app.capabilities.live_safety.authority` |
| `app.live.drill` | `app.capabilities.live_safety.drill` |
| `app.live.gates` | `app.capabilities.live_safety.gates` |
| `app.live.model` | `app.capabilities.live_safety.model` |
| `app.live.models` | `app.capabilities.live_safety.models` |
| `app.live.proofs` | `app.capabilities.live_safety.proofs` |
| `app.live.retention` | `app.capabilities.live_safety.retention` |
| `app.live.stack` | `app.capabilities.live_safety.stack` |
| `app.live.status` | `app.capabilities.live_safety.status` |
| `app.live.store` | `app.capabilities.live_safety.store` |
| `app.live.visual` | `app.capabilities.live_safety.visual` |
| `app.operate` | `app.stages.operate` |
| `app.operate.service` | `app.stages.operate.service` |
| `app.products` | `app.stages.products` |
| `app.products.catalog` | `app.stages.products.catalog` |
| `app.products.contracts` | `app.stages.products.contracts` |
| `app.products.image_model` | `app.stages.products.image_model` |
| `app.products.image_models` | `app.stages.products.image_models` |
| `app.products.image_store` | `app.stages.products.image_store` |
| `app.products.images` | `app.stages.products.images` |
| `app.products.materialization` | `app.stages.products.materialization` |
| `app.products.model` | `app.stages.products.model` |
| `app.products.models` | `app.stages.products.models` |
| `app.products.pricing` | `app.stages.products.pricing` |
| `app.products.pricing_service` | `app.stages.products.pricing_service` |
| `app.products.pricing_store` | `app.stages.products.pricing_store` |
| `app.products.quantity` | `app.stages.products.quantity` |
| `app.products.readiness` | `app.stages.products.readiness` |
| `app.products.service` | `app.stages.products.service` |
| `app.products.store` | `app.stages.products.store` |
| `app.register` | `app.stages.register` |
| `app.register.authoring` | `app.stages.register.authoring` |
| `app.register.builder` | `app.stages.register.builder` |
| `app.register.canary` | `app.stages.register.canary` |
| `app.register.category_metadata` | `app.stages.register.category_metadata` |
| `app.register.category_metadata_models` | `app.stages.register.category_metadata_models` |
| `app.register.contracts` | `app.stages.register.contracts` |
| `app.register.drafting` | `app.stages.register.drafting` |
| `app.register.execution` | `app.stages.register.execution` |
| `app.register.model` | `app.stages.register.model` |
| `app.register.models` | `app.stages.register.models` |
| `app.register.payload` | `app.stages.register.payload` |
| `app.register.policy` | `app.stages.register.policy` |
| `app.register.preflight` | `app.stages.register.preflight` |
| `app.register.preparation` | `app.stages.register.preparation` |
| `app.register.provider` | `app.stages.register.provider` |
| `app.register.sanitize` | `app.stages.register.sanitize` |
| `app.register.service` | `app.stages.register.service` |
| `app.register.store` | `app.stages.register.store` |
| `app.register.target_policy` | `app.stages.register.target_policy` |
| `app.register.target_policy_models` | `app.stages.register.target_policy_models` |
| `app.review` | `app.capabilities.review` |
| `app.review.collect_producer` | `app.capabilities.review.collect_producer` |
| `app.review.contracts` | `app.capabilities.review.contracts` |
| `app.review.counts` | `app.capabilities.review.counts` |
| `app.review.coverage` | `app.capabilities.review.coverage` |
| `app.review.model` | `app.capabilities.review.model` |
| `app.review.models` | `app.capabilities.review.models` |
| `app.review.owner` | `app.capabilities.review.owner` |
| `app.review.preflight_producer` | `app.capabilities.review.preflight_producer` |
| `app.review.products_producer` | `app.capabilities.review.products_producer` |
| `app.review.reconciler` | `app.capabilities.review.reconciler` |
| `app.review.register_producer` | `app.capabilities.review.register_producer` |
| `app.review.scopes` | `app.capabilities.review.scopes` |
| `app.review.service` | `app.capabilities.review.service` |
| `app.screens` | `app.interface.screens` |
| `app.screens.contracts` | `app.interface.screens.contracts` |
| `app.screens.service` | `app.interface.screens.service` |
| `app.system` | `app.platform.system` |
| `app.system.diagnostics` | `app.platform.system.diagnostics` |
| `app.system.execution_mode` | `app.platform.system.execution_mode` |
| `app.system.readiness` | `app.platform.system.readiness` |
| `app.system.secret_scan` | `app.platform.system.secret_scan` |
| `scripts.g3_visual_acceptance` | `automation.acceptance.gate3_visual.g3_visual_acceptance` |
| `scripts.g3visual` | `automation.acceptance.gate3_visual.harness` |
| `scripts.g3visual.checker` | `automation.acceptance.gate3_visual.harness.checker` |
| `scripts.g3visual.harness` | `automation.acceptance.gate3_visual.harness.harness` |
| `scripts.g3visual.scenario` | `automation.acceptance.gate3_visual.harness.scenario` |
| `scripts.m0_acceptance` | `automation.acceptance.m0.m0_acceptance` |
| `scripts.m1_acceptance` | `automation.archive.m1.m1_acceptance` |
| `scripts.m2_acceptance` | `automation.acceptance.m2.m2_acceptance` |
| `scripts.m2harness` | `automation.acceptance.m2.harness` |
| `scripts.m2harness.campaign` | `automation.acceptance.m2.harness.campaign` |
| `scripts.m2harness.cli` | `automation.acceptance.m2.harness.cli` |
| `scripts.m2harness.crash` | `automation.acceptance.m2.harness.crash` |
| `scripts.m2harness.evidence` | `automation.acceptance.m2.harness.evidence` |
| `scripts.m2harness.fake_provider` | `automation.acceptance.m2.harness.fake_provider` |
| `scripts.m2harness.gates` | `automation.acceptance.m2.harness.gates` |
| `scripts.m2harness.keyrings` | `automation.acceptance.m2.harness.keyrings` |
| `scripts.m2harness.ledger` | `automation.acceptance.m2.harness.ledger` |
| `scripts.m2harness.paths` | `automation.acceptance.m2.harness.paths` |
| `scripts.m2harness.transport` | `automation.acceptance.m2.harness.transport` |
| `scripts.m3_accept` | `automation.acceptance.m3.m3_accept` |
| `scripts.m3_collect` | `automation.acceptance.m3.m3_collect` |
| `scripts.m3_recon` | `automation.archive.m3.m3_recon` |
| `scripts.m3accept` | `automation.acceptance.m3.campaign` |
| `scripts.m3accept.campaign` | `automation.acceptance.m3.campaign.campaign` |
| `scripts.m3accept.gateways` | `automation.acceptance.m3.campaign.gateways` |
| `scripts.m3accept.ledger` | `automation.acceptance.m3.campaign.ledger` |
| `scripts.m3accept.m1` | `automation.acceptance.m3.campaign.m1` |
| `scripts.m3accept.manifest` | `automation.acceptance.m3.campaign.manifest` |
| `scripts.m3accept.prep` | `automation.acceptance.m3.campaign.prep` |
| `scripts.m3collect` | `automation.acceptance.m3.rehearsal` |
| `scripts.m3collect.fake_shop` | `automation.acceptance.m3.rehearsal.fake_shop` |
| `scripts.m3collect.runner` | `automation.acceptance.m3.rehearsal.runner` |
| `scripts.m3harness` | `automation.acceptance.m3.recon` |
| `scripts.m3harness.capture` | `automation.acceptance.m3.recon.capture` |
| `scripts.m3harness.cli` | `automation.acceptance.m3.recon.cli` |
| `scripts.m3harness.fake_site` | `automation.acceptance.m3.recon.fake_site` |
| `scripts.m3harness.inventory` | `automation.acceptance.m3.recon.inventory` |
| `scripts.m3harness.ledger` | `automation.acceptance.m3.recon.ledger` |
| `scripts.m3harness.paths` | `automation.acceptance.m3.recon.paths` |
| `scripts.m3harness.recon` | `automation.acceptance.m3.recon.recon` |
| `scripts.m4_acceptance` | `automation.acceptance.m4.m4_acceptance` |
| `scripts.m4accept` | `automation.acceptance.common` |
| `scripts.m4accept.checkout` | `automation.acceptance.common.checkout` |
| `scripts.m4accept.evidence` | `automation.acceptance.common.evidence` |
| `scripts.m4accept.guards` | `automation.acceptance.common.guards` |
| `scripts.m4accept.harness` | `automation.acceptance.m4.harness` |
| `scripts.m4accept.operator` | `automation.acceptance.common.operator` |
| `scripts.m4accept.owners` | `automation.acceptance.m4.owners` |
| `scripts.m4accept.root` | `automation.acceptance.common.root` |
| `scripts.m4accept.synthetic` | `automation.acceptance.common.synthetic` |
| `scripts.m5_acceptance` | `automation.acceptance.m5.m5_acceptance` |
| `scripts.m5accept` | `automation.acceptance.m5.harness` |
| `scripts.m5accept.guards` | `automation.acceptance.m5.harness.guards` |
| `scripts.m5accept.harness` | `automation.acceptance.m5.harness.harness` |
| `scripts.m5accept.owners` | `automation.acceptance.m5.harness.owners` |
| `scripts.m5accept.root` | `automation.acceptance.m5.harness.root` |
| `scripts.m5accept.seams` | `automation.acceptance.m5.harness.seams` |
| `scripts.m5accept.synthetic` | `automation.acceptance.m5.harness.synthetic` |
| `scripts.phase_c` | `automation.adaptive.phase_c.phase_c` |
| `scripts.phasec` | `automation.adaptive.phase_c.harness` |
| `scripts.phasec.artifacts` | `automation.adaptive.phase_c.harness.artifacts` |
| `scripts.phasec.ceilings` | `automation.adaptive.phase_c.harness.ceilings` |
| `scripts.phasec.grants` | `automation.adaptive.phase_c.harness.grants` |
| `scripts.phasec.harness` | `automation.adaptive.phase_c.harness.harness` |
| `scripts.phasec.ledger` | `automation.adaptive.phase_c.harness.ledger` |
| `scripts.phasec.roots` | `automation.adaptive.phase_c.harness.roots` |
| `scripts.visual_check` | `automation.acceptance.m0.visual_check` |
| `tests.adaptive_support` | `tests.support.adaptive_support` |
| `tests.collect_submit_support` | `tests.support.collect_submit_support` |
| `tests.collect_support` | `tests.support.collect_support` |
| `tests.gate1_support` | `tests.support.gate1_support` |
| `tests.integration.test_adaptive_phase_c_c0` | `tests.integration.collect.adaptive.test_adaptive_phase_c_c0` |
| `tests.integration.test_adaptive_phase_c_prep0` | `tests.integration.collect.adaptive.test_adaptive_phase_c_prep0` |
| `tests.integration.test_adaptive_shadow` | `tests.integration.collect.adaptive.test_adaptive_shadow` |
| `tests.integration.test_adaptive_store` | `tests.integration.collect.adaptive.test_adaptive_store` |
| `tests.integration.test_api` | `tests.integration.interface.api.test_api` |
| `tests.integration.test_authoring_revision_ownership` | `tests.integration.register.test_authoring_revision_ownership` |
| `tests.integration.test_authoring_unowned_revisions` | `tests.integration.register.test_authoring_unowned_revisions` |
| `tests.integration.test_authoring_unowned_revisions_ui` | `tests.integration.register.test_authoring_unowned_revisions_ui` |
| `tests.integration.test_collect_diagnostic_contract_reproduction` | `tests.integration.collect.test_collect_diagnostic_contract_reproduction` |
| `tests.integration.test_collect_image_acceptance_path` | `tests.integration.collect.test_collect_image_acceptance_path` |
| `tests.integration.test_collect_image_reference_diagnostics` | `tests.integration.collect.test_collect_image_reference_diagnostics` |
| `tests.integration.test_collect_product_collection` | `tests.integration.collect.test_collect_product_collection` |
| `tests.integration.test_collect_run_lifecycle` | `tests.integration.collect.test_collect_run_lifecycle` |
| `tests.integration.test_collect_source_asset_path` | `tests.integration.collect.test_collect_source_asset_path` |
| `tests.integration.test_collect_source_truth_store` | `tests.integration.collect.test_collect_source_truth_store` |
| `tests.integration.test_connect_api` | `tests.integration.connect.test_connect_api` |
| `tests.integration.test_connect_collection_session` | `tests.integration.connect.test_connect_collection_session` |
| `tests.integration.test_connect_credentials` | `tests.integration.connect.test_connect_credentials` |
| `tests.integration.test_connect_lifecycle` | `tests.integration.connect.test_connect_lifecycle` |
| `tests.integration.test_g1a_settings_ui` | `tests.integration.register.test_g1a_settings_ui` |
| `tests.integration.test_g1a_target_policy` | `tests.integration.register.test_g1a_target_policy` |
| `tests.integration.test_g1b_category_metadata` | `tests.integration.register.test_g1b_category_metadata` |
| `tests.integration.test_g1b_category_metadata_ui` | `tests.integration.register.test_g1b_category_metadata_ui` |
| `tests.integration.test_g1c_product_db` | `tests.integration.products.test_g1c_product_db` |
| `tests.integration.test_g1c_product_db_ui` | `tests.integration.products.test_g1c_product_db_ui` |
| `tests.integration.test_g1d_draft_command` | `tests.integration.register.test_g1d_draft_command` |
| `tests.integration.test_g1d_draft_ui` | `tests.integration.register.test_g1d_draft_ui` |
| `tests.integration.test_g1d_gate1_rehearsal` | `tests.integration.register.test_g1d_gate1_rehearsal` |
| `tests.integration.test_g1e_collect_submit` | `tests.integration.collect.test_g1e_collect_submit` |
| `tests.integration.test_g1e_collect_submit_ui` | `tests.integration.collect.test_g1e_collect_submit_ui` |
| `tests.integration.test_g2a_review_owner` | `tests.integration.review.test_g2a_review_owner` |
| `tests.integration.test_g2b_collect_review` | `tests.integration.review.test_g2b_collect_review` |
| `tests.integration.test_g2b_collect_review_ui` | `tests.integration.review.test_g2b_collect_review_ui` |
| `tests.integration.test_g2c_review_counts` | `tests.integration.review.test_g2c_review_counts` |
| `tests.integration.test_g2c_review_counts_ui` | `tests.integration.review.test_g2c_review_counts_ui` |
| `tests.integration.test_g2c_review_paths` | `tests.integration.review.test_g2c_review_paths` |
| `tests.integration.test_g2c_review_paths_ui` | `tests.integration.review.test_g2c_review_paths_ui` |
| `tests.integration.test_g3a_live_authority` | `tests.integration.live_safety.test_g3a_live_authority` |
| `tests.integration.test_g3a_live_create` | `tests.integration.live_safety.test_g3a_live_create` |
| `tests.integration.test_g3b_restore_retention` | `tests.integration.live_safety.test_g3b_restore_retention` |
| `tests.integration.test_g3c_visual_acceptance` | `tests.integration.live_safety.test_g3c_visual_acceptance` |
| `tests.integration.test_g3c_visual_checker_ui` | `tests.harness.gate3_visual.test_g3c_visual_checker_ui` |
| `tests.integration.test_job_runner` | `tests.integration.jobs.test_job_runner` |
| `tests.integration.test_job_terminal_owner` | `tests.integration.jobs.test_job_terminal_owner` |
| `tests.integration.test_job_terminal_reconciliation` | `tests.integration.jobs.test_job_terminal_reconciliation` |
| `tests.integration.test_m2_harness_crash` | `tests.harness.m2.test_m2_harness_crash` |
| `tests.integration.test_m2_harness_dry_run` | `tests.harness.m2.test_m2_harness_dry_run` |
| `tests.integration.test_m3_accept_campaign` | `tests.harness.m3.test_m3_accept_campaign` |
| `tests.integration.test_m3_collect_rehearsal` | `tests.harness.m3.test_m3_collect_rehearsal` |
| `tests.integration.test_m3_recon_dry` | `tests.harness.m3.test_m3_recon_dry` |
| `tests.integration.test_m4_acceptance` | `tests.harness.m4.test_m4_acceptance` |
| `tests.integration.test_m4_images` | `tests.integration.products.test_m4_images` |
| `tests.integration.test_m4_materialization` | `tests.integration.products.test_m4_materialization` |
| `tests.integration.test_m4_pricing` | `tests.integration.products.test_m4_pricing` |
| `tests.integration.test_m4_product_foundation` | `tests.integration.products.test_m4_product_foundation` |
| `tests.integration.test_m4_quantity_offers` | `tests.integration.products.test_m4_quantity_offers` |
| `tests.integration.test_m5_acceptance` | `tests.harness.m5.test_m5_acceptance` |
| `tests.integration.test_m5_register_adapter` | `tests.integration.register.test_m5_register_adapter` |
| `tests.integration.test_m5_register_api` | `tests.integration.register.test_m5_register_api` |
| `tests.integration.test_m5_register_execution` | `tests.integration.register.test_m5_register_execution` |
| `tests.integration.test_m5_register_ui` | `tests.integration.register.test_m5_register_ui` |
| `tests.integration.test_m5_registration_foundation` | `tests.integration.register.test_m5_registration_foundation` |
| `tests.integration.test_m5_registration_preflight` | `tests.integration.register.test_m5_registration_preflight` |
| `tests.integration.test_marketplace_attestation_store` | `tests.integration.connect.test_marketplace_attestation_store` |
| `tests.integration.test_marketplace_capability_store` | `tests.integration.connect.test_marketplace_capability_store` |
| `tests.integration.test_migrations` | `tests.integration.platform.db.test_migrations` |
| `tests.integration.test_ownership_app` | `tests.integration.platform.core.test_ownership_app` |
| `tests.integration.test_ownership_children` | `tests.integration.platform.core.test_ownership_children` |
| `tests.integration.test_ownership_processes` | `tests.integration.platform.core.test_ownership_processes` |
| `tests.integration.test_register_admission_facts` | `tests.integration.live_safety.test_register_admission_facts` |
| `tests.integration.test_smartstore_capability_projection` | `tests.integration.connect.test_smartstore_capability_projection` |
| `tests.integration.test_smartstore_connect` | `tests.integration.connect.test_smartstore_connect` |
| `tests.integration.test_smartstore_operator_api` | `tests.integration.connect.test_smartstore_operator_api` |
| `tests.integration.test_smartstore_operator_ui` | `tests.integration.connect.test_smartstore_operator_ui` |
| `tests.live_support` | `tests.support.live_safety_support` |
| `tests.product_support` | `tests.support.product_support` |
| `tests.register_support` | `tests.support.register_support` |
| `tests.shadow_support` | `tests.support.shadow_support` |
| `tests.suppliers` | `tests.support.fake_suppliers` |
| `tests.support` | `tests.support.jobs_support` |
| `tests.unit.adaptive` | `tests.unit.collect.adaptive.engine` |
| `tests.unit.adaptive.conftest` | `tests.unit.collect.adaptive.engine.conftest` |
| `tests.unit.adaptive.test_canonical` | `tests.unit.collect.adaptive.engine.test_canonical` |
| `tests.unit.adaptive.test_capture` | `tests.unit.collect.adaptive.engine.test_capture` |
| `tests.unit.adaptive.test_capture_candidate` | `tests.unit.collect.adaptive.engine.test_capture_candidate` |
| `tests.unit.adaptive.test_extraction` | `tests.unit.collect.adaptive.engine.test_extraction` |
| `tests.unit.adaptive.test_hooks` | `tests.unit.collect.adaptive.engine.test_hooks` |
| `tests.unit.adaptive.test_identity` | `tests.unit.collect.adaptive.engine.test_identity` |
| `tests.unit.adaptive.test_lint` | `tests.unit.collect.adaptive.engine.test_lint` |
| `tests.unit.adaptive.test_profiles` | `tests.unit.collect.adaptive.engine.test_profiles` |
| `tests.unit.adaptive.test_validation` | `tests.unit.collect.adaptive.engine.test_validation` |
| `tests.unit.adaptive_shadow` | `tests.unit.collect.adaptive.shadow` |
| `tests.unit.adaptive_shadow.test_compare` | `tests.unit.collect.adaptive.shadow.test_compare` |
| `tests.unit.adaptive_shadow.test_evidence` | `tests.unit.collect.adaptive.shadow.test_evidence` |
| `tests.unit.phasec` | `tests.harness.phase_c` |
| `tests.unit.phasec.test_campaign_units` | `tests.harness.phase_c.test_campaign_units` |
| `tests.unit.test_collect_facts` | `tests.unit.collect.test_collect_facts` |
| `tests.unit.test_collect_fetch_target_contract` | `tests.unit.collect.test_collect_fetch_target_contract` |
| `tests.unit.test_collect_gateway` | `tests.unit.collect.test_collect_gateway` |
| `tests.unit.test_collect_image_acceptance` | `tests.unit.collect.test_collect_image_acceptance` |
| `tests.unit.test_collect_image_decode` | `tests.unit.collect.test_collect_image_decode` |
| `tests.unit.test_config` | `tests.unit.platform.core.test_config` |
| `tests.unit.test_connect_state` | `tests.unit.connect.test_connect_state` |
| `tests.unit.test_correlation_and_logging` | `tests.unit.platform.core.test_correlation_and_logging` |
| `tests.unit.test_data_root` | `tests.unit.platform.core.test_data_root` |
| `tests.unit.test_egress` | `tests.unit.platform.core.test_egress` |
| `tests.unit.test_egress_grant` | `tests.unit.platform.core.test_egress_grant` |
| `tests.unit.test_error_taxonomy` | `tests.contracts.test_error_taxonomy` |
| `tests.unit.test_errors` | `tests.unit.platform.core.test_errors` |
| `tests.unit.test_extraction_identity` | `tests.unit.collect.test_extraction_identity` |
| `tests.unit.test_g1c_product_db_contract` | `tests.contracts.test_g1c_product_db_contract` |
| `tests.unit.test_g1e_collect_contract` | `tests.contracts.test_g1e_collect_contract` |
| `tests.unit.test_g3a_replay_key` | `tests.unit.live_safety.test_g3a_replay_key` |
| `tests.unit.test_g3c_visual_checker` | `tests.harness.gate3_visual.test_g3c_visual_checker` |
| `tests.unit.test_image_sample_policy` | `tests.unit.collect.test_image_sample_policy` |
| `tests.unit.test_km_facts_parser` | `tests.unit.integrations.suppliers.kmretail.test_km_facts_parser` |
| `tests.unit.test_km_image_roles` | `tests.unit.integrations.suppliers.kmretail.test_km_image_roles` |
| `tests.unit.test_m2_harness_evidence` | `tests.harness.m2.test_m2_harness_evidence` |
| `tests.unit.test_m2_harness_gates` | `tests.harness.m2.test_m2_harness_gates` |
| `tests.unit.test_m2_harness_ledger` | `tests.harness.m2.test_m2_harness_ledger` |
| `tests.unit.test_m2_harness_operator` | `tests.harness.m2.test_m2_harness_operator` |
| `tests.unit.test_m2_harness_static` | `tests.harness.m2.test_m2_harness_static` |
| `tests.unit.test_m2_harness_transport` | `tests.harness.m2.test_m2_harness_transport` |
| `tests.unit.test_m3_findings_secrets` | `tests.harness.m3.test_m3_findings_secrets` |
| `tests.unit.test_m3_ledger_guard` | `tests.harness.m3.test_m3_ledger_guard` |
| `tests.unit.test_m3_recon_inventory` | `tests.harness.m3.test_m3_recon_inventory` |
| `tests.unit.test_m3_recon_ledger` | `tests.harness.m3.test_m3_recon_ledger` |
| `tests.unit.test_m4_acceptance_harness` | `tests.harness.m4.test_m4_acceptance_harness` |
| `tests.unit.test_m4_product_contract` | `tests.contracts.test_m4_product_contract` |
| `tests.unit.test_m5_preflight_rules` | `tests.unit.register.test_m5_preflight_rules` |
| `tests.unit.test_m5_register_adapter` | `tests.unit.integrations.marketplaces.smartstore.test_m5_register_adapter` |
| `tests.unit.test_m5_register_contract` | `tests.contracts.test_m5_register_contract` |
| `tests.unit.test_m5_register_execution_rules` | `tests.unit.register.test_m5_register_execution_rules` |
| `tests.unit.test_marketplace_attestation` | `tests.unit.connect.test_marketplace_attestation` |
| `tests.unit.test_marketplace_capability` | `tests.unit.connect.test_marketplace_capability` |
| `tests.unit.test_ownership` | `tests.unit.platform.core.test_ownership` |
| `tests.unit.test_pricing` | `tests.unit.products.test_pricing` |
| `tests.unit.test_products_model` | `tests.unit.products.test_products_model` |
| `tests.unit.test_quantity_offers` | `tests.unit.products.test_quantity_offers` |
| `tests.unit.test_repository_rules` | `tests.contracts.test_repository_rules` |
| `tests.unit.test_retry_policy` | `tests.unit.jobs.test_retry_policy` |
| `tests.unit.test_safe_payload` | `tests.unit.platform.core.test_safe_payload` |
| `tests.unit.test_schema_contract` | `tests.unit.platform.db.test_schema_contract` |
| `tests.unit.test_secret_scan` | `tests.unit.platform.system.test_secret_scan` |
| `tests.unit.test_secrets` | `tests.unit.platform.core.test_secrets` |
| `tests.unit.test_smartstore_caller` | `tests.unit.integrations.marketplaces.smartstore.test_smartstore_caller` |
| `tests.unit.test_smartstore_classify` | `tests.unit.integrations.marketplaces.smartstore.test_smartstore_classify` |
| `tests.unit.test_smartstore_image_upload` | `tests.unit.integrations.marketplaces.smartstore.test_smartstore_image_upload` |
| `tests.unit.test_smartstore_product_reads` | `tests.unit.integrations.marketplaces.smartstore.test_smartstore_product_reads` |
| `tests.unit.test_smartstore_registry` | `tests.unit.integrations.marketplaces.smartstore.test_smartstore_registry` |
| `tests.unit.test_smartstore_signing` | `tests.unit.integrations.marketplaces.smartstore.test_smartstore_signing` |
| `tests.unit.test_smartstore_storage` | `tests.unit.connect.test_smartstore_storage` |
| `tests.unit.test_smartstore_transmission` | `tests.unit.integrations.marketplaces.smartstore.test_smartstore_transmission` |
| `tests.unit.test_supplier_credentials` | `tests.unit.connect.test_supplier_credentials` |
| `tests.unit.test_supplier_probes` | `tests.unit.connect.test_supplier_probes` |
| `tests.unit.test_supplier_sessions` | `tests.unit.connect.test_supplier_sessions` |
| `tests.unit.test_supplier_transport` | `tests.unit.integrations.suppliers.test_supplier_transport` |
| `tests.visual_support` | `tests.support.visual_support` |

## 4. Every path

The complete per-path table — all 716 old paths, each exactly once, with new path, disposition
and reason — is `PATH_MIGRATION_MAP.csv` in this directory (columns `old_path,new_path,disposition,reason`).
The directory-level moves it contains are:

| old prefix | new prefix | files |
| --- | --- | --- |
| `(root)/` | `documents/archive/proposals/` | 1 |
| `(root)/` | `documents/roadmap/` | 1 |
| `app/` | `app/capabilities/` | 26 |
| `app/` | `app/interface/` | 18 |
| `app/` | `app/platform/` | 57 |
| `app/` | `app/stages/` | 79 |
| `app/collect/adaptive/` | `app/stages/collect/adaptive/engine/` | 11 |
| `app/collect/adaptive_capture/` | `app/stages/collect/adaptive/phase_c_capture/` | 7 |
| `app/collect/adaptive_shadow/` | `app/stages/collect/adaptive/shadow/` | 7 |
| `app/collect/adaptive_store/` | `app/stages/collect/adaptive/store/` | 4 |
| `app/live/` | `app/capabilities/live_safety/` | 13 |
| `docs/` | `documents/` | 28 |
| `docs/` | `documents/architecture/` | 2 |
| `docs/` | `documents/archive/` | 1 |
| `docs/` | `documents/archive/ui/` | 1 |
| `docs/` | `documents/contracts/` | 8 |
| `docs/` | `documents/contracts/ui/` | 1 |
| `docs/` | `documents/decisions/` | 22 |
| `docs/` | `documents/decisions/architect-reviews/` | 1 |
| `docs/` | `documents/rules/agent-host/` | 1 |
| `docs/acceptance/` | `documents/acceptance/adaptive/` | 1 |
| `docs/acceptance/` | `documents/acceptance/gates/` | 1 |
| `docs/acceptance/` | `documents/acceptance/issues/` | 1 |
| `docs/acceptance/` | `documents/acceptance/milestones/` | 56 |
| `docs/acceptance/evidence/` | `automation/acceptance/m2/` | 1 |
| `docs/acceptance/evidence/` | `documents/acceptance/milestones/M2/` | 3 |
| `docs/acceptance/evidence/` | `documents/archive/acceptance/` | 1 |
| `docs/architecture/` | `documents/architecture/frozen/` | 1 |
| `docs/review/` | `documents/archive/reviews/` | 5 |
| `docs/review/` | `documents/reviews/` | 1 |
| `scripts/` | `automation/acceptance/gate3_visual/` | 1 |
| `scripts/` | `automation/acceptance/m0/` | 2 |
| `scripts/` | `automation/acceptance/m2/` | 1 |
| `scripts/` | `automation/acceptance/m3/` | 2 |
| `scripts/` | `automation/acceptance/m4/` | 1 |
| `scripts/` | `automation/acceptance/m5/` | 1 |
| `scripts/` | `automation/adaptive/phase_c/` | 1 |
| `scripts/` | `automation/archive/m1/` | 1 |
| `scripts/` | `automation/archive/m3/` | 1 |
| `scripts/g3visual/` | `automation/acceptance/gate3_visual/harness/` | 5 |
| `scripts/m2harness/` | `automation/acceptance/m2/harness/` | 11 |
| `scripts/m3accept/` | `automation/acceptance/m3/campaign/` | 7 |
| `scripts/m3collect/` | `automation/acceptance/m3/rehearsal/` | 3 |
| `scripts/m3harness/` | `automation/acceptance/m3/recon/` | 8 |
| `scripts/m4accept/` | `automation/acceptance/common/` | 7 |
| `scripts/m4accept/` | `automation/acceptance/m4/` | 2 |
| `scripts/m5accept/` | `automation/acceptance/m5/harness/` | 7 |
| `scripts/phasec/` | `automation/adaptive/phase_c/harness/` | 7 |
| `tests/` | `tests/support/` | 8 |
| `tests/integration/` | `tests/harness/gate3_visual/` | 1 |
| `tests/integration/` | `tests/harness/m2/` | 2 |
| `tests/integration/` | `tests/harness/m3/` | 3 |
| `tests/integration/` | `tests/harness/m4/` | 1 |
| `tests/integration/` | `tests/harness/m5/` | 1 |
| `tests/integration/` | `tests/integration/collect/` | 9 |
| `tests/integration/` | `tests/integration/collect/adaptive/` | 4 |
| `tests/integration/` | `tests/integration/connect/` | 10 |
| `tests/integration/` | `tests/integration/interface/api/` | 1 |
| `tests/integration/` | `tests/integration/jobs/` | 3 |
| `tests/integration/` | `tests/integration/live_safety/` | 5 |
| `tests/integration/` | `tests/integration/platform/core/` | 3 |
| `tests/integration/` | `tests/integration/platform/db/` | 1 |
| `tests/integration/` | `tests/integration/products/` | 7 |
| `tests/integration/` | `tests/integration/register/` | 16 |
| `tests/integration/` | `tests/integration/review/` | 7 |
| `tests/unit/` | `tests/contracts/` | 6 |
| `tests/unit/` | `tests/harness/gate3_visual/` | 1 |
| `tests/unit/` | `tests/harness/m2/` | 6 |
| `tests/unit/` | `tests/harness/m3/` | 4 |
| `tests/unit/` | `tests/harness/m4/` | 1 |
| `tests/unit/` | `tests/unit/collect/` | 7 |
| `tests/unit/` | `tests/unit/connect/` | 7 |
| `tests/unit/` | `tests/unit/integrations/marketplaces/smartstore/` | 8 |
| `tests/unit/` | `tests/unit/integrations/suppliers/` | 1 |
| `tests/unit/` | `tests/unit/integrations/suppliers/kmretail/` | 2 |
| `tests/unit/` | `tests/unit/jobs/` | 1 |
| `tests/unit/` | `tests/unit/live_safety/` | 1 |
| `tests/unit/` | `tests/unit/platform/core/` | 9 |
| `tests/unit/` | `tests/unit/platform/db/` | 1 |
| `tests/unit/` | `tests/unit/platform/system/` | 1 |
| `tests/unit/` | `tests/unit/products/` | 3 |
| `tests/unit/` | `tests/unit/register/` | 2 |
| `tests/unit/adaptive/` | `tests/unit/collect/adaptive/engine/` | 11 |
| `tests/unit/adaptive_shadow/` | `tests/unit/collect/adaptive/shadow/` | 3 |
| `tests/unit/phasec/` | `tests/harness/phase_c/` | 2 |
| `tools/` | `automation/` | 13 |
| `ui/` | `design/` | 2 |
| `ui/prototypes/` | `design/prototypes/archive/` | 1 |
| `ui/web/js/core/` | `ui/web/js/platforms/smartstore/` | 1 |
| `ui/web/js/pages/` | `ui/web/js/pages/settings/` | 6 |

Individual moves that do not follow a directory prefix:

| old path | new path | disposition |
| --- | --- | --- |
| `docs/acceptance/evidence/README.md` | `automation/acceptance/m2/RUNBOOK.md` | MOVE |
| `docs/platforms/smartstore/CAPABILITY_MAPPING_IMPLEMENTATION_OWNERSHIP.md` | `documents/contracts/platforms/smartstore/CAPABILITY_MAPPING.md` | MERGE |
| `integrations/marketplaces/base.py` | — | REMOVE |
| `tests/live_support.py` | `tests/support/live_safety_support.py` | MOVE |
| `tests/suppliers.py` | `tests/support/fake_suppliers.py` | MOVE |
| `tests/support.py` | `tests/support/jobs_support.py` | MOVE |
| `tests/unit/test_smartstore_binding.py` | `tests/unit/connect/test_marketplace_capability.py` | MERGE |

## 5. Files added by the migration

| path | why |
| --- | --- |
| `app/capabilities/__init__.py` | package marker for a new Python package |
| `app/interface/__init__.py` | package marker for a new Python package |
| `app/platform/__init__.py` | package marker for a new Python package |
| `app/platform/db/migrations/__init__.py` | package marker for a new Python package |
| `app/platform/db/migrations/versions/__init__.py` | package marker for a new Python package |
| `app/stages/__init__.py` | package marker for a new Python package |
| `app/stages/collect/adaptive/__init__.py` | package marker for a new Python package |
| `automation/__init__.py` | package marker for a new Python package |
| `automation/acceptance/__init__.py` | package marker for a new Python package |
| `automation/acceptance/gate3_visual/__init__.py` | package marker for a new Python package |
| `automation/acceptance/m0/__init__.py` | package marker for a new Python package |
| `automation/acceptance/m2/__init__.py` | package marker for a new Python package |
| `automation/acceptance/m3/__init__.py` | package marker for a new Python package |
| `automation/acceptance/m4/__init__.py` | package marker for a new Python package |
| `automation/acceptance/m5/__init__.py` | package marker for a new Python package |
| `automation/adaptive/__init__.py` | package marker for a new Python package |
| `automation/adaptive/phase_c/__init__.py` | package marker for a new Python package |
| `documents/reference/PATH_MIGRATION_MAP.csv` | rule body / index split from CLAUDE.md, or this map |
| `documents/reference/PATH_MIGRATION_MAP.md` | rule body / index split from CLAUDE.md, or this map |
| `documents/reference/REPOSITORY_MAP.md` | rule body / index split from CLAUDE.md, or this map |
| `documents/roadmap/CURRENT-MILESTONE.md` | rule body / index split from CLAUDE.md, or this map |
| `documents/rules/01-roles-and-exchange.md` | rule body / index split from CLAUDE.md, or this map |
| `documents/rules/02-no-legacy.md` | rule body / index split from CLAUDE.md, or this map |
| `documents/rules/03-ui-source.md` | rule body / index split from CLAUDE.md, or this map |
| `documents/rules/04-runtime-stack.md` | rule body / index split from CLAUDE.md, or this map |
| `documents/rules/05-architectural-rules.md` | rule body / index split from CLAUDE.md, or this map |
| `documents/rules/06-immutable-domain-rules.md` | rule body / index split from CLAUDE.md, or this map |
| `documents/rules/07-execution-safety.md` | rule body / index split from CLAUDE.md, or this map |
| `documents/rules/08-git-conventions.md` | rule body / index split from CLAUDE.md, or this map |
| `documents/rules/09-definition-of-done.md` | rule body / index split from CLAUDE.md, or this map |
| `documents/rules/10-working-style.md` | rule body / index split from CLAUDE.md, or this map |
| `documents/rules/12-first-vertical.md` | rule body / index split from CLAUDE.md, or this map |
| `documents/rules/README.md` | rule body / index split from CLAUDE.md, or this map |
| `tests/contracts/__init__.py` | package marker for a new Python package |
| `tests/harness/__init__.py` | package marker for a new Python package |
| `tests/harness/gate3_visual/__init__.py` | package marker for a new Python package |
| `tests/harness/m2/__init__.py` | package marker for a new Python package |
| `tests/harness/m3/__init__.py` | package marker for a new Python package |
| `tests/harness/m4/__init__.py` | package marker for a new Python package |
| `tests/harness/m5/__init__.py` | package marker for a new Python package |
| `tests/integration/collect/__init__.py` | package marker for a new Python package |
| `tests/integration/collect/adaptive/__init__.py` | package marker for a new Python package |
| `tests/integration/connect/__init__.py` | package marker for a new Python package |
| `tests/integration/interface/__init__.py` | package marker for a new Python package |
| `tests/integration/interface/api/__init__.py` | package marker for a new Python package |
| `tests/integration/jobs/__init__.py` | package marker for a new Python package |
| `tests/integration/live_safety/__init__.py` | package marker for a new Python package |
| `tests/integration/platform/__init__.py` | package marker for a new Python package |
| `tests/integration/platform/core/__init__.py` | package marker for a new Python package |
| `tests/integration/platform/db/__init__.py` | package marker for a new Python package |
| `tests/integration/products/__init__.py` | package marker for a new Python package |
| `tests/integration/register/__init__.py` | package marker for a new Python package |
| `tests/integration/review/__init__.py` | package marker for a new Python package |
| `tests/support/__init__.py` | package marker for a new Python package |
| `tests/unit/collect/__init__.py` | package marker for a new Python package |
| `tests/unit/collect/adaptive/__init__.py` | package marker for a new Python package |
| `tests/unit/connect/__init__.py` | package marker for a new Python package |
| `tests/unit/integrations/__init__.py` | package marker for a new Python package |
| `tests/unit/integrations/marketplaces/__init__.py` | package marker for a new Python package |
| `tests/unit/integrations/marketplaces/smartstore/__init__.py` | package marker for a new Python package |
| `tests/unit/integrations/suppliers/__init__.py` | package marker for a new Python package |
| `tests/unit/integrations/suppliers/kmretail/__init__.py` | package marker for a new Python package |
| `tests/unit/jobs/__init__.py` | package marker for a new Python package |
| `tests/unit/live_safety/__init__.py` | package marker for a new Python package |
| `tests/unit/platform/__init__.py` | package marker for a new Python package |
| `tests/unit/platform/core/__init__.py` | package marker for a new Python package |
| `tests/unit/platform/db/__init__.py` | package marker for a new Python package |
| `tests/unit/platform/system/__init__.py` | package marker for a new Python package |
| `tests/unit/products/__init__.py` | package marker for a new Python package |
| `tests/unit/register/__init__.py` | package marker for a new Python package |
