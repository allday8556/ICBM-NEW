"""The shape-only runtime capture of a store-catalog sync (ADR-0042 §5.1 item 3).

On an operator's 「지금 동기화」 the sync records the **shape** of the raw provider responses it
received — the search pages and the origin reads, each as its own union — as runtime evidence for
the provider facts the official reference could not supply (E2–E6). A shape is every key path with
its JSON types: ``string``, ``number``, ``boolean``, ``null``, ``object`` or ``array``. An array's
items are merged under ``path[]``.

**No value is recorded.** The single exception is an enum token: a string held at a key whose
name ends in ``Type``, ``Status``, ``Unit`` or ``Method`` and that matches
``^[A-Z][A-Z0-9_]{0,40}$``. Product numbers, names, prices, texts, URLs and contacts never are. A
key that is not a plain identifier (a map keyed by a number, say) is recorded as ``*``, so no value
can ride on a path either.

The shape is bounded (:data:`MAX_PATHS`, :data:`MAX_TOKENS_PER_PATH`, :data:`MAX_DEPTH`); what does
not fit is counted as truncated, never kept. It widens no retention profile: nothing here is ever
read back as a listing field. Pure: no I/O.
"""

import re
from typing import Any, Final

SHAPE_VERSION: Final = "store-catalog-shape/v1"
MAX_PATHS: Final = 2000
MAX_TOKENS_PER_PATH: Final = 200
MAX_DEPTH: Final = 32

ROOT: Final = "$"
WILDCARD_KEY: Final = "*"
_ENUM_SUFFIXES: Final = ("Type", "Status", "Unit", "Method")
_ENUM_TOKEN: Final = re.compile(r"^[A-Z][A-Z0-9_]{0,40}$")
_PLAIN_KEY: Final = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,63}$")


def json_type(value: object) -> str:
    """The JSON type of one parsed value. A boolean is never a number."""
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int | float):
        return "number"
    if isinstance(value, str):
        return "string"
    if isinstance(value, dict):
        return "object"
    if isinstance(value, list | tuple):
        return "array"
    return "unknown"


def enum_token(key: str | None, value: object) -> str | None:
    """The value kept as an enum token, or ``None``: only a string at a key named ``…Type``,
    ``…Status``, ``…Unit`` or ``…Method`` that is an upper-case code."""
    if key is None or not isinstance(value, str) or not key.endswith(_ENUM_SUFFIXES):
        return None
    return value if _ENUM_TOKEN.fullmatch(value) else None


def _key(key: object) -> str:
    text = str(key)
    return text if _PLAIN_KEY.fullmatch(text) else WILDCARD_KEY


class ShapeRecorder:
    """The union of the shapes of every response handed to :meth:`add`, within the bounds."""

    def __init__(self) -> None:
        self._paths: dict[str, set[str]] = {}
        self._tokens: dict[str, set[str]] = {}
        self._tokens_truncated: set[str] = set()
        self._responses = 0
        self._truncated = False

    @property
    def responses(self) -> int:
        return self._responses

    def add(self, body: object) -> None:
        """Merge the shape of one parsed response body."""
        self._responses += 1
        self._walk(body, ROOT, None, 0)

    def _walk(self, value: object, path: str, key: str | None, depth: int) -> None:
        if not self._note(path, json_type(value)):
            return
        token = enum_token(key, value)
        if token is not None:
            tokens = self._tokens.setdefault(path, set())
            if token in tokens or len(tokens) < MAX_TOKENS_PER_PATH:
                tokens.add(token)
            else:
                self._tokens_truncated.add(path)
        if depth >= MAX_DEPTH:
            if isinstance(value, dict | list | tuple) and value:
                self._truncated = True
            return
        if isinstance(value, dict):
            for child_key, child in value.items():
                name = _key(child_key)
                self._walk(child, f"{path}.{name}", name, depth + 1)
        elif isinstance(value, list | tuple):
            for item in value:
                self._walk(item, f"{path}[]", None, depth + 1)

    def _note(self, path: str, kind: str) -> bool:
        types = self._paths.get(path)
        if types is None:
            if len(self._paths) >= MAX_PATHS:
                self._truncated = True
                return False
            types = self._paths[path] = set()
        types.add(kind)
        return True

    def shape(self) -> dict[str, Any]:
        """The canonical shape: paths in order, each with its sorted types and enum tokens."""
        return {
            "version": SHAPE_VERSION,
            "responses": self._responses,
            "truncated": self._truncated,
            "paths": {
                path: {
                    "types": sorted(types),
                    **(
                        {"enum_tokens": sorted(self._tokens[path])}
                        if self._tokens.get(path)
                        else {}
                    ),
                    **({"enum_tokens_truncated": True} if path in self._tokens_truncated else {}),
                }
                for path, types in sorted(self._paths.items())
            },
        }


__all__ = [
    "MAX_DEPTH",
    "MAX_PATHS",
    "MAX_TOKENS_PER_PATH",
    "SHAPE_VERSION",
    "ShapeRecorder",
    "enum_token",
    "json_type",
]
