"""Explicit composition root: every service is built here and nowhere else."""

import logging
import os
import uuid
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path

import integrations.suppliers as supplier_packages
from app.capabilities.audit.service import AuditLog
from app.capabilities.jobs.diagnostic import FAILING_JOB
from app.capabilities.jobs.policy import RetryPolicy
from app.capabilities.jobs.registry import JobDefinition, JobRegistry
from app.capabilities.jobs.runner import JobRunner
from app.capabilities.jobs.service import JobService
from app.capabilities.jobs.worker import JobWorker
from app.capabilities.live_safety.assets import (
    AssetUploadService,
    PreparationCandidateGate,
    PreparedUploadAssets,
)
from app.capabilities.live_safety.authority import LiveAuthorityService
from app.capabilities.live_safety.drill import DrillPaths, RestoreDrillService
from app.capabilities.live_safety.eligibility import CanaryEligibilityService
from app.capabilities.live_safety.gates import CanaryStageReadiness
from app.capabilities.live_safety.model import WireHostPolicy
from app.capabilities.live_safety.proofs import DurableStageProofs
from app.capabilities.live_safety.residual_risk import ResidualRiskAcceptanceService
from app.capabilities.live_safety.retention import RetentionProofService
from app.capabilities.live_safety.stack import SafetyStack
from app.capabilities.live_safety.status import LiveStatusService
from app.capabilities.live_safety.store import LiveAuthorityStore
from app.capabilities.live_safety.upload_run import AssetUploadRun
from app.capabilities.live_safety.visual import VisualAcceptanceService
from app.capabilities.review.collect_producer import COLLECT_PRODUCER, CollectReviewProducer
from app.capabilities.review.counts import ReviewCounts
from app.capabilities.review.coverage import ReviewCoverageStore
from app.capabilities.review.owner import ReviewItemStore
from app.capabilities.review.preflight_producer import PreflightReviewProducer
from app.capabilities.review.products_producer import ProductsReviewProducer
from app.capabilities.review.reconciler import ReviewReconciler
from app.capabilities.review.register_producer import RegisterReviewProducer
from app.capabilities.review.service import ReviewService
from app.config import AppConfig
from app.interface.screens.register_fixes import RegisterFixService
from app.interface.screens.service import ScreenService
from app.platform.core.clock import Clock, SystemClock
from app.platform.core.code_identity import running_checkout_sha, running_code_digest
from app.platform.core.egress import EGRESS
from app.platform.core.ownership import DataDirLease, require_ownership
from app.platform.core.secrets import SecretStore, build_secret_store
from app.platform.db.database import Database, sqlite_database_dir
from app.platform.db.migrate import head_revision
from app.platform.system.diagnostics import DiagnosticsService
from app.platform.system.execution_mode import ExecutionModeService
from app.platform.system.readiness import ReadinessService
from app.stages.collect.adaptive.engine.capture import boundary_of
from app.stages.collect.adaptive.engine.hooks import HookManifest
from app.stages.collect.adaptive.phase_c_capture.accounting import PhaseCReadAccounting
from app.stages.collect.adaptive.phase_c_capture.commands import PhaseCCommandStore
from app.stages.collect.adaptive.phase_c_capture.runner import CaptureRunner
from app.stages.collect.adaptive.phase_c_capture.store import CaptureStore
from app.stages.collect.adaptive.shadow.runner import ShadowRunner
from app.stages.collect.adaptive.shadow.store import ADR_RETENTION, ShadowEvidenceStore
from app.stages.collect.adaptive.shadow.switch import ShadowSwitch
from app.stages.collect.adaptive.store.gate import (
    SupplierGate,
    build_supplier_gate,
    registered_suppliers,
)
from app.stages.collect.adaptive.store.store import AdaptiveProfileStore, AdaptiveValidationStore
from app.stages.collect.assets import SourceAssetStore
from app.stages.collect.collection import (
    CollectionGateway,
    ProductCollectionService,
    RegisteredCollection,
    SessionProvider,
)
from app.stages.collect.extension.buffer import CaptureBuffer
from app.stages.collect.extension.gate import GateResult, security_gate
from app.stages.collect.extension.nonces import NonceCache
from app.stages.collect.extension.pairing import ExtensionPairing
from app.stages.collect.extension.policy import CapturePolicySource
from app.stages.collect.extension.queue import ExtensionQueues
from app.stages.collect.extension.service import ExtensionCaptureService, ReportSink
from app.stages.collect.imagedecode import HeaderImageDecoder
from app.stages.collect.readback import SourceTruthReadback
from app.stages.collect.revisions import ProductFactsRevisionStore
from app.stages.collect.runs import CollectionRunStore
from app.stages.collect.service import CollectService
from app.stages.collect.sourceassets import SourceAssetRecorder
from app.stages.collect.synthetic import SyntheticTestProductService
from app.stages.connect.accounts import MarketplaceAccountStore
from app.stages.connect.credentials import SupplierCredentialStore
from app.stages.connect.marketplace.attestation_service import PermissionAttestationService
from app.stages.connect.marketplace.revision import EndpointMappingRevisionProvider
from app.stages.connect.marketplace.service import MarketplaceCapabilityService
from app.stages.connect.marketplace.sources import ApplicationIdentitySource
from app.stages.connect.service import ConnectService
from app.stages.connect.sessions import (
    MARKETPLACE_SESSIONS_DIR_NAME,
    SESSIONS_DIR_NAME,
    SupplierSessionStore,
)
from app.stages.connect.smartstore.service import SmartStoreConnectService
from app.stages.operate.service import OperateService
from app.stages.products.atomic_sku_item_store import AtomicSKUItemStore
from app.stages.products.atomic_sku_store import AtomicSKUStore
from app.stages.products.auto_images import (
    ImageAutoSelector,
    ImageSlot,
    StaticSupplierImageRoles,
    slot_table,
)
from app.stages.products.common_images import SupplierCommonImageService
from app.stages.products.common_option_mapping_store import CommonOptionFactMappingStore
from app.stages.products.common_option_store import CommonSalesOptionStore
from app.stages.products.image_store import DerivedImageStore
from app.stages.products.images import ProductImageService
from app.stages.products.materialization import Materialization, ProductMaterializer
from app.stages.products.pricing_service import ProductPricingService
from app.stages.products.readiness import ProductReadinessService
from app.stages.products.service import ProductsService
from app.stages.products.store import ProductFoundationStore
from app.stages.register.authoring import RegistrationPreparationService
from app.stages.register.authoring_revisions import AuthoringRevisionStore
from app.stages.register.builder import RegistrationSnapshotBuilder
from app.stages.register.bulk import BulkRegistrationService
from app.stages.register.category_catalog import CategoryCatalogService, CategoryCatalogStore
from app.stages.register.category_metadata import (
    CategoryMetadataService,
    CategoryMetadataStore,
    DurableRegistrationMetadata,
)
from app.stages.register.deletion import RegistrationDeletionService
from app.stages.register.drafting import DraftCommandService
from app.stages.register.execution import (
    CREATE_ENDPOINT_GROUP,
    CREATE_POLICY,
    RegistrationExecutionService,
    create_job_definition,
)
from app.stages.register.preflight import RegistrationPreflightService
from app.stages.register.service import RegisterService
from app.stages.register.store import RegistrationStore
from app.stages.register.target_policy import (
    DurableRegistrationPolicy,
    TargetPolicyService,
    TargetPolicyStore,
    editable_surfaces,
)
from integrations.marketplaces.identity import MARKETPLACE_IDENTITIES
from integrations.marketplaces.smartstore import product as smartstore_product
from integrations.marketplaces.smartstore import readback as smartstore_readback
from integrations.marketplaces.smartstore import registry as smartstore_registry
from integrations.marketplaces.smartstore.addressbook import SmartStoreAddressBookSource
from integrations.marketplaces.smartstore.adoption import SmartStoreAdoption
from integrations.marketplaces.smartstore.assets import SmartStoreAssetSender
from integrations.marketplaces.smartstore.caller import SmartStoreEndpointCaller
from integrations.marketplaces.smartstore.category import SmartStoreCategoryCatalogSource
from integrations.marketplaces.smartstore.deletion import SmartStoreDeleteSender
from integrations.marketplaces.smartstore.execution import MARKETPLACE_KEY as SMARTSTORE_KEY
from integrations.marketplaces.smartstore.execution import (
    SmartStoreCreateSender,
    SmartStoreReadback,
    SmartStoreReconcileLookup,
)
from integrations.marketplaces.smartstore.lookup import SmartStoreDuplicateLookup
from integrations.marketplaces.smartstore.notice_catalog import SmartStoreNoticeCatalog
from integrations.marketplaces.smartstore.notice_schema import SmartStoreNoticeRules
from integrations.marketplaces.smartstore.registry import RegistryMappingRevision
from integrations.suppliers.base import SupplierDefinition, SupplierGateway
from integrations.suppliers.collection import ImageRole as SupplierImageRole
from integrations.suppliers.collection import SupplierCollection
from integrations.suppliers.extraction import supplier_manifest
from integrations.suppliers.kmretail import PROFILE as KM_PROFILE
from integrations.suppliers.kmretail.collect.images import OG_IMAGE_RULE as KM_OG_IMAGE_RULE
from integrations.suppliers.kmretail.collect.images import ROLE_RULES as KM_ROLE_RULES
from integrations.suppliers.registry import COLLECTIONS, SUPPLIERS
from integrations.suppliers.transport.collection import DeferredCollectionGateway
from integrations.suppliers.transport.gateway import PolicedSupplierGateway

