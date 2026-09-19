"""The production owners one M4 acceptance run drives, composed from its dedicated data root.

They are composed exactly as ``app.container.build_container`` composes them: the same classes,
over the same database, clock, audit log and data-root directories. Only the M4 owners and the
COLLECT source-truth stores are built. No CONNECT service, supplier gateway, collection transport,
SmartStore caller, job worker or secret store exists in the run, so none can be reached.

The data root is owned the way the application owns it (ADR-0006): the lease is taken before the
database is migrated or opened, and released only after the database is disposed. A restart closes
the owners and opens them again over the same root.
"""

from dataclasses import dataclass
from pathlib import Path

from app import __version__
from app.audit.service import AuditLog
from app.collect.assets import SourceAssetStore
from app.collect.imagedecode import HeaderImageDecoder
from app.collect.revisions import ProductFactsRevisionStore
from app.collect.runs import CollectionRunStore
from app.config import AppConfig, database_path
from app.core.clock import SystemClock
from app.core.ownership import DataDirLease, acquire_data_dir
from app.db.database import Database
from app.db.migrate import current_revision, upgrade_to_head
from app.products.image_store import DerivedImageStore
from app.products.images import ProductImageService
from app.products.materialization import ProductMaterializer
from app.products.pricing_service import ProductPricingService
from app.products.readiness import ProductReadinessService
from app.products.service import ProductsService
from app.products.store import ProductFoundationStore
from app.register.service import RegisterService


@dataclass
class Owners:
    config: AppConfig
    lease: DataDirLease
    db: Database
    audit: AuditLog
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
    register: RegisterService

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


def open_owners(data_dir: Path, *, migrate: bool) -> Owners:
    """Own ``data_dir``, migrate it when asked, and compose the owners over it."""
    data_dir.mkdir(parents=True, exist_ok=True)
    lease = acquire_data_dir(data_dir, app_version=__version__)
    try:
        config = AppConfig(data_dir=data_dir)
        if migrate:
            upgrade_to_head(config.database_url, ownership=lease)
        clock = SystemClock()
        db = Database(config.database_url)
        audit = AuditLog(db, clock)
        revisions = ProductFactsRevisionStore(db, clock)
        product_store = ProductFoundationStore(db, clock)
        pricing = ProductPricingService(
            store=product_store, revisions=revisions, audit=audit, clock=clock
        )
        derived_images = DerivedImageStore(config.derived_images_dir, db, HeaderImageDecoder())
        images = ProductImageService(
            store=product_store, artifacts=derived_images, audit=audit, clock=clock
        )
        return Owners(
            config=config,
            lease=lease,
            db=db,
            audit=audit,
            runs=CollectionRunStore(db, clock),
            revisions=revisions,
            source_assets=SourceAssetStore(
                config.source_assets_dir, db, HeaderImageDecoder(), clock
            ),
            derived_images=derived_images,
            product_store=product_store,
            materializer=ProductMaterializer(
                db=db, store=product_store, revisions=revisions, audit=audit
            ),
            products=ProductsService(product_store),
            pricing=pricing,
            images=images,
            product_readiness=ProductReadinessService(
                store=product_store, revisions=revisions, pricing=pricing, images=images
            ),
            register=RegisterService(),
        )
    except BaseException:
        lease.release()
        raise
