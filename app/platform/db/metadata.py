"""Aggregates every ORM model so Alembic sees the complete canonical schema."""

from app.capabilities.audit import models as _audit_models  # noqa: F401
from app.capabilities.jobs import models as _job_models  # noqa: F401
from app.capabilities.live_safety import models as _live_models  # noqa: F401
from app.capabilities.review import models as _review_models  # noqa: F401
from app.platform.db.base import Base
from app.stages.collect import models as _collect_models  # noqa: F401
from app.stages.collect.adaptive.phase_c_capture import (
    models as _adaptive_capture_models,  # noqa: F401
)
from app.stages.collect.adaptive.shadow import models as _adaptive_shadow_models  # noqa: F401
from app.stages.collect.adaptive.store import models as _adaptive_models  # noqa: F401
from app.stages.connect import account_models as _account_models  # noqa: F401
from app.stages.connect import models as _connect_models  # noqa: F401
from app.stages.connect.marketplace import models as _marketplace_models  # noqa: F401
from app.stages.connect.smartstore import models as _smartstore_models  # noqa: F401
from app.stages.operate import listing_models as _operate_listing_models  # noqa: F401
from app.stages.operate import order_models as _operate_order_models  # noqa: F401
from app.stages.operate import stock_models as _operate_stock_models  # noqa: F401
from app.stages.products import (
    atomic_sku_economics_models as _atomic_sku_economics_models,  # noqa: F401
)
from app.stages.products import atomic_sku_item_models as _atomic_sku_item_models  # noqa: F401
from app.stages.products import atomic_sku_models as _atomic_sku_models  # noqa: F401
from app.stages.products import (
    common_option_mapping_models as _common_option_mapping_models,  # noqa: F401
)
from app.stages.products import common_option_models as _common_option_models  # noqa: F401
from app.stages.products import image_models as _image_models  # noqa: F401
from app.stages.products import models as _product_models  # noqa: F401
from app.stages.register import authoring_revisions as _authoring_revisions  # noqa: F401
from app.stages.register import category_catalog as _category_catalog_models  # noqa: F401
from app.stages.register import category_metadata_models as _category_metadata_models  # noqa: F401
from app.stages.register import models as _register_models  # noqa: F401
from app.stages.register import target_policy_models as _target_policy_models  # noqa: F401

metadata = Base.metadata