logger = logging.getLogger("icbm.container")

# Where the supplier packages, and with them each reviewed capture policy, live.
SUPPLIER_PACKAGES = Path(supplier_packages.__file__).resolve().parent


def _server_final_scan(html: str) -> GateResult:
    """The server's security gate over an extension capture (ADR-0019 §6.1, the user's decision of
    2026-10-01): ``app.stages.collect.extension.gate``, naming each boundary as the capture owner
    names it. Nothing blocking means the capture goes on as it arrived."""
    return security_gate(html, boundary_of=boundary_of)


@dataclass
class Container:
    config: AppConfig
    clock: Clock
    db: Database
    audit: AuditLog
    job_registry: JobRegistry
    jobs: JobService
    runner: JobRunner
    worker: JobWorker
    secrets: SecretStore
    execution_mode: ExecutionModeService
    diagnostics: DiagnosticsService
    readiness: ReadinessService
    screens: ScreenService
    # B-UX2: the fix-only projection of Registration Management (screens layer).
    register_fixes: RegisterFixService
    connect: ConnectService
    source_assets: SourceAssetStore
    source_asset_recorder: SourceAssetRecorder
    revisions: ProductFactsRevisionStore
    source_truth: SourceTruthReadback
    collection: ProductCollectionService
    synthetic_products: SyntheticTestProductService
    extension_pairing: ExtensionPairing
    extension_capture: ExtensionCaptureService
    extension_queues: ExtensionQueues
    product_store: ProductFoundationStore
    common_sales_options: CommonSalesOptionStore
    common_option_fact_mappings: CommonOptionFactMappingStore
    atomic_skus: AtomicSKUStore
    atomic_sku_items: AtomicSKUItemStore
    products: ProductsService
    materializer: ProductMaterializer
    pricing: ProductPricingService
    images: ProductImageService
    common_images: SupplierCommonImageService
    auto_images: ImageAutoSelector
    product_readiness: ProductReadinessService
    accounts: MarketplaceAccountStore
    registrations: RegistrationStore
    authoring_revisions: AuthoringRevisionStore
    target_policies: TargetPolicyService
    category_metadata: CategoryMetadataService
    category_catalog: CategoryCatalogService
    # Settings delivery policy: the seller's address book, read on request and never stored.
    smartstore_addressbook: SmartStoreAddressBookSource
    registration_preflight: RegistrationPreflightService
    registration_preparations: RegistrationPreparationService
    registration_builder: RegistrationSnapshotBuilder
    registration_execution: RegistrationExecutionService
    bulk_registration: BulkRegistrationService
    live_authority: LiveAuthorityService
    canary_eligibility: CanaryEligibilityService
    residual_risk: ResidualRiskAcceptanceService
    safety_stack: SafetyStack
    asset_uploads: AssetUploadService
    # The operator's upload run (`icbm live upload-assets`, owner decision 5975217061).
    asset_upload_run: AssetUploadRun
    registration_deletions: RegistrationDeletionService
    notice_catalog: SmartStoreNoticeCatalog
    restore_drills: RestoreDrillService
    retention: RetentionProofService
    visual_acceptance: VisualAcceptanceService
    live_status: LiveStatusService
    register: RegisterService
    drafting: DraftCommandService
    marketplace_capability: MarketplaceCapabilityService
    permission_attestation: PermissionAttestationService
    smartstore: SmartStoreConnectService
    review_items: ReviewItemStore
    review_reconciler: ReviewReconciler
    adaptive_profiles: AdaptiveProfileStore
    adaptive_validation: AdaptiveValidationStore
    shadow_switch: ShadowSwitch
    shadow_evidence: ShadowEvidenceStore
    capture_store: CaptureStore
    phase_c_commands: PhaseCCommandStore
    phase_c_reads: PhaseCReadAccounting
    ownership: DataDirLease


