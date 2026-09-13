"""Aggregates every ORM model so Alembic sees the complete canonical schema."""

from app.audit import models as _audit_models  # noqa: F401
from app.connect import models as _connect_models  # noqa: F401
from app.db.base import Base
from app.jobs import models as _job_models  # noqa: F401

metadata = Base.metadata
