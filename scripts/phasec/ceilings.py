"""The Phase C per-stage ceilings, frozen at campaign creation (review `5312911203` Q5).

A ceiling is never a target and is never widened inside a campaign: the campaign ledger records
these values when it is created, and no command changes them. C0, C2 and C4 send nothing.
"""

from types import MappingProxyType
from typing import Final

STAGES: Final = ("C0", "C1", "C2", "C3", "C4")

_IMAGES = {
    "image_requests_per_attempt": 13,
    "image_bytes_max": 2 * 1024 * 1024,
    "new_image_bytes_per_attempt": 24 * 1024 * 1024,
}
_ZERO = {
    "collection_submissions": 0,
    "product_reads": 0,
    "connect_control_reads": 0,
    "connect_protected_reads": 0,
    "connect_authenticate": 0,
    "policy_reads": 0,
}

CEILINGS: Final = MappingProxyType(
    {
        "C0": dict(_ZERO),
        "C1": {
            "collection_submissions": 2,
            "target_identities": 2,
            "product_reads": 4,
            "connect_control_reads": 4,
            "connect_protected_reads": 4,
            "connect_authenticate": 0,
            "policy_reads": 0,
            "approved_samples_per_target_run": 1,
            **_IMAGES,
        },
        "C2": dict(_ZERO),
        "C3": {
            "collection_submissions": 3,
            "product_reads": 6,
            "connect_control_reads": 6,
            "connect_protected_reads": 6,
            "connect_authenticate": 0,
            "policy_reads": 0,
            "window_min_size": 3,
            **_IMAGES,
        },
        "C4": dict(_ZERO),
    }
)
