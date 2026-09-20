"""The production owners one M5 acceptance run drives, composed from its dedicated data root.

They are composed exactly as ``app.container.build_container`` composes them: the same classes,
over the same database, clock, audit log and data-root directories. The M4 product owners are
built because M5 reads their truth, and the M5 owners are built whole — the registration store,
the preflight and Snapshot builder, the execution owner over the real M0 job system, and the
Registration Management read model.

**Only the provider seams are local fakes** (`seams.py`), and only the ones whose contracts are
NOT_ADOPTED: the CREATE handoff, the reconcile lookup and the read-back. The production seams are
constructed too, so the run can prove they refuse locally — which is what keeps a marketplace
mutation unreachable rather than merely unused.

The data root is owned the way the application owns it (ADR-0006): the lease is taken before the
database is migrated or opened and released only after it is disposed, and a restart closes the
owners and opens them again over the same root.
"""

import os
from dataclasses import dataclass
from pathlib import Path

from app import __version__
from app.audit.service import AuditLog
from app.collect.assets import SourceAssetStore
from app.collect.imagedecode import HeaderImageDecoder
from app.collect.revisions import ProductFactsRevisionStore
from app.collect.runs import CollectionRunStore
from app.config import AppConfig, database_path
from app.connect.accounts import MarketplaceAccountStore
from app.connect.marketplace.service import MarketplaceCapabilityService
from app.core.clock import Clock, SystemClock
from app.core.ownership import DataDirLease, acquire_data_dir
from app.db.database import Database
from app.db.migrate import current_revision, upgrade_to_head
from app.jobs.policy import RetryPolicy
from app.jobs.registry import JobRegistry
from app.jobs.runner import JobRunner
from app.jobs.service import JobService
from app.products.image_store import DerivedImageStore
from app.products.images import ProductImageService
from app.products.materialization import ProductMaterializer
from app.products.pricing_service import ProductPricingService
from app.products.readiness import ProductReadinessService
from app.products.service import ProductsService
from app.products.store import ProductFoundationStore
from app.register.authoring import RegistrationPreparationService
from app.register.builder import RegistrationSnapshotBuilder
from app.register.execution import (
    CREATE_POLICY,
    ExecutionPolicy,
    RegistrationExecutionService,
    create_job_definition,
)
from app.register.policy import StaticRegistrationMetadata, StaticRegistrationPolicy
from app.register.preflight import RegistrationPreflightService
from app.register.service import RegisterService
from app.register.store import RegistrationStore
from integrations.marketplaces.smartstore import readback as smartstore_readback
from integrations.marketplaces.smartstore.adoption import SmartStoreAdoption
from scripts.m5accept.seams import (
    DeclaredComparator,
    DeclaredProjector,
    FakeReadback,
    FakeSender,
    RecordingLookup,
)

MARKETPLACE = "smartstore"


@dataclass
class Owners:
    config: AppConfig
    lease: DataDirLease
    db: Database
    audit: AuditLog
    clock: Clock
    runs: CollectionRunStore
    revisions: ProductFactsRevisionStore
    source_assets: SourceAssetStore
    derived_images: DerivedImageStore
    product_store: ProductFoundationStore
    materializer: ProductMaterializer
    products: ProductsService
    pricing: ProductPricingService
    images: ProductImageService
    product_readiness: ProductReadinessService
    accounts: MarketplaceAccountStore
    capability: MarketplaceCapabilityService
    registrations: RegistrationStore
    preflight: RegistrationPreflightService
    builder: RegistrationSnapshotBuilder
    preparations: RegistrationPreparationService
    execution: RegistrationExecutionService
    register: RegisterService
    jobs: JobService
    runner: JobRunner
    registry: JobRegistry
    sender: FakeSender
    readback: FakeReadback
    lookup: RecordingLookup
    projector: DeclaredProjector
    comparator: DeclaredComparator

    @property
    def database_file(self) -> Path:
        return database_path(self.config.data_dir)

    def database_revision(self) -> str | None:
        return current_revision(self.db.engine)

    def close(self) -> None:
        try:
            self.db.dispose()
        finally:
            self.lease.release()


