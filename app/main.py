"""FastAPI application factory."""

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from starlette.middleware.trustedhost import TrustedHostMiddleware

from app import MILESTONE, __version__
from app.api.errors import install_error_handlers
from app.api.middleware import ClientHeaderGuard, RequestContextMiddleware
from app.api.routes import diagnostics, screens, system
from app.config import AppConfig
from app.container import Container, build_container
from app.core.egress import EGRESS
from app.core.logging import configure_logging

logger = logging.getLogger("icbm.app")

ALLOWED_HOSTS = ["127.0.0.1", "localhost"]


def create_app(config: AppConfig | None = None, *, container: Container | None = None) -> FastAPI:
    if container is not None:
        config = container.config
    config = config or AppConfig.from_env()
    log_file = configure_logging(config.log_level, config.log_dir)
    EGRESS.install()
    services = container or build_container(config)

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
            },
        )
        if services.readiness.schema_at_head():
            await services.worker.start()
        else:
            logger.error("app.schema_not_at_head", extra={"hint": "run `icbm db upgrade`"})
        try:
            yield
        finally:
            await services.worker.stop()
            services.db.dispose()
            logger.info("app.stopped")

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

    # Starlette wraps in reverse order: RequestContextMiddleware ends up outermost.
    app.add_middleware(ClientHeaderGuard)
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=ALLOWED_HOSTS)
    app.add_middleware(RequestContextMiddleware)

    if config.ui_dir.is_dir():
        app.mount("/", StaticFiles(directory=config.ui_dir, html=True), name="ui")
    return app
