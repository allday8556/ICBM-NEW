"""Artifact secret scan (Issue #7 §15): reports counts, never values.

For each known secret, every representation that could hide it from a naive search is generated:
raw text (UTF-8, UTF-16-LE, CP949), URL encoding, JSON string escaping, Base64 (standard and
URL-safe, at every byte alignment) and HTML entities. Files are searched as bytes. The report
holds only labels, variant names and counts, so the scan cannot become a disclosure channel.
"""

import base64
import contextlib
import html
import json
from collections.abc import Iterable, Mapping
from pathlib import Path
from urllib.parse import quote, quote_plus

VARIANTS = ("raw", "url", "json", "base64", "html")
_MIN_BASE64_CORE = 8


def _raw(secret: str) -> set[bytes]:
    forms = {secret.encode("utf-8"), secret.encode("utf-16-le")}
    with contextlib.suppress(UnicodeEncodeError):
        forms.add(secret.encode("cp949"))
    return forms


def _base64(data: bytes) -> set[bytes]:
    """Base64 cores of ``data`` wherever it sits in a larger encoded stream."""
    cores: set[bytes] = set()
    for offset in range(3):
        total = offset + len(data)
        encoded = base64.b64encode(b"\0" * offset + data)
        start = -(-offset * 4 // 3)  # characters touched by the unknown preceding bytes
        end = (total - total % 3) * 4 // 3  # characters touched by unknown following bytes
        core = encoded[start:end]
        if len(core) >= _MIN_BASE64_CORE:
            cores.add(core)
            cores.add(core.replace(b"+", b"-").replace(b"/", b"_"))
    return cores


def variants(secret: str) -> dict[str, set[bytes]]:
    data = secret.encode("utf-8")
    entities = "".join(f"&#{ord(c)};" for c in secret)
    return {
        "raw": _raw(secret),
        "url": {quote(secret, safe="").encode(), quote_plus(secret).encode()},
        "json": {
            json.dumps(secret)[1:-1].encode(),
            json.dumps(secret, ensure_ascii=False)[1:-1].encode("utf-8"),
        },
        "base64": _base64(data),
        "html": {
            html.escape(secret, quote=True).encode("utf-8"),
            entities.encode(),
            "".join(f"&#x{ord(c):x};" for c in secret).encode(),
        },
    }


def _files(paths: Iterable[Path]) -> list[Path]:
    found: list[Path] = []
    for path in paths:
        if path.is_dir():
            found.extend(p for p in sorted(path.rglob("*")) if p.is_file())
        elif path.is_file():
            found.append(path)
    return found


def scan(paths: Iterable[Path], secrets: Mapping[str, str]) -> dict[str, object]:
    """Count occurrences of each labelled secret, per variant, across ``paths``.

    Returns ``{"files_scanned": n, "hits": {label: {variant: count}}, "total_hits": n}``.
    """
    files = _files(paths)
    contents = [f.read_bytes() for f in files]
    hits: dict[str, dict[str, int]] = {}
    for label, secret in secrets.items():
        if not secret:
            raise ValueError(f"secret {label!r} is empty")
        forms = variants(secret)
        hits[label] = {
            variant: sum(blob.count(form) for blob in contents for form in forms[variant])
            for variant in VARIANTS
        }
    total = sum(count for per_variant in hits.values() for count in per_variant.values())
    return {"files_scanned": len(files), "hits": hits, "total_hits": total}
