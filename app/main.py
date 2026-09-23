"""FastAPI application factory."""

import logging
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from starlette.middleware.trustedhost import TrustedHostMiddleware

from app import MILESTONE, __version__
from app.api.errors import install_error_handlers
from app.api.middleware import ClientHeaderGuard, RequestContextMiddleware
from app.api.routes import (
    collect,
    connect,
    diagnostics,
    products,
    register,
    screens,
    settings,
    system,
)
from app.collect.collection import CollectionGateway, RegisteredCollection, SessionProvider
from app.config import AppConfig
from app.connect.marketplace.revision import EndpointMappingRevisionProvider
from app.connect.marketplace.sources import ApplicationIdentitySource
from app.container import build_container
from app.core.egress import EGRESS
from app.core.logging import configure_logging
from app.core.ownership import DataDirLease, acquire_data_dir, require_ownership
from app.jobs.registry import JobDefinition
from integrations.marketplaces.smartstore.caller import SmartStoreEndpointCaller
from integrations.suppliers.base import SupplierGateway

logger = logging.getLogger("icbm.app")

ALLOWED_HOSTS = ["127.0.0.1", "localhost"]


def create_app(
    config: AppConfig | None = None,
    *,
    ownership: DataDirLease | None = None,
    extra_jobs: Sequence[JobDefinition] = (),
    supplier_gateway: SupplierGateway | None = None,
    application_identity: ApplicationIdentitySource | None = None,
    mapping_revision: EndpointMappingRevisionProvider | None = None,
    smartstore_caller: SmartStoreEndpointCaller | None = None,
    collection_gateway: CollectionGateway | None = None,
    collection_sessions: SessionProvider | None = None,
    collections: Sequence[RegisteredCollection] | None = None,
) -> FastAPI:
    """Build the application for one data directory (ADR-0006).

    Without an injected ``ownership`` lease the factory acquires the data-directory lock itself,
    before any per-directory side effect, and releases it when the application stops. Any
    launcher therefore owns the directory; none can bypass the lock. An injected lease must cover
    ``config.data_dir`` itself.

    The collection transport, its session source and its supplier definitions are handed straight
    to the container, like the supplier gateway: a test serves the real application over a
    scripted shop, and a production launcher passes none of them.
    """
    config = config or AppConfig.from_env()
    owns_lease = ownership is None
    lease = ownership or acquire_data_dir(config.data_dir, app_version=__version__)
    try:
        # Before the log file, the database and the worker (ADR-0006).
        require_ownership(lease, config.data_dir)
        log_file = configure_logging(config.log_level, config.log_dir)
        EGRESS.install()
        services = build_container(
            config,
            ownership=lease,
            extra_jobs=extra_jobs,
            supplier_gateway=supplier_gateway,
            application_identity=application_identity,
            mapping_revision=mapping_revision,
            smartstore_caller=smartstore_caller,
            collection_gateway=collection_gateway,
            collection_sessions=collection_sessions,
            collections=collections,
        )
    except BaseException:
        if owns_lease:
            lease.release()
        raise

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        logger.info(
            "app.starting",
            extra={
                "version": __version__,
                "milestone": MILESTONE,
                "data_dir": str(config.data_dir),
                "bind": f"{config.host}:{config.port}",
                "execution_mode": config.execution_mode,
                "diagnostics_enabled": config.diagnostics_enabled,
                "log_file": str(log_file) if log_file else None,
                "owner_lock": str(lease.lock_path),
            },
        )
        if services.readiness.schema_at_head():
            # Metadata only: a new process holds no connection proof and makes no supplier
            # request until a supplier capability is first needed (lazy connection).
            services.connect.normalize_on_startup()
            # Likewise a persisted marketplace auth READY is never trusted (M2 PR-B, §17 #17).
            services.marketplace_capability.normalize_on_startup()
            await services.worker.start()
        else:
            logger.error("app.schema_not_at_head", extra={"hint": "run `icbm db upgrade`"})
        try:
            yield
        finally:
            await services.worker.stop()
            services.db.dispose()
            logger.info("app.stopped")
            if owns_lease:
                lease.release()

    app = FastAPI(
        title="ICBM-NEW",
        version=__version__,
        lifespan=lifespan,
        # Swagger/ReDoc pull assets from a CDN; the loopback app loads nothing external.
        docs_url=None,
        redoc_url=None,
        openapi_url="/api/openapi.json",
    )
    app.state.container = services
    install_error_handlers(app)
    app.include_router(system.router)
    app.include_router(diagnostics.router)
    app.include_router(screens.router)
    app.include_router(connect.router)
    app.include_router(collect.router)
    app.include_router(products.router)
    app.include_router(register.router)
    app.include_router(settings.router)

    # Starlette wraps in reverse order: RequestContextMiddleware ends up outermost.
    app.add_middleware(ClientHeaderGuard)
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=ALLOWED_HOSTS)
    app.add_middleware(RequestContextMiddleware)

    if config.ui_dir.is_dir():
        app.mount("/", StaticFiles(directory=config.ui_dir, html=True), name="ui")
    return app
