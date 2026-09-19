"""Aggregates every ORM model so Alembic sees the complete canonical schema."""

from app.audit import models as _audit_models  # noqa: F401
from app.collect import models as _collect_models  # noqa: F401
from app.connect import account_models as _account_models  # noqa: F401
from app.connect import models as _connect_models  # noqa: F401
from app.connect.marketplace import models as _marketplace_models  # noqa: F401
from app.connect.smartstore import models as _smartstore_models  # noqa: F401
from app.db.base import Base
from app.jobs import models as _job_models  # noqa: F401
from app.products import image_models as _image_models  # noqa: F401
from app.products import models as _product_models  # noqa: F401
from app.register import models as _register_models  # noqa: F401

metadata = Base.metadata
