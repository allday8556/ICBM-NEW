"""ADR-0027 §3–§5: what the probe derives from the served sidecar, and how the adapter reads an
answer. The configuration's secrets never reach the routing fingerprint."""

import json
import os
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

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


def test_an_explicit_config_path_is_the_one_fingerprinted(tmp_path: Path) -> None:
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (elsewhere / "custom.yaml").write_text(CONFIG.replace("8317", "9000"), encoding="utf-8")
    process = _process(tmp_path, args=f' -local-model -config "{elsewhere / "custom.yaml"}"')
    assert sidecar.config_path(process) == elsewhere / "custom.yaml"
    here = sidecar.routing(_process(tmp_path))
    there = sidecar.routing(process)
    assert here is not None and there is not None and here.fingerprint != there.fingerprint
    assert not hasattr(sidecar, "client_key")


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


def test_the_call_never_goes_through_an_environment_proxy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ADR-0027 §3: the request reaches the verified loopback port directly, whatever proxy the
    environment names; a proxy here would receive the key and the facts."""

    class Answer(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            self.rfile.read(int(self.headers["Content-Length"]))
            body = json.dumps(
                {"model": "m-1", "choices": [{"message": {"content": '{"ok": true}'}}]}
            ).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args: object) -> None:
            return

    server = HTTPServer(("127.0.0.1", 0), Answer)
    threading.Thread(target=server.handle_request, daemon=True).start()
    for name in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "all_proxy"):
        monkeypatch.setenv(name, "http://127.0.0.1:9")
    monkeypatch.delenv("NO_PROXY", raising=False)
    monkeypatch.delenv("no_proxy", raising=False)
    try:
        answer = cliproxyapi.complete(
            f"http://127.0.0.1:{server.server_port}", "k", "m", "x", timeout_s=10
        )
    finally:
        server.server_close()
    assert (answer.value, answer.error_code, answer.actual_model) == ({"ok": True}, None, "m-1")


def test_the_executable_hash_is_read_again_every_time(tmp_path: Path) -> None:
    """A same-size replacement that restores the modification time is a new identity."""
    exe = tmp_path / "cli-proxy-api.exe"
    exe.write_bytes(b"approved")
    before = exe.stat()
    first = sidecar.file_sha256(str(exe))
    exe.write_bytes(b"replaced")
    os.utime(exe, ns=(before.st_atime_ns, before.st_mtime_ns))
    assert exe.stat().st_size == before.st_size
    assert exe.stat().st_mtime_ns == before.st_mtime_ns
    assert sidecar.file_sha256(str(exe)) != first


SPREAD = """host: "127.0.0.1"
port: 8317
api-keys:
  - "first-SECRET-1"

  # a comment between the items
  - "second-SECRET-2"
claude-api-key:
  - api-key: "sk-SECRET-3"
    base-url: "https://example.invalid"

remote-management:
  secret-key: "SECRET-4"  # trailing
  disable-auto-update-panel: true
debug: false # note SECRET-5
"""


def test_every_secret_is_removed_however_it_is_spread_and_from_the_arguments(
    tmp_path: Path,
) -> None:
    """GPT audit of #273: blank and comment lines inside a secret list, a nested mapping under a
    secret key, comments, and secret-named launch arguments never reach the fingerprint."""
    kept = sidecar._without_secrets(SPREAD)
    assert "SECRET" not in kept
    assert "port: 8317" in kept and "debug: false" in kept
    assert "disable-auto-update-panel: true" in kept
    # Only values are hidden: the destination beside a secret stays in the fingerprint.
    assert 'base-url: "https://example.invalid"' in kept
    assert "api-key: <redacted>" in kept and kept.count("- <redacted>") == 2
    args = " -local-model -password=SECRET-6 --token SECRET-7 -config-name x"
    redacted = sidecar._redacted_args(sidecar._arguments(f'"exe"{args}'))
    assert not any("SECRET" in arg for arg in redacted)
    assert "-local-model" in redacted and "x" in redacted
    first = sidecar.routing(_process(tmp_path, SPREAD, args))
    other = sidecar.routing(
        _process(tmp_path, SPREAD.replace("SECRET", "OTHER"), args.replace("SECRET", "OTHER"))
    )
    assert first is not None and other is not None
    assert first.fingerprint == other.fingerprint
    assert first.panel_auto_update_disabled is True
    moved = sidecar.routing(
        _process(tmp_path, SPREAD.replace("example.invalid", "elsewhere.invalid"), args)
    )
    assert moved is not None and moved.fingerprint != first.fingerprint


def test_an_http_error_from_the_sidecar_is_never_retried_automatically(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ADR-0027 §4: a 5xx may have been forwarded; it is UNKNOWN, never TRANSIENT."""

    class Failing(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            self.rfile.read(int(self.headers["Content-Length"]))
            self.send_response(502)
            self.send_header("Content-Length", "0")
            self.end_headers()

        def log_message(self, *args: object) -> None:
            return

    server = HTTPServer(("127.0.0.1", 0), Failing)
    threading.Thread(target=server.handle_request, daemon=True).start()
    try:
        answer = cliproxyapi.complete(f"http://127.0.0.1:{server.server_port}", "k", "m", "x")
    finally:
        server.server_close()
    assert (answer.error_kind, answer.error_code) == ("UNKNOWN", "AI_SIDECAR_HTTP_ERROR")


def test_the_update_switches_are_read_as_they_take_effect() -> None:
    """GPT audit of #273: a shadowed, duplicated or misplaced panel setting is never "off", and a
    later ``-local-model=false`` turns the flag off again."""
    good = "port: 1\nremote-management:\n  allow-remote: false\n  disable-auto-update-panel: true\n"
    assert sidecar._panel_off(good) is True
    assert sidecar._panel_off(good + "  disable-auto-update-panel: false\n") is False
    assert sidecar._panel_off(good + "remote-management:\n  allow-remote: true\n") is False
    assert sidecar._panel_off("other:\n  disable-auto-update-panel: true\n") is False
    assert sidecar._panel_off("disable-auto-update-panel: true\n") is False
    assert sidecar._panel_off(good.replace("true\n", "false\n")) is False
    assert sidecar._local_model(["-local-model"]) is True
    assert sidecar._local_model(["-local-model", "-local-model=false"]) is False
    assert sidecar._local_model(["--local-model=true"]) is True
    assert sidecar._local_model([]) is False
