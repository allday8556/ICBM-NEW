"""ADR-0027 §3–§5: what the probe derives from the served sidecar, and how the adapter reads an
answer. The configuration's secrets never reach the routing fingerprint."""

from pathlib import Path

from integrations.ai import cliproxyapi, sidecar

CONFIG = """host: "127.0.0.1"
port: 8317

api-keys:
  - "client-key-123"

remote-management:
  allow-remote: false
  secret-key: "shh"
  disable-auto-update-panel: true
"""


def _process(
    tmp_path: Path, config: str = CONFIG, args: str = " -local-model"
) -> sidecar.ServingProcess:
    (tmp_path / "config.yaml").write_text(config, encoding="utf-8")
    exe = tmp_path / "cli-proxy-api.exe"
    exe.write_bytes(b"binary")
    return sidecar.ServingProcess(
        pid=1,
        path=str(exe),
        sha256=sidecar.file_sha256(str(exe)) or "",
        command_line=f'"{exe}"{args}',
    )


def test_the_routing_identity_holds_no_secret_and_sees_both_update_switches(tmp_path: Path) -> None:
    observed = sidecar.routing(_process(tmp_path))
    assert observed is not None
    assert (observed.local_model, observed.panel_auto_update_disabled) == (True, True)
    # A different key or management secret is the same routing identity: secrets are removed.
    other = sidecar.routing(
        _process(tmp_path, CONFIG.replace("client-key-123", "other").replace("shh", "x"))
    )
    assert other is not None and other.fingerprint == observed.fingerprint
    # A routing-relevant change is a new identity, and so are the launch arguments.
    moved = sidecar.routing(_process(tmp_path, CONFIG.replace("8317", "8318")))
    assert moved is not None and moved.fingerprint != observed.fingerprint
    plain = sidecar.routing(_process(tmp_path, args=""))
    assert plain is not None and (plain.local_model, plain.fingerprint != observed.fingerprint) == (
        False,
        True,
    )
    off = sidecar.routing(_process(tmp_path, CONFIG.replace("true", "false")))
    assert off is not None and off.panel_auto_update_disabled is False


def test_the_client_key_is_read_from_the_sidecar_config_and_explicit_config_paths_count(
    tmp_path: Path,
) -> None:
    assert sidecar.client_key(_process(tmp_path)) == "client-key-123"
    assert sidecar.client_key(_process(tmp_path, CONFIG.replace("api-keys:", "keys:"))) is None
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (elsewhere / "custom.yaml").write_text(CONFIG.replace("client-key-123", "k2"), encoding="utf-8")
    process = _process(tmp_path, args=f' -config "{elsewhere / "custom.yaml"}"')
    assert sidecar.config_path(process) == elsewhere / "custom.yaml"
    assert sidecar.client_key(process) == "k2"


def test_the_adapter_reads_the_first_json_object_of_an_answer() -> None:
    assert cliproxyapi.first_json_object('note {"a": {"b": 1}} trailing {"c": 2}') == {
        "a": {"b": 1}
    }
    assert cliproxyapi.first_json_object('```json\n{"ok": true}\n```') == {"ok": True}
    assert cliproxyapi.first_json_object("[1, 2]") is None
    assert cliproxyapi.first_json_object("no json") is None


def test_an_unreachable_sidecar_is_a_transient_failure() -> None:
    answer = cliproxyapi.complete("http://127.0.0.1:9", "k", "m", "x", timeout_s=2)
    assert (answer.error_kind, answer.error_code, answer.value) == (
        "TRANSIENT",
        "AI_SIDECAR_UNREACHABLE",
        None,
    )