def supplier_image_roles() -> dict[str, dict[str, ImageSlot]]:
    """Each supplier's published image role rules, as the image auto-selection reads them
    (Issue #219): rule name → slot. A layout or unrecognised role is no product image."""
    slot_of = {
        SupplierImageRole.PRIMARY.value: ImageSlot.REPRESENTATIVE,
        SupplierImageRole.THUMBNAIL.value: ImageSlot.ADDITIONAL,
        SupplierImageRole.DETAIL.value: ImageSlot.DETAIL,
        SupplierImageRole.PRODUCT_AUX.value: ImageSlot.AUXILIARY,
    }.get
    return {
        KM_PROFILE.supplier_key: slot_table(
            ((rule.rule_id, rule.role.value) for rule in (*KM_ROLE_RULES, KM_OG_IMAGE_RULE)),
            slot_of,
        )
    }


def build_container(
    config: AppConfig,
    *,
    ownership: DataDirLease,
    clock: Clock | None = None,
    secret_store: SecretStore | None = None,
    extra_jobs: Sequence[JobDefinition] = (),
    supplier_gateway: SupplierGateway | None = None,
    collection_gateway: CollectionGateway | None = None,
    collection_sessions: SessionProvider | None = None,
    collections: Sequence[RegisteredCollection] | None = None,
    suppliers: Sequence[SupplierDefinition] = SUPPLIERS,
    application_identity: ApplicationIdentitySource | None = None,
    mapping_revision: EndpointMappingRevisionProvider | None = None,
    smartstore_caller: SmartStoreEndpointCaller | None = None,
    adaptive_supplier_gate: SupplierGate | None = None,
    adaptive_hook_manifests: Mapping[str, HookManifest] | None = None,
    capture_policy_root: Path | None = None,
    extension_report_sink: ReportSink | None = None,
) -> Container:
    """Compose the application for one data directory.

    This opens the application database for writing, so the lease must cover that database's own
    directory before anything is opened (ADR-0006). It never acquires the lock itself and never
    creates the directory; acquisition belongs to the process entry point.
    """
    require_ownership(ownership, sqlite_database_dir(config.database_url))
    clock = clock or SystemClock()
    db = Database(config.database_url)
    audit = AuditLog(db, clock)

    registry = JobRegistry()
    registry.register(FAILING_JOB)
    for definition in extra_jobs:
        registry.register(definition)

    policy = RetryPolicy(
        max_attempts=config.job_max_attempts,
        base_delay_s=config.job_backoff_base_s,
        factor=config.job_backoff_factor,
        max_delay_s=config.job_backoff_max_s,
    )
    jobs = JobService(db, registry, clock, policy)
    runner = JobRunner(
        db,
        registry,
        clock,
        audit,
        default_policy=policy,
        worker_id=f"worker-{os.getpid()}",
        lease_s=config.job_lease_s,
    )
    worker = JobWorker(runner, poll_interval_s=config.job_poll_interval_s, clock=clock)
    jobs.set_worker_notifier(worker.notify)

    secrets = secret_store or build_secret_store(config.secret_backend)
    connect = ConnectService(
        db=db,
        clock=clock,
        audit=audit,
        jobs=jobs,
        credentials=SupplierCredentialStore(secrets),
        sessions=SupplierSessionStore(config.runtime_dir / SESSIONS_DIR_NAME, secrets),
        gateway=supplier_gateway or PolicedSupplierGateway(browser_channel=config.browser_channel),
        suppliers=suppliers,
        marketplaces=MARKETPLACE_IDENTITIES,
    )
    registry.register(connect.job_definition())
    # Marketplace capability truth: typed evidence in, no provider access (M2 PR-B).
    marketplace_capability = MarketplaceCapabilityService(db=db, clock=clock, audit=audit)
    # SmartStore CONNECT (M2 PR-A): every provider call goes through the registry-gated caller.
    smartstore = SmartStoreConnectService(
        db=db,
        clock=clock,
        audit=audit,
        secrets=secrets,
        sessions=SupplierSessionStore(
            config.runtime_dir / MARKETPLACE_SESSIONS_DIR_NAME, secrets, namespace="marketplace"
        ),
        capability=marketplace_capability,
        caller=smartstore_caller or SmartStoreEndpointCaller(),
        # AUTH §15: from validated configuration only; unset, CONNECT refuses.
        renewal_margin=(
            timedelta(seconds=config.smartstore_renewal_margin_s)
            if config.smartstore_renewal_margin_s is not None
            else None
        ),
    )
    # SMARTSTORE-A0-PERMISSION (M2 PR-C). The application identity comes from the committed
    # SmartStore credential bundle and the endpoint-mapping revision from the endpoint registry
    # (§5.1, §5.2); test fixtures may stand in for either.
    permission_attestation = PermissionAttestationService(
        db=db,
        clock=clock,
        audit=audit,
        secrets=secrets,
        capability=marketplace_capability,
        identity=application_identity or smartstore,
        revision=mapping_revision or RegistryMappingRevision(),
        max_age_days=config.smartstore_a0_max_age_days,
    )
    marketplace_capability.set_permission_evidence(permission_attestation)
    # ROADMAP §14 item 5: a LIVE window opens, and stays open, only inside an approved bounded
    # LIVE mutation scope — a live grant, the grant owner's durable record (ADR-0018 §2, §3).
    live_store = LiveAuthorityStore(db, clock, audit)
    execution_mode = ExecutionModeService(
        config.execution_mode, audit, clock, scope=live_store.live_scope_until
    )
    diagnostics = DiagnosticsService(
        enabled=config.diagnostics_enabled, db=db, jobs=jobs, audit=audit
    )
    readiness = ReadinessService(
        db=db,
        worker=worker,
        secrets=secrets,
        egress=EGRESS,
        execution_mode=execution_mode,
        clock=clock,
        head_revision=head_revision(),
        ownership=ownership,
        capabilities=connect.capabilities,
    )

    # COLLECT source truth: the content-addressed asset path and the immutable revisions over
    # it. The decoder reads MIME and the original size from the stored bytes themselves.
    source_assets = SourceAssetStore(config.source_assets_dir, db, HeaderImageDecoder(), clock)
    revisions = ProductFactsRevisionStore(db, clock)
    source_asset_recorder = SourceAssetRecorder(source_assets)
    # PRODUCT DB (M4 PR-C): the canonical Product follows durably RECORDED source truth. It is
    # materialized after a run is RECORDED, or by an explicit call; never by a startup sweep.
    product_store = ProductFoundationStore(db, clock)
    common_sales_options = CommonSalesOptionStore(db, clock)
    common_option_fact_mappings = CommonOptionFactMappingStore(db, clock)
    atomic_skus = AtomicSKUStore(db, clock)
    atomic_sku_items = AtomicSKUItemStore(db, clock)
    materializer = ProductMaterializer(db=db, store=product_store, revisions=revisions, audit=audit)
    registered_collections = (
        tuple(_registered(COLLECTIONS)) if collections is None else tuple(collections)
    )
    # Adaptive Collector (ADR-0017; P2 persistence, P3 shadow foundation). A supplier is admitted
    # only with a registered CONNECT definition and a registered COLLECT access envelope. No
    # supplier has a switch entry, so every run freezes DISABLED and the shadow never runs until
    # a separately authorized Phase C opening enables one.
    process_run_id = str(uuid.uuid4())
    adaptive_gate = adaptive_supplier_gate or build_supplier_gate(
        registered_suppliers(suppliers, (r.collection for r in registered_collections))
    )
    adaptive_profiles = AdaptiveProfileStore(db, clock, adaptive_gate)
    adaptive_validation = AdaptiveValidationStore(db, clock, adaptive_gate, adaptive_profiles)
    hook_manifests = dict(adaptive_hook_manifests or {})
    shadow_switch = ShadowSwitch(
        db, clock, adaptive_gate, adaptive_profiles, adaptive_validation, hook_manifests
    )
    shadow_evidence = ShadowEvidenceStore(
        db,
        clock,
        supplier_gate=adaptive_gate,
        profiles=adaptive_profiles,
        retention=ADR_RETENTION,
        process_run_id=process_run_id,
    )
    # Phase C C0: the in-memory sample capture. Off for every run unless the Phase C harness left a
    # capture request for its target, consumed at the run's first reservation.
    # C1 PREP-0: the durable accounting of every send of a Phase-C-accounted collection.
    phase_c_reads = PhaseCReadAccounting(db, clock)
    capture_store = CaptureStore(
        db, clock, supplier_gate=adaptive_gate, validation=adaptive_validation
    )
    runs = CollectionRunStore(
        db, clock, shadow_freezer=shadow_switch.freeze, capture_freezer=capture_store.freeze
    )

    def _auto_select(materialized: Materialization) -> None:
        """Issue #219: the image auto-selection of every Item the run materialized. It never
        raises into the run; a refusal is logged, and the Item stays unselected until retried."""
        item_ids = dict.fromkeys(
            i for i in (materialized.item_id, *materialized.quantity_item_ids) if i is not None
        )
        for item_id in item_ids:
            try:
                auto_images.auto_select(item_id)
            except Exception:
                logger.exception("image auto-selection failed for %s", item_id)

    def after_recorded(collection_run_id: str) -> None:
        """What follows a durably RECORDED run: the Product, then the review fast path. The review
        step never raises into the run; a failure there is a recorded known failure (§4). The
        fast path also asks for a full pass, which covers what the materialization moved in M4
        (G2-C); until it completes, M4's coverage is not current."""
        try:
            materialized = materializer.materialize_run(collection_run_id)
            _auto_select(materialized)
        finally:
            run = runs.get(collection_run_id)
            if run is not None and run.source_product_id is not None:
                review_reconciler.index_scope(
                    COLLECT_PRODUCER,
                    {"supplier_key": run.supplier_key, "source_product_id": run.source_product_id},
                )

    # Owner decision 2026-10-03: an operator's labelled synthetic test product, a copy of one
    # collected revision's facts under the reserved icbm-synthetic namespace; it materializes like
    # any RECORDED run.
    synthetic_products = SyntheticTestProductService(
        db=db,
        runs=runs,
        revisions=revisions,
        audit=audit,
        clock=clock,
        after_recorded=after_recorded,
    )
    # One operator-submitted product at a time, as a durable collect.* job. The transport is
    # deferred: composing the application opens no connection, and under CI it cannot.
    collection = ProductCollectionService(
        db=db,
        clock=clock,
        jobs=jobs,
        runs=runs,
        revisions=revisions,
        recorder=source_asset_recorder,
        sessions=collection_sessions or connect,
        gateway=collection_gateway or DeferredCollectionGateway(),
        collections=registered_collections,
        after_recorded=after_recorded,
        shadow=ShadowRunner(adaptive_profiles, shadow_evidence, hook_manifests),
        capture=CaptureRunner(capture_store),
        accounting=phase_c_reads,
    )
    registry.register(collection.job_definition())

    # ADR-0019 E1/E2: the extension capture transport. The pairing lives in the keyring only, the
    # replay cache and the capture buffer in this process only, and the capture policy is read
    # from the repository on every use. An accepted capture opens a canonical run, and the
    # collection owner records its document through the one pipeline a direct run uses: the
    # server reads no product page for it, and fetches its images through the policed gateway.
    extension_pairing = ExtensionPairing(secrets, clock, NonceCache(clock))
    # ADR-0019 §8.1 (E3): the list queue. Every queue read is issued here, durably, before it
    # happens; a ticketed capture claims its item in the ingest's own write unit.
    extension_queues = ExtensionQueues(
        db=db, clock=clock, runs=runs, collections=registered_collections
    )
    extension_capture = ExtensionCaptureService(
        db=db,
        clock=clock,
        jobs=jobs,
        runs=runs,
        policies=CapturePolicySource(capture_policy_root or SUPPLIER_PACKAGES),
        buffer=CaptureBuffer(),
        final_scan=_server_final_scan,
        # The buffer is a handoff inside one process (ADR-0002 Option A; ruling 5906712259 N-1).
        worker_in_process=worker.IN_PROCESS,
        recorder=collection,
        collections=registered_collections,
        report_sink=extension_report_sink,
        queue_tickets=extension_queues,
    )
    registry.register(extension_capture.job_definition())

    products = ProductsService(product_store)
    # M4 PR-D: pricing per Item and explicit context, and derived product readiness. Neither
    # makes a registration candidate: that is M5's preflight.
    pricing = ProductPricingService(
        store=product_store, revisions=revisions, audit=audit, clock=clock
    )
    # M4 PR-E: derived image lineage, operator image selection and exact-binary QA. Derived
    # bytes live in their own namespace, never among the source assets.
    images = ProductImageService(
        store=product_store,
        artifacts=DerivedImageStore(config.derived_images_dir, db, HeaderImageDecoder()),
        audit=audit,
        clock=clock,
    )
    # Issue #219: the operator's BLOCK / KEEP decisions on supplier common images and their
    # detection over each supplier's collection history.
    common_images = SupplierCommonImageService(db, audit, clock)
    # Issue #219: the image auto-selection rule over each supplier's own published role rules, and
    # its automatic QA. An operator's selection always supersedes it.
    auto_images = ImageAutoSelector(
        store=product_store,
        images=images,
        roles=StaticSupplierImageRoles(supplier_image_roles()),
        audit=audit,
        clock=clock,
    )
    product_readiness = ProductReadinessService(
        store=product_store, revisions=revisions, pricing=pricing, images=images
    )
    # M5 PR-B (ADR-0014): registration persistence only. Nothing sends, reads back or compares a
    # listing yet, and no marketplace endpoint behind it is adopted.
    # The canonical marketplace-account identity that scopes registration state (M5 PR-B,
    # ACCOUNT_IDENTITY §2): established only from a committed M2 binding, with no provider call.
    accounts = MarketplaceAccountStore(db, clock, audit)
    registrations = RegistrationStore(db, clock, audit)
    # The only bearer source for provider metadata reads as well as REGISTER execution. Reading it
    # never renews or commits a token and authorizes no mutation.
    committed_bearer = smartstore.committed_bearer
    # Gate 1 G1-A (ADR-0015 §2): the durable, append-only target policy of each canonical account,
    # saved from Settings. It is the production policy source: an account without a current
    # revision still fails closed with REGISTER_TARGET_POLICY_MISSING.
    # ADR-0014 §27.1 (Issue #89 5907626428): the server-owned category-mapping and
    # detail-composition revisions a newly appended target-policy revision is stamped with.
    authoring_revisions = AuthoringRevisionStore(db, clock, audit)
    target_policy_store = TargetPolicyStore(db, clock, audit, authoring_revisions)
    target_policies = TargetPolicyService(target_policy_store, accounts)
    # Official SmartStore leaf-category catalog (owner decision 2026-10-04): one read-only provider
    # capture becomes an immutable local snapshot. Its digest is the taxonomy revision used by the
    # target policy and category metadata.
    category_catalog_store = CategoryCatalogStore(db, clock, audit)
    category_catalog = CategoryCatalogService(
        category_catalog_store,
        SmartStoreCategoryCatalogSource(
            smartstore_caller or SmartStoreEndpointCaller(), committed_bearer
        ),
        endpoint_mapping_revision=smartstore_registry.SMARTSTORE_ENDPOINT_MAPPING_REVISION,
    )
    # Gate 1 G1-B (ADR-0015 §3): the durable operator-reviewed category metadata of each
    # marketplace × taxonomy × category. New revisions are admitted only for a current provider
    # leaf category; a category with no current metadata still fails closed.
    category_metadata_store = CategoryMetadataStore(db, clock, audit)
    category_metadata = CategoryMetadataService(
        category_metadata_store,
        frozenset(m.key for m in MARKETPLACE_IDENTITIES),
        category_catalog,
    )
    # M5 PR-C (ADR-0014 §3): the derived preflight and the Snapshot builder. No provider is behind
    # either; both sources it reads are the durable owners above.
    registration_preflight = RegistrationPreflightService(
        registrations=registrations,
        readiness=product_readiness,
        pricing=pricing,
        images=images,
        capability=marketplace_capability,
        # Notice coverage S3: a SmartStore category's notice fields are the provider notice
        # schema's own for the reviewed notice type, so metadata, preflight and wire read one
        # contract.
        metadata=DurableRegistrationMetadata(category_metadata_store, SmartStoreNoticeRules()),
        policies=DurableRegistrationPolicy(target_policy_store),
        # B-DETAIL: the detail-composition profile each target names.
        detail_profiles=authoring_revisions,
    )
    # Gate 1 G1-D (ADR-0015 §5): a Draft from the operator's Product DB selection. It composes the
    # owners above — the revalidated selection, the bound account, the current target policy, M4
    # pricing and the registration store — and replaces none of them.
    drafting = DraftCommandService(
        products=products,
        accounts=accounts,
        # The same durable policy source the preflight reads: the current revision, every call.
        policies=DurableRegistrationPolicy(target_policy_store),
        pricing=pricing,
        registrations=registrations,
        marketplaces=[m.key for m in MARKETPLACE_IDENTITIES],
    )
    registration_builder = RegistrationSnapshotBuilder(
        preflight=registration_preflight, registrations=registrations
    )
    # M5 PR-F (ADR-0014 §27, decision 5751540323): the durable operator-authored preparation. It
    # owns inputs only; the preflight still derives every verdict, and the builder still freezes.
    # The durable ASSET upload-attempt owner (ADR-0018 §3.4) is read by the application freeze:
    # the provider assets prepared for the exact candidate being frozen (5919917893 §3).
    registration_preparations = RegistrationPreparationService(
        registrations=registrations,
        preflight=registration_preflight,
        builder=registration_builder,
        duplicate_lookup=SmartStoreDuplicateLookup(),
        prepared_assets=PreparedUploadAssets(live_store),
    )
    # M5 PR-E (ADR-0014 §9-§11): the execution owner over the M0 job system. Its CREATE seam is
    # the production SmartStore one, which is unavailable while the endpoint is NOT_ADOPTED, so
    # no code path here can mutate the marketplace; the read-back seam is PR-D's adopted one.
    # Gate 3 area 1 (ADR-0018 §3, §3.4, §4, §10): the pre-LIVE safety owners. The stack reads the
    # execution-mode owner, whose M0 policy refuses every LIVE write, and no eligibility, restore,
    # retention or visual proof exists yet, so every mutation it judges is refused at this main.
    # Gate 3 area 2 (ADR-0018 §7, §8): the restore-drill and evidence-retention proofs are durable
    # owners. Gate 3 area 3 (§9): the reviewed visual acceptance, current only for exactly the
    # commit this process runs at and its running code digest (both taken once at composition), at
    # the current schema head.
    # Eligibility (§5.1) is its own durable owner, wired below; no record exists for any lineage.
    retention = RetentionProofService(
        db=db,
        store=live_store,
        job_types=registry.job_types,
        safe_retention_profile_version=smartstore_registry.SAFE_RETENTION_PROFILE_VERSION,
        schema_head=head_revision,
    )
    code_sha = running_checkout_sha()
    code_identity = running_code_digest(config.ui_dir)
    visual_acceptance = VisualAcceptanceService(
        store=live_store,
        code_sha=lambda: code_sha,
        code_identity=lambda: code_identity,
        schema_head=head_revision,
    )
    # ADR-0018 §5.1 (Issue #89 5910018106): the canary-eligibility owner. A record proves
    # CANARY_NON_REGULATED only for the exact lineage the stack re-derives at each stage.
    canary_eligibility = CanaryEligibilityService(
        store=live_store, registrations=registrations, preparations=registration_preparations
    )
    # ADR-0018 §6.1, G3-30 (migration 0036): the durable proof of the user and architect
    # residual-risk acceptance recorded in GitHub, for one account and the exact risk contract.
    residual_risk = ResidualRiskAcceptanceService(live_store)
    stage_proofs = DurableStageProofs(
        store=live_store,
        retention=retention,
        visual=visual_acceptance,
        eligibility=canary_eligibility,
        residual_risk=residual_risk,
        schema_head=head_revision,
    )
    safety_stack = SafetyStack(
        store=live_store, mode=execution_mode, proofs=stage_proofs, clock=clock
    )
    live_authority = LiveAuthorityService(
        store=live_store, registrations=registrations, preparations=registration_preparations
    )
    # ROADMAP §14 item 4: the one canonical bearer source of every SmartStore REGISTER seam.
    registration_execution = RegistrationExecutionService(
        registrations=registrations,
        preflight=registration_preflight,
        # ROADMAP §14 item 4: the four SmartStore seams read one canonical bearer source, the
        # CONNECT owner's read-only committed bearer. It answers only for the current committed
        # session of a proven, bound account with more than the renewal margin left, and None
        # otherwise. A bearer permits no mutation: M0_DRY_RUN_ONLY, the brake, the grant and every
        # other send-time layer still refuse CREATE and ASSET, and the wire projection is not
        # sendable while the official evidence leaves a required value uncaptured.
        sender=SmartStoreCreateSender(
            caller=smartstore_caller or SmartStoreEndpointCaller(),
            bearer=committed_bearer,
        ),
        readback=SmartStoreReadback(
            caller=smartstore_caller or SmartStoreEndpointCaller(),
            bearer=committed_bearer,
        ),
        # SEARCH is adopted for positive-only reconcile with the same bearer source.
        lookup=SmartStoreReconcileLookup(
            caller=smartstore_caller or SmartStoreEndpointCaller(),
            bearer=committed_bearer,
        ),
        capability=marketplace_capability,
        compare=smartstore_readback,
        projection=smartstore_product.project,
        clock=clock,
        authority=safety_stack,
    )
    bulk_registration = BulkRegistrationService(db, jobs, clock)
    registry.register(
        create_job_definition(
            registration_execution,
            retry_policy=CREATE_POLICY,
            after_terminal=bulk_registration.settle_terminal,
            additional_unsettled=bulk_registration.unsettled_jobs,
        )
    )
    # The ASSET upload path (§3.4) with its durable attempt owner. The sender is the adopted
    # SmartStore image upload with the same canonical bearer source as the CREATE seams; the
    # send-time stack still refuses every upload under M0_DRY_RUN_ONLY.
    asset_uploads = AssetUploadService(
        store=live_store,
        stack=safety_stack,
        sender=SmartStoreAssetSender(
            caller=smartstore_caller or SmartStoreEndpointCaller(),
            bearer=committed_bearer,
        ),
        hosts=WireHostPolicy(
            {SMARTSTORE_KEY: smartstore_registry.canonical_host()},
            {SMARTSTORE_KEY: smartstore_registry.HOST_ALIASES},
        ),
        candidates=PreparationCandidateGate(registration_preparations, registrations),
        clock=clock,
    )
    # The only production entry point of the upload owner: an `icbm live upload-assets` run in the
    # process that owns the data directory. It composes the grant, CONNECT, the execution-mode
    # window and the M4 lineage stores, and decides nothing itself.
    asset_upload_run = AssetUploadRun(
        store=live_store,
        uploads=asset_uploads,
        mode=execution_mode,
        connect=smartstore.connect,
        sources=source_assets,
        derived=DerivedImageStore(config.derived_images_dir, db, HeaderImageDecoder()),
    )
    # ADR-0018 §3.5: the deletion of one ICBM-confirmed registration, through the same send-time
    # stack and the same canonical bearer source. Its exact DELETE grant is the only authority for
    # it, an unknown deletion is never resent, and only a read-back confirms or resolves one.
    # Notice coverage S0 (owner directive 2026-10-03): a read-only capture of the official
    # 상품정보제공고시 schema with the same canonical bearer source. It reads and stores nothing.
    notice_catalog = SmartStoreNoticeCatalog(
        caller=smartstore_caller or SmartStoreEndpointCaller(),
        bearer=committed_bearer,
    )
    registration_deletions = RegistrationDeletionService(
        registrations=registrations,
        sender=SmartStoreDeleteSender(
            caller=smartstore_caller or SmartStoreEndpointCaller(),
            bearer=committed_bearer,
        ),
        readback=SmartStoreReadback(
            caller=smartstore_caller or SmartStoreEndpointCaller(),
            bearer=committed_bearer,
        ),
        sale_status=lambda retained: smartstore_readback.normalize(retained).sale_status,
        authority=safety_stack,
    )
    # Gate 3 area 3 (§9): the read-only projection of the brake, the grants, their ASSET readiness
    # and the unit-independent proofs, which 등록관리 shows. It writes and authorizes nothing.
    live_status = LiveStatusService(
        store=live_store,
        registrations=registrations,
        assets=asset_uploads,
        retention_ready=retention.ready,
        visual_recorded=visual_acceptance.recorded,
    )
    restore_drills = RestoreDrillService(
        db=db,
        audit=audit,
        paths=DrillPaths(data_dir=config.data_dir, database_path=config.database_path),
        store=live_store,
        stack=safety_stack,
        assets=asset_uploads,
        preparations=registration_preparations,
        registrations=registrations,
        clock=clock,
        schema_head=head_revision,
    )
    # Gate 2 (ADR-0016): the durable ReviewItem owner (G2-A) with its producers: COLLECT / M3
    # (G2-B), M4 base readiness, REGISTER execution and REGISTER preparations (G2-C). Each
    # process run has its own identity:
    # coverage is current only after a complete full pass in this run (§7). The review owner
    # reads these owners; none of them reads it.
    review_items = ReviewItemStore(
        db,
        clock,
        audit,
        producers=[
            CollectReviewProducer(revisions),
            ProductsReviewProducer(product_readiness),
            RegisterReviewProducer(registrations, clock),
            PreflightReviewProducer(
                registrations=registrations,
                preparations=registration_preparations,
                preflight=registration_preflight,
                readiness=product_readiness,
                audit=audit,
            ),
        ],
    )
    review_reconciler = ReviewReconciler(
        review_items,
        ReviewCoverageStore(db, clock, audit),
        clock,
        process_run_id=process_run_id,
        interval_s=config.review_reconcile_interval_s,
        max_age_s=config.review_coverage_max_age_s,
    )
    # M5 PR-F (ADR-0014 §22, §24): the Registration Management read model and its operator
    # actions. It owns no truth of its own — it reads the owners above and hands each action to
    # the owner of that action — and its canary readiness is derived and read-only.
    # ADR-0018 §10: the canary readiness only summarizes the two mutation stages, so it reads each
    # stage's verdict from the owners the send-time stack uses — never from its own weaker copy.
    canary_stages = CanaryStageReadiness(
        stack=safety_stack,
        assets=asset_uploads,
        live=live_store,
        registrations=registrations,
        preparations=registration_preparations,
        create_sender_available=registration_execution.create_sender_available,
        reconcile_path_adopted=registration_execution.reconcile_path_adopted,
        endpoint_group=CREATE_ENDPOINT_GROUP,
    )
    register_service = RegisterService(
        registrations=registrations,
        execution=registration_execution,
        preflight=registration_preflight,
        authoring=registration_preparations,
        accounts=accounts,
        jobs=jobs,
        bulk=bulk_registration,
        capability=marketplace_capability,
        adoption=SmartStoreAdoption(),
        stages=canary_stages,
        # Read at every evaluation: a bounded LIVE window opens and lapses at runtime (§14 item 5).
        execution_mode=lambda: execution_mode.state().mode.value,
        # ADR-0014 §28.5: the status panel shows the ICBM seller code the provider is sent.
        clock=clock,
        seller_code=smartstore_product.seller_management_code,
        # B-PREVIEW: the frozen-Snapshot preview reads the same wire projection the sender uses.
        preview_projection=smartstore_product.project,
    )
    screens = ScreenService(
        clock=clock,
        operator_name=config.operator_name,
        marketplaces=MARKETPLACE_IDENTITIES,
        connect=connect,
        collect=CollectService(jobs),
        products=products,
        register=register_service,
        operate=OperateService(),
        review=ReviewService(ReviewCounts(review_items, review_reconciler)),
        execution_mode=execution_mode,
        editable_surfaces=editable_surfaces(),
        collection_suppliers=collection.supplier_keys(),
    )
    return Container(
        auto_images=auto_images,
        common_images=common_images,
        config=config,
        clock=clock,
        db=db,
        audit=audit,
        job_registry=registry,
        jobs=jobs,
        runner=runner,
        worker=worker,
        secrets=secrets,
        execution_mode=execution_mode,
        diagnostics=diagnostics,
        readiness=readiness,
        screens=screens,
        register_fixes=RegisterFixService(register_service, review_items, clock),
        connect=connect,
        source_assets=source_assets,
        source_asset_recorder=source_asset_recorder,
        revisions=revisions,
        source_truth=SourceTruthReadback(revisions, source_assets),
        collection=collection,
        synthetic_products=synthetic_products,
        extension_pairing=extension_pairing,
        extension_capture=extension_capture,
        extension_queues=extension_queues,
        product_store=product_store,
        common_sales_options=common_sales_options,
        common_option_fact_mappings=common_option_fact_mappings,
        atomic_skus=atomic_skus,
        atomic_sku_items=atomic_sku_items,
        products=products,
        materializer=materializer,
        pricing=pricing,
        images=images,
        product_readiness=product_readiness,
        accounts=accounts,
        registrations=registrations,
        authoring_revisions=authoring_revisions,
        target_policies=target_policies,
        category_metadata=category_metadata,
        category_catalog=category_catalog,
        smartstore_addressbook=SmartStoreAddressBookSource(
            smartstore_caller or SmartStoreEndpointCaller(), committed_bearer
        ),
        registration_preflight=registration_preflight,
        registration_preparations=registration_preparations,
        registration_builder=registration_builder,
        registration_execution=registration_execution,
        bulk_registration=bulk_registration,
        live_authority=live_authority,
        canary_eligibility=canary_eligibility,
        residual_risk=residual_risk,
        safety_stack=safety_stack,
        asset_uploads=asset_uploads,
        asset_upload_run=asset_upload_run,
        registration_deletions=registration_deletions,
        notice_catalog=notice_catalog,
        restore_drills=restore_drills,
        retention=retention,
        visual_acceptance=visual_acceptance,
        live_status=live_status,
        register=register_service,
        drafting=drafting,
        marketplace_capability=marketplace_capability,
        permission_attestation=permission_attestation,
        smartstore=smartstore,
        review_items=review_items,
        review_reconciler=review_reconciler,
        adaptive_profiles=adaptive_profiles,
        adaptive_validation=adaptive_validation,
        shadow_switch=shadow_switch,
        shadow_evidence=shadow_evidence,
        capture_store=capture_store,
        phase_c_commands=PhaseCCommandStore(db, clock),
        phase_c_reads=phase_c_reads,
        ownership=ownership,
    )


def _registered(collections: Sequence[SupplierCollection]) -> Iterator[RegisteredCollection]:
    """Bind each supplier's collection to the extraction identity its own manifest names.

    A revision records the rules that produced it, so a supplier whose image-role rules and
    manifest disagree is refused here rather than storing a revision under a stale identity.
    """
    for collection in collections:
        manifest = supplier_manifest(collection.supplier_key)
        yield RegisteredCollection(
            collection=collection,
            extractor_revision=manifest.revision,
            extractor_fingerprint=manifest.fingerprint,
        )
