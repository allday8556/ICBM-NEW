import json
import logging

from app.core.correlation import (
    accept_or_issue,
    correlation_scope,
    get_correlation_id,
    is_valid_correlation_id,
)
from app.core.logging import CorrelationFilter, JsonFormatter


def test_well_formed_ids_are_accepted_and_bad_ones_reissued() -> None:
    assert accept_or_issue("m0-acceptance-0001") == "m0-acceptance-0001"
    reissued = accept_or_issue("bad id with spaces")
    assert reissued != "bad id with spaces"
    assert is_valid_correlation_id(reissued)
    assert is_valid_correlation_id(accept_or_issue(None))


def test_scope_sets_and_restores() -> None:
    assert get_correlation_id() is None
    with correlation_scope("outer-12345678"):
        with correlation_scope("inner-12345678"):
            assert get_correlation_id() == "inner-12345678"
        assert get_correlation_id() == "outer-12345678"
    assert get_correlation_id() is None


def test_json_log_record_carries_correlation_and_extras() -> None:
    record = logging.LogRecord("icbm.test", logging.INFO, __file__, 1, "job.enqueued", (), None)
    record.job_id = "abc"
    with correlation_scope("trace-12345678"):
        CorrelationFilter().filter(record)
    payload = json.loads(JsonFormatter().format(record))
    assert payload["msg"] == "job.enqueued"
    assert payload["correlation_id"] == "trace-12345678"
    assert payload["job_id"] == "abc"
    assert payload["level"] == "INFO"