def open_owners(
    data_dir: Path,
    *,
    migrate: bool,
    clock: Clock | None = None,
    policy: ExecutionPolicy | None = None,
) -> Owners:
    """Own ``data_dir``, migrate it when asked, and compose the owners over it."""
    data_dir.mkdir(parents=True, exist_ok=True)
    lease = acquire_data_dir(data_dir, app_version=__version__)
    try:
        config = AppConfig(data_dir=data_dir)
        if migrate:
            upgrade_to_head(config.database_url, ownership=lease)
        the_clock = clock or SystemClock()
        db = Database(config.database_url)
        audit = AuditLog(db, the_clock)
        revisions = ProductFactsRevisionStore(db, the_clock)
        product_store = ProductFoundationStore(db, the_clock)
        pricing = ProductPricingService(
            store=product_store, revisions=revisions, audit=audit, clock=the_clock
        )
        derived_images = DerivedImageStore(config.derived_images_dir, db, HeaderImageDecoder())
        images = ProductImageService(
            store=product_store, artifacts=derived_images, audit=audit, clock=the_clock
        )
        readiness = ProductReadinessService(
            store=product_store, revisions=revisions, pricing=pricing, images=images
        )
        products = ProductsService(product_store)
        accounts = MarketplaceAccountStore(db, the_clock, audit)
        capability = MarketplaceCapabilityService(db=db, clock=the_clock, audit=audit)
        registrations = RegistrationStore(db, the_clock, audit)
        preflight = RegistrationPreflightService(
            registrations=registrations,
            readiness=readiness,
            pricing=pricing,
            images=images,
            capability=capability,
            metadata=StaticRegistrationMetadata(),
            policies=StaticRegistrationPolicy(),
        )
        builder = RegistrationSnapshotBuilder(preflight=preflight, registrations=registrations)
        preparations = RegistrationPreparationService(
            registrations=registrations, preflight=preflight, builder=builder
        )
        sender, readback, lookup = FakeSender(), FakeReadback(), RecordingLookup()
        projector = DeclaredProjector()
        comparator = DeclaredComparator(smartstore_readback)
        execution = RegistrationExecutionService(
            registrations=registrations,
            preflight=preflight,
            sender=sender,
            readback=readback,
            lookup=lookup,
            capability=capability,
            compare=comparator,
            projection=projector,
            clock=the_clock,
            policy=policy or ExecutionPolicy(),
        )
        registry = JobRegistry()
        registry.register(create_job_definition(execution, retry_policy=CREATE_POLICY))
        default_policy = RetryPolicy(
            max_attempts=config.job_max_attempts,
            base_delay_s=config.job_backoff_base_s,
            factor=config.job_backoff_factor,
            max_delay_s=config.job_backoff_max_s,
        )
        jobs = JobService(db, registry, the_clock, default_policy)
        runner = JobRunner(
            db,
            registry,
            the_clock,
            audit,
            default_policy=default_policy,
            worker_id=f"m5-acceptance-{os.getpid()}",
            lease_s=config.job_lease_s,
        )
        return Owners(
            config=config,
            lease=lease,
            db=db,
            audit=audit,
            clock=the_clock,
            runs=CollectionRunStore(db, the_clock),
            revisions=revisions,
            source_assets=SourceAssetStore(
                config.source_assets_dir, db, HeaderImageDecoder(), the_clock
            ),
            derived_images=derived_images,
            product_store=product_store,
            materializer=ProductMaterializer(
                db=db, store=product_store, revisions=revisions, audit=audit
            ),
            products=products,
            pricing=pricing,
            images=images,
            product_readiness=readiness,
            accounts=accounts,
            capability=capability,
            registrations=registrations,
            preflight=preflight,
            builder=builder,
            execution=execution,
            preparations=preparations,
            register=RegisterService(
                registrations=registrations,
                execution=execution,
                preflight=preflight,
                authoring=preparations,
                accounts=accounts,
                jobs=jobs,
                capability=capability,
                adoption=SmartStoreAdoption(),
            ),
            jobs=jobs,
            runner=runner,
            registry=registry,
            sender=sender,
            readback=readback,
            lookup=lookup,
            projector=projector,
            comparator=comparator,
        )
    except BaseException:
        lease.release()
        raise
