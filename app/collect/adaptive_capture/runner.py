"""The capture step (implements ``app.collect.shadow.CaptureStep``).

It runs only for a run frozen ``REQUESTED``, after the canonical revision has committed and after
the shadow step. It cuts a candidate from the one in-memory document with the accepted capture
sanitizer and final scan, and keeps only that sanitized material. A refused capture is recorded
by its kind; a failure of the capture owner's own write is a log line. Nothing reaches the run.
"""

import logging

from app.collect.adaptive.capture import CaptureRefused, capture_candidate
from app.collect.adaptive_capture.store import CaptureStore
from app.collect.shadow import CaptureInput

logger = logging.getLogger("icbm.collect.capture")


class CaptureRunner:
    def __init__(self, store: CaptureStore) -> None:
        self._store = store

    def __call__(self, capture: CaptureInput) -> None:
        frozen = capture.capture
        if frozen.decision != "REQUESTED" or frozen.request_id is None:
            return
        keys = {
            "collection_run_id": capture.collection_run_id,
            "request_id": frozen.request_id,
            "revision_id": capture.revision_id,
        }
        try:
            try:
                candidate = capture_candidate(capture.document.body)
            except CaptureRefused as refused:
                self._store.refuse(**keys, refusal=str(refused))
                return
            except Exception as failure:
                self._store.refuse(**keys, refusal=f"CAPTURE_FAILED:{type(failure).__name__}")
                return
            self._store.record(**keys, candidate=candidate)
        except Exception:
            logger.exception(
                "collect.capture_write_failed",
                extra={"collection_run_id": capture.collection_run_id},
            )
