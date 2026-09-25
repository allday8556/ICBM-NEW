"""DRAFT lint (ADR-0017 §7.1): the findings recorded when an EPR is saved as ``DRAFT``.

A DRAFT is a strict parse, a computed digest and recorded lint findings. Lint never refuses a
DRAFT and is never a verdict: an unmapped CORE field is allowed in a DRAFT, and V2 decides
coverage when the bundle is validated. It is deterministic, reads only the bundle, and costs no
network.

``LINT_REVISION`` names the rule set, so a recorded result stays interpretable after the rules
change: findings are only ever compared with the rule set that produced them. Each finding is a
``CODE:template_key:detail`` string (``CODE`` alone for an EPR-level finding), sorted and without
duplicates. Findings name fields, regions and template keys only, never page content.
"""

from app.collect.adaptive.canonical import digest
from app.collect.adaptive.profiles import RULED_FIELDS, Bundle
from app.collect.facts import FIELD_REGISTRY, SUPPLIED_FIELDS, FieldLevel

LINT_REVISION = "adaptive-lint-1"
LINT_DIGEST_SCHEME = "icbm-profile-lint/v1"

UNMAPPED_CORE_FIELD = "UNMAPPED_CORE_FIELD"
COVERAGE_FIELD_WITHOUT_RULE = "COVERAGE_FIELD_WITHOUT_RULE"
NO_IMAGE_REGION = "NO_IMAGE_REGION"
REPRESENTATIVE_REGION_MISSING = "REPRESENTATIVE_REGION_MISSING"
NO_REPRESENTATIVE_IMAGE_ROLE = "NO_REPRESENTATIVE_IMAGE_ROLE"

_CORE = frozenset(key for key in RULED_FIELDS if FIELD_REGISTRY[key].level is FieldLevel.CORE)
_COVERAGE = frozenset(
    key for key in SUPPLIED_FIELDS if FIELD_REGISTRY[key].level is FieldLevel.COVERAGE
)


def draft_lint(bundle: Bundle) -> tuple[str, ...]:
    """The lint findings of one resolved bundle under ``LINT_REVISION``."""
    findings: set[str] = set()
    representative = {
        rule.region for rule in bundle.epr.image_roles if rule.role == "REPRESENTATIVE"
    }
    if not representative:
        findings.add(NO_REPRESENTATIVE_IMAGE_ROLE)
    for _, template in bundle.templates:
        key = template.template_key
        ruled = set(template.fields)
        findings.update(f"{UNMAPPED_CORE_FIELD}:{key}:{field}" for field in _CORE - ruled)
        findings.update(
            f"{COVERAGE_FIELD_WITHOUT_RULE}:{key}:{field}" for field in _COVERAGE - ruled
        )
        regions = {region.name for region in template.image_regions}
        if not regions:
            findings.add(f"{NO_IMAGE_REGION}:{key}")
        findings.update(
            f"{REPRESENTATIVE_REGION_MISSING}:{key}:{region}" for region in representative - regions
        )
    return tuple(sorted(findings))


def lint_digest(lint_revision: str, findings: tuple[str, ...]) -> str:
    """The content digest of one recorded lint result, bound to the rule set that produced it."""
    return digest(LINT_DIGEST_SCHEME, {"lint_revision": lint_revision, "findings": list(findings)})
