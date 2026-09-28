"""The adoption facts the CREATE owner hands the send-time stack (ADR-0018 §10, ADR-0014 §28).

``admit_create`` is given exactly what :meth:`RegistrationExecutionService.create_sender_available`
and :meth:`RegistrationExecutionService.reconcile_path_adopted` report for the same seams, as
``bool`` values — the facts a readiness reports and the facts a send is admitted on are one owner's.
"""

from typing import Any

import pytest

from app.container import Container
from app.register.store import RegistrationStore
from tests.integration.test_m5_register_execution import (  # noqa: F401 (fixtures)
    FakeLookup,
    FakeSender,
    account,
    context,
    execution,
    prep,
    prepare,
    sources,
    store,
)
from tests.live_support import AdmittingAuthority
from tests.product_support import Collections
from tests.register_support import Preparation


class RecordingAuthority(AdmittingAuthority):
    """Admits like :class:`AdmittingAuthority` and keeps the adoption facts it was given."""

    def __init__(self) -> None:
        super().__init__()
        self.facts: list[tuple[Any, Any]] = []

    def admit_create(self, session: Any, **kwargs: Any) -> None:
        self.facts.append((kwargs["endpoint_adopted"], kwargs["reconcile_path_adopted"]))
        super().admit_create(session, **kwargs)


@pytest.mark.parametrize("lookup_adopted", [False, True])
def test_the_stack_is_given_the_owners_own_adoption_facts(
    container: Container,
    sources: Collections,  # noqa: F811
    store: RegistrationStore,  # noqa: F811
    account: str,  # noqa: F811
    prep: Preparation,  # noqa: F811
    lookup_adopted: bool,
) -> None:
    ready = prepare(container, sources, store, account, prep)
    authority = RecordingAuthority()
    run = execution(
        container,
        prep,
        authority=authority,
        sender=FakeSender(is_available=True),
        lookup=FakeLookup(is_available=lookup_adopted),
    )
    run.service.run(context(ready))
    ((endpoint_adopted, reconcile_path_adopted),) = authority.facts
    assert type(endpoint_adopted) is bool and type(reconcile_path_adopted) is bool
    assert endpoint_adopted is run.service.create_sender_available() is True
    assert reconcile_path_adopted is run.service.reconcile_path_adopted() is lookup_adopted
