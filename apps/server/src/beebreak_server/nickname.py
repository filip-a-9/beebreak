"""Nickname validation and the skeleton used for uniqueness. Owned by M3-07."""

import re
import unicodedata
from typing import Final, Literal

_PATTERN: Final = re.compile(r"[A-Za-z0-9_-]{3,24}")
_CONFUSABLES: Final = str.maketrans({"0": "o", "1": "l", "i": "l", "5": "s"})

_RESERVED_WORDS: Final = (
    "admin",
    "beebreak",
    "moderator",
    "mod",
    "support",
    "system",
    "root",
    "staff",
    "official",
    "null",
)
# Only these also reserve every nickname that starts with them.
_PREFIX_WORDS: Final = (
    "admin",
    "beebreak",
    "moderator",
    "support",
    "official",
    "staff",
)


class NicknameError(ValueError):
    def __init__(self, code: Literal["invalid_request", "nickname_reserved"]) -> None:
        super().__init__(code)
        self.code: Literal["invalid_request", "nickname_reserved"] = code


def skeleton(nickname: str) -> str:
    """Uniqueness key: casefold, map look-alike digits/letters, drop separators."""
    folded = nickname.casefold().translate(_CONFUSABLES)
    return folded.replace("_", "").replace("-", "")


_RESERVED_SKELETONS: Final = frozenset(skeleton(w) for w in _RESERVED_WORDS)
_RESERVED_PREFIXES: Final = tuple(skeleton(w) for w in _PREFIX_WORDS)


def _is_reserved(nickname: str) -> bool:
    key = skeleton(nickname)
    return key in _RESERVED_SKELETONS or key.startswith(_RESERVED_PREFIXES)


def validate_nickname(raw: str) -> str:
    """NFKC-normalized nickname; raises NicknameError."""
    nickname = unicodedata.normalize("NFKC", raw).strip()
    if _PATTERN.fullmatch(nickname) is None:
        raise NicknameError("invalid_request")
    # Only separators ("---", "_-_") would skeleton to nothing and collide.
    if _is_reserved(nickname) or not skeleton(nickname):
        raise NicknameError("nickname_reserved")
    return nickname
