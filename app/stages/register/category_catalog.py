"""Durable snapshots of the official marketplace leaf-category catalog."""

import hashlib
import json
import re
import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from pydantic import BaseModel
from sqlalchemy import (
    CheckConstraint,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
    func,
    select,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.capabilities.audit.models import AuditEventType, AuditOutcome
from app.capabilities.audit.service import AuditEntry, AuditLog
from app.platform.core.clock import Clock
from app.platform.core.errors import InputValidationError, NotFoundError
from app.platform.db.base import Base
from app.platform.db.database import Database
from app.platform.db.types import UTCDateTime

MARKETPLACE = "smartstore"
CATALOG_VERSION = "marketplace-leaf-category-catalog/v1"
_CATEGORY_ID = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


@dataclass(frozen=True)
class ProviderCategory:
    """Provider-neutral leaf-category fact returned by a catalog source."""

    category_id: str
    name: str
    whole_category_name: str
    leaf: bool


class MarketplaceCategoryCatalogSnapshot(Base):
    __tablename__ = "marketplace_category_catalog_snapshots"
    __table_args__ = (
        UniqueConstraint("marketplace_key", "content_fingerprint"),
        UniqueConstraint("marketplace_key", "taxonomy_revision"),
        UniqueConstraint("marketplace_key", "snapshot_sequence"),
        CheckConstraint("marketplace_key <> ''", name="marketplace_key_present"),
        CheckConstraint("taxonomy_revision <> ''", name="taxonomy_revision_present"),
        CheckConstraint(
            "length(content_fingerprint) = 64 AND content_fingerprint NOT GLOB '*[^0-9a-f]*'",
            name="content_fingerprint_hex",
        ),
        CheckConstraint("entry_count >= 1", name="entry_count_positive"),
        CheckConstraint("snapshot_sequence >= 1", name="snapshot_sequence_positive"),
        CheckConstraint("recorded_by <> ''", name="recorded_by_present"),
        CheckConstraint("correlation_id <> ''", name="correlation_present"),
    )

    snapshot_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    marketplace_key: Mapped[str] = mapped_column(String(40))
    taxonomy_revision: Mapped[str] = mapped_column(String(64))
    content_fingerprint: Mapped[str] = mapped_column(String(64))
    snapshot_sequence: Mapped[int] = mapped_column(Integer)
    entry_count: Mapped[int] = mapped_column(Integer)
    endpoint_mapping_revision: Mapped[str] = mapped_column(String(64))
    recorded_by: Mapped[str] = mapped_column(String(64))
    correlation_id: Mapped[str] = mapped_column(String(64))
    recorded_at: Mapped[datetime] = mapped_column(UTCDateTime)


class MarketplaceCategoryCatalogEntry(Base):
    __tablename__ = "marketplace_category_catalog_entries"
    __table_args__ = (
        UniqueConstraint("snapshot_id", "category_id"),
        Index("ix_marketplace_category_catalog_entries_snapshot_name", "snapshot_id", "name"),
        CheckConstraint("category_id <> ''", name="category_id_present"),
        CheckConstraint("name <> ''", name="name_present"),
        CheckConstraint("whole_category_name <> ''", name="whole_name_present"),
        CheckConstraint("leaf = 1", name="leaf_true"),
    )

    entry_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    snapshot_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("marketplace_category_catalog_snapshots.snapshot_id")
    )
    category_id: Mapped[str] = mapped_column(String(64))
    name: Mapped[str] = mapped_column(String(200))
    whole_category_name: Mapped[str] = mapped_column(String(1000))
    leaf: Mapped[int] = mapped_column(Integer)


class CategoryCatalogSource(Protocol):
    def leaf_categories(self) -> tuple[ProviderCategory, ...]: ...


class CategoryCatalogEntryView(BaseModel):
    category_id: str
    name: str
    whole_category_name: str
    leaf: bool = True


class CategoryCatalogView(BaseModel):
    marketplace_key: str
    snapshot_id: str | None = None
    taxonomy_revision: str | None = None
    content_fingerprint: str | None = None
    endpoint_mapping_revision: str | None = None
    recorded_at: datetime | None = None
    total: int = 0
    entries: tuple[CategoryCatalogEntryView, ...] = ()


@dataclass(frozen=True)
class CatalogSnapshot:
    snapshot_id: str
    marketplace_key: str
    taxonomy_revision: str
    content_fingerprint: str
    endpoint_mapping_revision: str
    recorded_at: datetime
    entries: tuple[ProviderCategory, ...]


