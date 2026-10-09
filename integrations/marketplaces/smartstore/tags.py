"""Read SmartStore's recommended tags and check restricted tags through the adopted caller.

ADR-0028 §2 (T2). Two reads, never mutation authority, and never a tag sent to the marketplace
(AIT-01):
- ``recommend(keyword)``: the platform's recommended tags for one keyword, at most 20, each with its
  tag id (``code``) when the platform gives one.
- ``check(tags)``: the restricted-tag answer for 1–10 distinct tags. A tag the platform did not
  answer exactly once is **not checked** (``None``), never "not restricted" (AIT-03). A check is a
  snapshot: the caller records when it was made, and it is re-made before any future send (AIT-04).

Nothing is stored here: the tag task reads when it runs.
"""

from dataclasses import dataclass
from typing import Any

from app.platform.core.errors import PolicyBlockedError
from integrations.marketplaces.smartstore.caller import (
    TAG_CHECK_MAX,
    SmartStoreEndpointCaller,
    TagRecommendRequest,
    TagRestrictedRequest,
)
from integrations.marketplaces.smartstore.registry import EndpointId

SMARTSTORE_SESSION_UNAVAILABLE = "SMARTSTORE_SESSION_UNAVAILABLE"
SMARTSTORE_TAG_RESPONSE_INVALID = "SMARTSTORE_TAG_RESPONSE_INVALID"


@dataclass(frozen=True)
class RecommendedTag:
    text: str
    # The platform's tag id. A tag sent with a code must carry exactly this text (2.90.1).
    code: int | None


class SmartStoreTagSource:
    def __init__(self, caller: SmartStoreEndpointCaller, bearer: Any) -> None:
        self._caller = caller
        self._bearer = bearer

    def _session(self) -> Any:
        bearer = self._bearer()
        if bearer is None:
            raise PolicyBlockedError(
                SMARTSTORE_SESSION_UNAVAILABLE,
                "a current committed SmartStore session is required to read tags",
            )
        return bearer

    def recommend(self, keyword: str) -> tuple[RecommendedTag, ...]:
        bearer = self._session()
        response = self._caller.call(
            EndpointId.SMARTSTORE_TAG_RECOMMEND,
            TagRecommendRequest(
                bearer.access_token,
                bearer.credential_generation,
                bearer.session_generation,
                keyword,
            ),
        )
        # Retention drops an empty array, so an absent ``items`` is no recommendation.
        items = response.retained.get("items", [])
        if not isinstance(items, list):
            raise _invalid("the recommended tags did not retain the documented array")
        tags: list[RecommendedTag] = []
        seen: set[str] = set()
        for item in items:
            text = item.get("text") if isinstance(item, dict) else None
            code = item.get("code") if isinstance(item, dict) else None
            if not isinstance(text, str) or not text.strip():
                raise _invalid("a recommended tag has no text")
            if code is not None and (not isinstance(code, int) or isinstance(code, bool)):
                raise _invalid("a recommended tag id is not an integer")
            if text not in seen:
                seen.add(text)
                tags.append(RecommendedTag(text, code))
        return tuple(tags)

    def check(self, tags: tuple[str, ...]) -> dict[str, bool | None]:
        """``True`` restricted, ``False`` not restricted, ``None`` not checked."""
        if not 1 <= len(tags) <= TAG_CHECK_MAX or len(set(tags)) != len(tags):
            raise ValueError("a restricted-tag check carries 1 to 10 distinct tags")
        bearer = self._session()
        response = self._caller.call(
            EndpointId.SMARTSTORE_TAG_RESTRICTED,
            TagRestrictedRequest(
                bearer.access_token,
                bearer.credential_generation,
                bearer.session_generation,
                tags,
            ),
        )
        items = response.retained.get("items", [])
        if not isinstance(items, list):
            raise _invalid("the restricted-tag answer did not retain the documented array")
        answers: dict[str, list[bool]] = {}
        for item in items:
            tag = item.get("tag") if isinstance(item, dict) else None
            restricted = item.get("restricted") if isinstance(item, dict) else None
            if not isinstance(tag, str) or not isinstance(restricted, bool):
                raise _invalid("a restricted-tag answer is missing a documented field")
            if tag not in tags:
                # An answer for a tag that was not asked proves nothing about any tag asked.
                raise _invalid("the restricted-tag answer names a tag that was not asked")
            answers.setdefault(tag, []).append(restricted)
        return {tag: answers[tag][0] if len(answers.get(tag, [])) == 1 else None for tag in tags}


def _invalid(message: str) -> PolicyBlockedError:
    return PolicyBlockedError(SMARTSTORE_TAG_RESPONSE_INVALID, message)


__all__ = ["RecommendedTag", "SmartStoreTagSource"]
