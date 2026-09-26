"""Explicit composition root: every service is built here and nowhere else."""

import os
import uuid
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from datetime import timedelta

from app.audit.service import AuditLog
from app.collect.adaptive.hooks import HookManifest
from app.collect.adaptive_capture.accounting import PhaseCReadAccounting
from app.collect.adaptive_capture.commands import PhaseCCommandStore
from app.collect.adaptive_capture.runner import CaptureRunner
from app.collect.adaptive_capture.store import CaptureStore
from app.collect.adaptive_shadow.runner import ShadowRunner
from app.collect.adaptive_shadow.store import ADR_RETENTION, ShadowEvidenceStore
from app.collect.adaptive_shadow.switch import ShadowSwitch
from app.collect.adaptive_store.gate import (
    SupplierGate,
    build_supplier_gate,
    registered_suppliers,
)
from app.collect.adaptive_store.store import AdaptiveProfileStore, AdaptiveValidationStore
from app.collect.assets import SourceAssetStore
from app.collect.collection import (
    CollectionGateway,
    ProductCollectionService,
    RegisteredCollection,
    SessionProvider,
)
from app.collect.imagedecode import HeaderImageDecoder
from app.collect.readback import SourceTruthReadback
from app.collect.revisions import ProductFactsRevisionStore
from app.collect.runs import CollectionRunStore
from app.collect.service import CollectService
from app.collect.sourceassets import SourceAssetRecorder
from app.config import AppConfig
from app.connect.accounts import MarketplaceAccountStore
from app.connect.credentials import SupplierCredentialStore
from app.connect.marketplace.attestation_service import PermissionAttestationService
from app.connect.marketplace.revision import EndpointMappingRevisionProvider
from app.connect.marketplace.service import MarketplaceCapabilityService
from app.connect.marketplace.sources import ApplicationIdentitySource
from app.connect.service import ConnectService
from app.connect.sessions import (
    MARKETPLACE_SESSIONS_DIR_NAME,
    SESSIONS_DIR_NAME,
    SupplierSessionStore,
)
from app.connect.smartstore.service import SmartStoreConnectService
from app.core.clock import Clock, SystemClock
from app.core.egress import EGRESS
from app.core.ownership import DataDirLease, require_ownership
from app.core.secrets import SecretStore, build_secret_store
from app.db.database import Database, sqlite_database_dir
from app.db.migrate import head_revision
from app.jobs.diagnostic import FAILING_JOB
from app.jobs.policy import RetryPolicy
from app.jobs.registry import JobDefinition, JobRegistry
from app.jobs.runner import JobRunner
from app.jobs.service import JobService
from app.jobs.worker import JobWorker
from app.live.assets import AssetUploadService, PreparationCandidateGate, UnwiredAssetSender
from app.live.authority import LiveAuthorityService
from app.live.model import WireHostPolicy
from app.live.stack import SafetyStack, UnprovenStageProofs
from app.live.store import LiveAuthorityStore
from app.operate.service import OperateService
from app.products.image_store import DerivedImageStore
from app.products.images import ProductImageService
from app.products.materialization import ProductMaterializer
from app.products.pricing_service import ProductPricingService
from app.products.readiness import ProductReadinessService
from app.products.service import ProductsService
from app.products.store import ProductFoundationStore
from app.register.authoring import RegistrationPreparationService
from app.register.builder import RegistrationSnapshotBuilder
from app.register.category_metadata import (
    CategoryMetadataService,
    CategoryMetadataStore,
    DurableRegistrationMetadata,
)
from app.register.drafting import DraftCommandService
from app.register.execution import (
    CREATE_POLICY,
    RegistrationExecutionService,
    create_job_definition,
)
from app.register.preflight import RegistrationPreflightService
from app.register.service import RegisterService
from app.register.store import RegistrationStore
from app.register.target_policy import (
    DurableRegistrationPolicy,
    TargetPolicyService,
    TargetPolicyStore,
    editable_surfaces,
)
from app.review.collect_producer import COLLECT_PRODUCER, CollectReviewProducer
from app.review.counts import ReviewCounts
from app.review.coverage import ReviewCoverageStore
from app.review.owner import ReviewItemStore
from app.review.preflight_producer import PreflightReviewProducer
from app.review.products_producer import ProductsReviewProducer
from app.review.reconciler import ReviewReconciler
from app.review.register_producer import RegisterReviewProducer
from app.review.service import ReviewService
from app.screens.service import ScreenService
from app.system.diagnostics import DiagnosticsService
from app.system.execution_mode import ExecutionModeService
from app.system.readiness import ReadinessService
from integrations.marketplaces.identity import MARKETPLACE_IDENTITIES
from integrations.marketplaces.smartstore import product as smartstore_product
from integrations.marketplaces.smartstore import readback as smartstore_readback
from integrations.marketplaces.smartstore import registry as smartstore_registry
from integrations.marketplaces.smartstore.adoption import SmartStoreAdoption
from integrations.marketplaces.smartstore.caller import SmartStoreEndpointCaller
from integrations.marketplaces.smartstore.execution import MARKETPLACE_KEY as SMARTSTORE_KEY
from integrations.marketplaces.smartstore.execution import (
    SmartStoreCreateSender,
    SmartStoreReadback,
    SmartStoreReconcileLookup,
)
from integrations.marketplaces.smartstore.lookup import SmartStoreDuplicateLookup
from integrations.marketplaces.smartstore.registry import RegistryMappingRevision
from integrations.suppliers.base import SupplierDefinition, SupplierGateway
from integrations.suppliers.collection import SupplierCollection
from integrations.suppliers.extraction import supplier_manifest
from integrations.suppliers.registry import COLLECTIONS, SUPPLIERS
from integrations.suppliers.transport.collection import DeferredCollectionGateway
from integrations.suppliers.transport.gateway import PolicedSupplierGateway


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
    connect: ConnectService
    source_assets: SourceAssetStore
    source_asset_recorder: SourceAssetRecorder
    revisions: ProductFactsRevisionStore
    source_truth: SourceTruthReadback
    collection: ProductCollectionService
    product_store: ProductFoundationStore
    products: ProductsService
    materializer: ProductMaterializer
    pricing: ProductPricingService
    images: ProductImageService
    product_readiness: ProductReadinessService
    accounts: MarketplaceAccountStore
    registrations: RegistrationStore
    target_policies: TargetPolicyService
    category_metadata: CategoryMetadataService
    registration_preflight: RegistrationPreflightService
    registration_preparations: RegistrationPreparationService
    registration_builder: RegistrationSnapshotBuilder
    registration_execution: RegistrationExecutionService
    live_authority: LiveAuthorityService
    safety_stack: SafetyStack
    asset_uploads: AssetUploadService
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
    execution_mode = ExecutionModeService(config.execution_mode, audit)
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

    def after_recorded(collection_run_id: str) -> None:
        """What follows a durably RECORDED run: the Product, then the review fast path. The review
        step never raises into the run; a failure there is a recorded known failure (§4). The
        fast path also asks for a full pass, which covers what the materialization moved in M4
        (G2-C); until it completes, M4's coverage is not current."""
        try:
            materializer.materialize_run(collection_run_id)
        finally:
            run = runs.get(collection_run_id)
            if run is not None and run.source_product_id is not None:
                review_reconciler.index_scope(
                    COLLECT_PRODUCER,
                    {"supplier_key": run.supplier_key, "source_product_id": run.source_product_id},
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
    product_readiness = ProductReadinessService(
        store=product_store, revisions=revisions, pricing=pricing, images=images
    )
    # M5 PR-B (ADR-0014): registration persistence only. Nothing sends, reads back or compares a
    # listing yet, and no marketplace endpoint behind it is adopted.
    # The canonical marketplace-account identity that scopes registration state (M5 PR-B,
    # ACCOUNT_IDENTITY §2): established only from a committed M2 binding, with no provider call.
    accounts = MarketplaceAccountStore(db, clock, audit)
    registrations = RegistrationStore(db, clock, audit)
    # Gate 1 G1-A (ADR-0015 §2): the durable, append-only target policy of each canonical account,
    # saved from Settings. It is the production policy source: an account without a current
    # revision still fails closed with REGISTER_TARGET_POLICY_MISSING.
    target_policy_store = TargetPolicyStore(db, clock, audit)
    target_policies = TargetPolicyService(target_policy_store, accounts)
    # Gate 1 G1-B (ADR-0015 §3): the durable operator-reviewed category metadata of each
    # marketplace × taxonomy × category. No provider category endpoint is adopted; a category with
    # no current revision still fails closed with CATEGORY_METADATA_MISSING.
    category_metadata_store = CategoryMetadataStore(db, clock, audit)
    category_metadata = CategoryMetadataService(
        category_metadata_store, frozenset(m.key for m in MARKETPLACE_IDENTITIES)
    )
    # M5 PR-C (ADR-0014 §3): the derived preflight and the Snapshot builder. No provider is behind
    # either; both sources it reads are the durable owners above.
    registration_preflight = RegistrationPreflightService(
        registrations=registrations,
        readiness=product_readiness,
        pricing=pricing,
        images=images,
        capability=marketplace_capability,
        metadata=DurableRegistrationMetadata(category_metadata_store),
        policies=DurableRegistrationPolicy(target_policy_store),
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
    registration_preparations = RegistrationPreparationService(
        registrations=registrations,
        preflight=registration_preflight,
        builder=registration_builder,
        duplicate_lookup=SmartStoreDuplicateLookup(),
    )
    # M5 PR-E (ADR-0014 §9-§11): the execution owner over the M0 job system. Its CREATE seam is
    # the production SmartStore one, which is unavailable while the endpoint is NOT_ADOPTED, so
    # no code path here can mutate the marketplace; the read-back seam is PR-D's adopted one.
    # Gate 3 area 1 (ADR-0018 §3, §3.4, §4, §10): the pre-LIVE safety owners. The stack reads the
    # execution-mode owner, whose M0 policy refuses every LIVE write, and no eligibility, restore,
    # retention or visual proof exists yet, so every mutation it judges is refused at this main.
    live_store = LiveAuthorityStore(db, clock, audit)
    safety_stack = SafetyStack(
        store=live_store, mode=execution_mode, proofs=UnprovenStageProofs(), clock=clock
    )
    live_authority = LiveAuthorityService(
        store=live_store, registrations=registrations, preparations=registration_preparations
    )
    registration_execution = RegistrationExecutionService(
        registrations=registrations,
        preflight=registration_preflight,
        sender=SmartStoreCreateSender(),
        readback=SmartStoreReadback(
            caller=smartstore_caller or SmartStoreEndpointCaller(),
            bearer=lambda: None,
        ),
        lookup=SmartStoreReconcileLookup(),
        capability=marketplace_capability,
        compare=smartstore_readback,
        projection=smartstore_product.project,
        clock=clock,
        authority=safety_stack,
    )
    registry.register(create_job_definition(registration_execution, retry_policy=CREATE_POLICY))
    # The ASSET upload path (§3.4) with its durable attempt owner. No provider sender is wired:
    # the sender declares the adopted wire endpoint (replay key, readiness) and sends nothing.
    upload_wire = smartstore_registry.wire_identity(
        smartstore_registry.EndpointId.SMARTSTORE_PRODUCT_IMAGE_UPLOAD
    )
    asset_uploads = AssetUploadService(
        store=live_store,
        stack=safety_stack,
        sender=UnwiredAssetSender(
            marketplace_key=SMARTSTORE_KEY,
            wire=upload_wire,
            contract_label=smartstore_registry.SMARTSTORE_ENDPOINT_MAPPING_REVISION,
            adopted=smartstore_registry.EndpointId.SMARTSTORE_PRODUCT_IMAGE_UPLOAD
            in smartstore_registry.ADOPTED,
        ),
        hosts=WireHostPolicy(
            {SMARTSTORE_KEY: smartstore_registry.canonical_host()},
            {SMARTSTORE_KEY: smartstore_registry.HOST_ALIASES},
        ),
        candidates=PreparationCandidateGate(registration_preparations, registrations),
        clock=clock,
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
            RegisterReviewProducer(registrations),
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
    register_service = RegisterService(
        registrations=registrations,
        execution=registration_execution,
        preflight=registration_preflight,
        authoring=registration_preparations,
        accounts=accounts,
        jobs=jobs,
        capability=marketplace_capability,
        adoption=SmartStoreAdoption(),
        execution_mode=execution_mode.state().mode.value,
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
        connect=connect,
        source_assets=source_assets,
        source_asset_recorder=source_asset_recorder,
        revisions=revisions,
        source_truth=SourceTruthReadback(revisions, source_assets),
        collection=collection,
        product_store=product_store,
        products=products,
        materializer=materializer,
        pricing=pricing,
        images=images,
        product_readiness=product_readiness,
        accounts=accounts,
        registrations=registrations,
        target_policies=target_policies,
        category_metadata=category_metadata,
        registration_preflight=registration_preflight,
        registration_preparations=registration_preparations,
        registration_builder=registration_builder,
        registration_execution=registration_execution,
        live_authority=live_authority,
        safety_stack=safety_stack,
        asset_uploads=asset_uploads,
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