def _document(entries: tuple[ProviderCategory, ...]) -> list[dict[str, object]]:
    return [
        {
            "id": item.category_id,
            "last": item.leaf,
            "name": item.name,
            "wholeCategoryName": item.whole_category_name,
        }
        for item in sorted(entries, key=lambda item: item.category_id)
    ]


def _fingerprint(entries: tuple[ProviderCategory, ...]) -> str:
    encoded = json.dumps(
        _document(entries), ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _validate(entries: tuple[ProviderCategory, ...]) -> tuple[ProviderCategory, ...]:
    if not entries:
        raise InputValidationError(
            "CATEGORY_CATALOG_EMPTY",
            "an empty provider response never replaces the current catalog",
        )
    seen: set[str] = set()
    for item in entries:
        if (
            not item.leaf
            or not _CATEGORY_ID.fullmatch(item.category_id)
            or not item.name
            or len(item.name) > 200
            or not item.whole_category_name
            or len(item.whole_category_name) > 1000
            or item.category_id in seen
        ):
            raise InputValidationError(
                "CATEGORY_CATALOG_INVALID", "the provider category catalog is not a valid leaf set"
            )
        seen.add(item.category_id)
    return tuple(sorted(entries, key=lambda item: item.category_id))


class CategoryCatalogStore:
    def __init__(self, db: Database, clock: Clock, audit: AuditLog) -> None:
        self._db = db
        self._clock = clock
        self._audit = audit

    def current(self, marketplace_key: str = MARKETPLACE) -> CatalogSnapshot | None:
        with self._db.read() as session:
            row = session.scalars(
                select(MarketplaceCategoryCatalogSnapshot)
                .where(MarketplaceCategoryCatalogSnapshot.marketplace_key == marketplace_key)
                .order_by(MarketplaceCategoryCatalogSnapshot.snapshot_sequence.desc())
                .limit(1)
            ).first()
            if row is None:
                return None
            entries = tuple(
                ProviderCategory(e.category_id, e.name, e.whole_category_name, bool(e.leaf))
                for e in session.scalars(
                    select(MarketplaceCategoryCatalogEntry)
                    .where(MarketplaceCategoryCatalogEntry.snapshot_id == row.snapshot_id)
                    .order_by(MarketplaceCategoryCatalogEntry.category_id)
                ).all()
            )
            return CatalogSnapshot(
                row.snapshot_id,
                row.marketplace_key,
                row.taxonomy_revision,
                row.content_fingerprint,
                row.endpoint_mapping_revision,
                row.recorded_at,
                entries,
            )

    def record(
        self,
        marketplace_key: str,
        entries: tuple[ProviderCategory, ...],
        *,
        endpoint_mapping_revision: str,
        actor: str,
        correlation_id: str,
    ) -> CatalogSnapshot:
        checked = _validate(entries)
        fingerprint = _fingerprint(checked)
        current = self.current(marketplace_key)
        if current is not None and current.content_fingerprint == fingerprint:
            return current
        now = self._clock.now()
        snapshot_id = str(uuid.uuid4())
        taxonomy_revision = f"{marketplace_key}-categories-{fingerprint[:24]}"
        with self._db.write() as session:
            snapshot_sequence = (
                session.scalar(
                    select(func.max(MarketplaceCategoryCatalogSnapshot.snapshot_sequence)).where(
                        MarketplaceCategoryCatalogSnapshot.marketplace_key == marketplace_key
                    )
                )
                or 0
            ) + 1
            row = MarketplaceCategoryCatalogSnapshot(
                snapshot_id=snapshot_id,
                marketplace_key=marketplace_key,
                taxonomy_revision=taxonomy_revision,
                content_fingerprint=fingerprint,
                snapshot_sequence=snapshot_sequence,
                entry_count=len(checked),
                endpoint_mapping_revision=endpoint_mapping_revision,
                recorded_by=actor,
                correlation_id=correlation_id,
                recorded_at=now,
            )
            session.add(row)
            # There is intentionally no mutable ORM relationship between immutable snapshots
            # and entries. Flush the parent explicitly so SQLite observes the FK dependency.
            session.flush()
            session.add_all(
                MarketplaceCategoryCatalogEntry(
                    entry_id=str(uuid.uuid4()),
                    snapshot_id=snapshot_id,
                    category_id=item.category_id,
                    name=item.name,
                    whole_category_name=item.whole_category_name,
                    leaf=1,
                )
                for item in checked
            )
            self._audit.append(
                AuditEntry(
                    event_type=AuditEventType.MARKETPLACE_CATEGORY_CATALOG_RECORDED,
                    action="CATEGORY_CATALOG_SNAPSHOT_RECORDED",
                    actor=actor,
                    outcome=AuditOutcome.RECORDED,
                    target_ref=snapshot_id,
                    before=None if current is None else {"snapshot_id": current.snapshot_id},
                    after={
                        "snapshot_id": snapshot_id,
                        "taxonomy_revision": taxonomy_revision,
                        "content_fingerprint": fingerprint,
                        "entry_count": len(checked),
                    },
                    details={
                        "marketplace_key": marketplace_key,
                        "catalog_version": CATALOG_VERSION,
                    },
                    correlation_id=correlation_id,
                ),
                session=session,
            )
        recorded = self.current(marketplace_key)
        assert recorded is not None
        return recorded


class CategoryCatalogService:
    def __init__(
        self,
        store: CategoryCatalogStore,
        source: CategoryCatalogSource,
        *,
        endpoint_mapping_revision: str,
    ) -> None:
        self._store = store
        self._source = source
        self._endpoint_mapping_revision = endpoint_mapping_revision

    def sync(self, *, actor: str, correlation_id: str) -> CategoryCatalogView:
        return self._view(
            self._store.record(
                MARKETPLACE,
                self._source.leaf_categories(),
                endpoint_mapping_revision=self._endpoint_mapping_revision,
                actor=actor,
                correlation_id=correlation_id,
            )
        )

    def catalog(
        self, marketplace_key: str, *, query: str = "", limit: int = 200
    ) -> CategoryCatalogView:
        if marketplace_key != MARKETPLACE:
            raise NotFoundError(
                "CATEGORY_CATALOG_MARKETPLACE_UNKNOWN", "no category catalog for marketplace"
            )
        current = self._store.current(marketplace_key)
        if current is None:
            return CategoryCatalogView(marketplace_key=marketplace_key)
        needle = query.strip().casefold()
        entries = tuple(
            item
            for item in current.entries
            if not needle
            or needle in item.category_id.casefold()
            or needle in item.name.casefold()
            or needle in item.whole_category_name.casefold()
        )[: max(1, min(limit, 500))]
        return self._view(current, entries=entries)

    def require_current_leaf(
        self, marketplace_key: str, taxonomy_revision: str, category_id: str
    ) -> None:
        current = self._store.current(marketplace_key)
        if current is None:
            raise InputValidationError(
                "CATEGORY_CATALOG_MISSING",
                "sync the current marketplace leaf-category catalog first",
            )
        if taxonomy_revision != current.taxonomy_revision:
            raise InputValidationError(
                "CATEGORY_TAXONOMY_NOT_CURRENT",
                "the category taxonomy revision is not the current provider catalog",
                details={"current_taxonomy_revision": current.taxonomy_revision},
            )
        if not any(item.category_id == category_id and item.leaf for item in current.entries):
            raise InputValidationError(
                "CATEGORY_NOT_CURRENT_LEAF",
                "the category id is not a current provider leaf category",
            )

    def has_current(self, marketplace_key: str) -> bool:
        return self._store.current(marketplace_key) is not None

    @staticmethod
    def _view(
        snapshot: CatalogSnapshot, *, entries: tuple[ProviderCategory, ...] | None = None
    ) -> CategoryCatalogView:
        selected = snapshot.entries if entries is None else entries
        return CategoryCatalogView(
            marketplace_key=snapshot.marketplace_key,
            snapshot_id=snapshot.snapshot_id,
            taxonomy_revision=snapshot.taxonomy_revision,
            content_fingerprint=snapshot.content_fingerprint,
            endpoint_mapping_revision=snapshot.endpoint_mapping_revision,
            recorded_at=snapshot.recorded_at,
            total=len(snapshot.entries),
            entries=tuple(
                CategoryCatalogEntryView(
                    category_id=item.category_id,
                    name=item.name,
                    whole_category_name=item.whole_category_name,
                )
                for item in selected
            ),
        )


__all__ = [
    "CategoryCatalogEntryView",
    "CategoryCatalogService",
    "CategoryCatalogStore",
    "CategoryCatalogView",
    "MarketplaceCategoryCatalogEntry",
    "MarketplaceCategoryCatalogSnapshot",
    "ProviderCategory",
]
