"""The protected-read proof procedure — common to every supplier (Issue #7 comment 5653567880,
comment 5653608622 §5).

A supplier contributes only its probe (target and predicates). The procedure is:

    unauthenticated control against the target
    → must hold the unauthenticated expectation, and must not look authenticated
    authenticated request against the same target
    → must hold the authenticated predicate, and must not look unauthenticated

HTTP status is never proof on its own: a supplier may answer a login page with 200. A predicate
that raises, or a response that satisfies both or neither predicate, is UNRECOGNIZED — never
success.
"""

from dataclasses import dataclass
from enum import StrEnum

from integrations.suppliers.base import ProbeResponse, ProtectedReadProbe


class ReadClassification(StrEnum):
    AUTHENTICATED = "AUTHENTICATED"
    LOGIN_REQUIRED = "LOGIN_REQUIRED"
    UNRECOGNIZED = "UNRECOGNIZED"


@dataclass(frozen=True)
class ReadOutcome:
    classification: ReadClassification
    http_status: int
    signals: tuple[str, ...]


def classify(probe: ProtectedReadProbe, response: ProbeResponse) -> ReadOutcome:
    try:
        authenticated, auth_signals = probe.authenticated_predicate(response)
        unauthenticated, unauth_signals = probe.unauthenticated_expectation(response)
    except Exception:  # a broken predicate proves nothing
        return ReadOutcome(ReadClassification.UNRECOGNIZED, response.status, ("predicate_error",))
    signals = tuple(sorted(set(auth_signals) | set(unauth_signals)))
    if authenticated and not unauthenticated:
        verdict = ReadClassification.AUTHENTICATED
    elif unauthenticated and not authenticated:
        verdict = ReadClassification.LOGIN_REQUIRED
    else:
        verdict = ReadClassification.UNRECOGNIZED
    return ReadOutcome(verdict, response.status, signals)


@dataclass(frozen=True)
class ProtectedReadProof:
    """Both halves of the proof for one target. Only ``proven`` may make a supplier READY."""

    target: str
    control: ReadOutcome
    authenticated: ReadOutcome

    @property
    def proven(self) -> bool:
        return (
            self.control.classification is ReadClassification.LOGIN_REQUIRED
            and self.authenticated.classification is ReadClassification.AUTHENTICATED
        )
