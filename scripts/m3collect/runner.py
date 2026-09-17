"""An offline rehearsal of the production one-product COLLECT path (M3 Stage-B2).

The operator can watch the whole path run without a provider: a synthetic shop answers, and every
step after that is the real thing — the durable ``collect.product`` job, the orchestrator, the
asset recorder, the revision store and the read-back. This module owns none of that work. It
parses nothing, hashes nothing, stores no asset and writes no revision; it submits a URL, turns
the worker, and reports what the application recorded.

It never reaches a network: the transport it installs is the synthetic shop, and the live
collection transport cannot even be constructed under CI or pytest.
"""

import json
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory

from app.collect.collection import RegisteredCollection
from app.collect.models import CollectionOutcome
from app.collect.runs import SameProductTooSoon
from app.config import AppConfig
from app.container import Container, build_container
from app.core.ownership import acquire_data_dir
from app.core.secrets import MemorySecretStore
from app.db.migrate import upgrade_to_head
from integrations.suppliers.collection import ImageResponse
from scripts.m3collect import fake_shop


@dataclass(frozen=True)
class Step:
    """One rehearsed collection, as the application answered it."""

    name: str
    outcome: CollectionOutcome
    revision_id: str | None
    facts_status: str | None
    detail: str | None
    image_requests: int
    assets: tuple[str, ...]

    def report(self) -> dict[str, object]:
        return {
            "step": self.name,
            "outcome": self.outcome.value,
            "revision_id": self.revision_id,
            "facts_status": self.facts_status,
            "detail": self.detail,
            "image_requests": self.image_requests,
            "assets": list(self.assets),
        }


class RehearsalClock:
    """A clock the rehearsal can move, so the same-product interval is honoured, not skipped.

    A real operator waits out the interval between two collections of one product. A rehearsal
    that pretended the interval did not exist would prove nothing about it.
    """

    def __init__(self) -> None:
        self._now = datetime(2026, 9, 17, 0, 0, tzinfo=UTC)

    def now(self) -> datetime:
        return self._now

    def wait(self, seconds: float) -> None:
        self._now += timedelta(seconds=seconds)


@contextmanager
def application(
    gateway: fake_shop.FakeGateway, directory: Path, clock: RehearsalClock
) -> Iterator[Container]:
    """The real application, owning a throwaway data directory, with the shop as its transport."""
    database = directory / "runtime" / "icbm.db"
    database.parent.mkdir(parents=True, exist_ok=True)
    upgrade_to_head(f"sqlite:///{database.as_posix()}")
    config = AppConfig(data_dir=directory)
    with acquire_data_dir(config.data_dir, app_version="m3collect-rehearsal") as lease:
        container = build_container(
            config,
            ownership=lease,
            clock=clock,
            secret_store=MemorySecretStore(),
            collection_gateway=gateway,
            collection_sessions=fake_shop.StubSessions(),
            collections=(
                RegisteredCollection(
                    collection=fake_shop.collection(),
                    extractor_revision=fake_shop.EXTRACTOR_REVISION,
                    extractor_fingerprint=fake_shop.EXTRACTOR_FINGERPRINT,
                ),
            ),
        )
        try:
            yield container
        finally:
            container.db.dispose()


def collect_once(container: Container, gateway: fake_shop.FakeGateway, name: str) -> Step:
    before = len(gateway.image_reads)
    submitted = container.collection.submit(fake_shop.SUPPLIER_KEY, fake_shop.PRODUCT_URL)
    container.runner.run_next()
    run = container.collection.run(submitted.collection_run_id)
    assets: tuple[str, ...] = ()
    if run.revision_id is not None:
        stored = container.source_truth.revision(run.revision_id)
        assets = tuple(
            f"{image.role.value}:{image.ordinal}:{image.asset.sha256[:12]}"
            if image.asset is not None
            else f"{image.role.value}:{image.ordinal}:{(image.issue.value if image.issue else '-')}"
            for image in stored.images
        )
    return Step(
        name=name,
        outcome=run.outcome,
        revision_id=run.revision_id,
        facts_status=None if run.facts_status is None else run.facts_status.value,
        detail=run.detail,
        image_requests=len(gateway.image_reads) - before,
        assets=assets,
    )


def too_soon(container: Container, gateway: fake_shop.FakeGateway, name: str) -> Step:
    """A submission the same-product interval refuses. No job, no run, no request."""
    before = len(gateway.image_reads)
    try:
        container.collection.submit(fake_shop.SUPPLIER_KEY, fake_shop.PRODUCT_URL)
    except SameProductTooSoon as refused:
        return Step(
            name=name,
            outcome=CollectionOutcome.PENDING,
            revision_id=None,
            facts_status=None,
            detail=refused.code,
            image_requests=len(gateway.image_reads) - before,
            assets=(),
        )
    raise AssertionError("the same-product interval did not refuse the submission")


def rehearse() -> list[Step]:
    """The collections one operator would meet, in order, including the ones that are refused."""
    gateway = fake_shop.FakeGateway(
        documents=[fake_shop.page()],
        images={
            fake_shop.PRIMARY_URL: fake_shop.PRIMARY_BYTES,
            fake_shop.DETAIL_URL: fake_shop.DETAIL_BYTES,
        },
    )
    steps = []
    clock = RehearsalClock()
    interval = fake_shop.collection().profile.limits.same_product_interval_s
    with (
        TemporaryDirectory(prefix="m3collect-") as temporary,
        application(gateway, Path(temporary), clock) as container,
    ):
        steps.append(collect_once(container, gateway, "first collection"))

        # The same product, too soon: refused before a job exists, and nothing was requested.
        steps.append(too_soon(container, gateway, "same product too soon"))
        clock.wait(interval + 1)

        # The provider says the representative image is unchanged: the stored bytes stay the
        # evidence, and the new revision points at the same asset.
        gateway.images[fake_shop.PRIMARY_URL] = ImageResponse(
            304, None, f'"etag-{len(fake_shop.PRIMARY_BYTES)}"', None
        )
        steps.append(collect_once(container, gateway, "revalidated reuse"))
        clock.wait(interval + 1)

        # The same URL now serves other bytes: a different asset, never the same identity.
        gateway.images[fake_shop.PRIMARY_URL] = fake_shop.OTHER_BYTES
        steps.append(collect_once(container, gateway, "changed content"))
        clock.wait(interval + 1)

        # A page that states no product number records nothing at all.
        gateway.documents = [fake_shop.page(product_id="")]
        steps.append(collect_once(container, gateway, "unresolved identity"))
    return steps


def main() -> int:
    steps = rehearse()
    print(json.dumps([step.report() for step in steps], ensure_ascii=False, indent=2))
    recorded = [s for s in steps if s.outcome is CollectionOutcome.RECORDED]
    unresolved = [s for s in steps if s.outcome is CollectionOutcome.NO_REVISION]
    refused = [s for s in steps if s.detail == "COLLECT_SAME_PRODUCT_TOO_SOON"]
    ok = (
        len(refused) == 1
        and refused[0].image_requests == 0
        and len(recorded) == 3
        and len(unresolved) == 1
        and unresolved[0].revision_id is None
        and recorded[0].assets[0].split(":")[2] == recorded[1].assets[0].split(":")[2]
        and recorded[1].assets[0].split(":")[2] != recorded[2].assets[0].split(":")[2]
        and recorded[1].image_requests == 2
    )
    print("REHEARSAL PASS" if ok else "REHEARSAL FAIL")
    return 0 if ok else 1
