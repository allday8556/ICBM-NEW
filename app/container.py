"""Explicit composition root: every service is built here and nowhere else."""

import os
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from datetime import timedelta

from app.audit.service import AuditLog
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
from app.operate.service import OperateService
from app.products.image_store import DerivedImageStore
from app.products.images import ProductImageService
from app.products.materialization import ProductMaterializer
from app.products.pricing_service import ProductPricingService
from app.products.readiness import ProductReadinessService
from app.products.service import ProductsService
from app.products.store import ProductFoundationStore
from app.register.builder import RegistrationSnapshotBuilder
from app.register.execution import (
    CREATE_POLICY,
    RegistrationExecutionService,
    create_job_definition,
)
from app.register.policy import StaticRegistrationMetadata, StaticRegistrationPolicy
from app.register.preflight import RegistrationPreflightService
from app.register.service import RegisterService
from app.register.store import RegistrationStore
from app.review.service import ReviewService
from app.screens.service import ScreenService
from app.system.diagnostics import DiagnosticsService
from app.system.execution_mode import ExecutionModeService
from app.system.readiness import ReadinessService
from integrations.marketplaces.identity import MARKETPLACE_IDENTITIES
from integrations.marketplaces.smartstore import product as smartstore_product
from integrations.marketplaces.smartstore import readback as smartstore_readback
from integrations.marketplaces.smartstore.caller import SmartStoreEndpointCaller
from integrations.marketplaces.smartstore.execution import (
    SmartStoreCreateSender,
    SmartStoreReadback,
    SmartStoreReconcileLookup,
)
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
    registration_preflight: RegistrationPreflightService
    registration_builder: RegistrationSnapshotBuilder
    registration_execution: RegistrationExecutionService
    marketplace_capability: MarketplaceCapabilityService
    permission_attestation: PermissionAttestationService
    smartstore: SmartStoreConnectService
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
    # One operator-submitted product at a time, as a durable collect.* job. The transport is
    # deferred: composing the application opens no connection, and under CI it cannot.
    collection = ProductCollectionService(
        db=db,
        clock=clock,
        jobs=jobs,
        runs=CollectionRunStore(db, clock),
        revisions=revisions,
        recorder=source_asset_recorder,
        sessions=collection_sessions or connect,
        gateway=collection_gateway or DeferredCollectionGateway(),
        collections=(
            tuple(_registered(COLLECTIONS)) if collections is None else tuple(collections)
        ),
        after_recorded=materializer.materialize_run,
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
    # M5 PR-C (ADR-0014 §3): the derived preflight and the Snapshot builder. No provider is behind
    # either. The metadata and policy sources start empty: every category fails closed until PR-D
    # adopts the reviewed marketplace metadata, and every account until Settings owns its policy.
    registration_preflight = RegistrationPreflightService(
        registrations=registrations,
        readiness=product_readiness,
        pricing=pricing,
        images=images,
        capability=marketplace_capability,
        metadata=StaticRegistrationMetadata(),
        policies=StaticRegistrationPolicy(),
    )
    registration_builder = RegistrationSnapshotBuilder(
        preflight=registration_preflight, registrations=registrations
    )
    # M5 PR-E (ADR-0014 §9-§11): the execution owner over the M0 job system. Its CREATE seam is
    # the production SmartStore one, which is unavailable while the endpoint is NOT_ADOPTED, so
    # no code path here can mutate the marketplace; the read-back seam is PR-D's adopted one.
    registration_execution = RegistrationExecutionService(
        registrations=registrations,
        preflight=registration_preflight,
        sender=SmartStoreCreateSender(),
        readback=SmartStoreReadback(
            caller=smartstore_caller or SmartStoreEndpointCaller(),
            bearer=lambda: None,
        ),
        lookup=SmartStoreReconcileLookup(),
        compare=smartstore_readback,
        projection=smartstore_product.project,
        clock=clock,
    )
    registry.register(create_job_definition(registration_execution, retry_policy=CREATE_POLICY))
    screens = ScreenService(
        clock=clock,
        operator_name=config.operator_name,
        marketplaces=MARKETPLACE_IDENTITIES,
        connect=connect,
        collect=CollectService(jobs),
        products=products,
        register=RegisterService(),
        operate=OperateService(),
        review=ReviewService(),
        execution_mode=execution_mode,
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
        registration_preflight=registration_preflight,
        registration_builder=registration_builder,
        registration_execution=registration_execution,
        marketplace_capability=marketplace_capability,
        permission_attestation=permission_attestation,
        smartstore=smartstore,
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
