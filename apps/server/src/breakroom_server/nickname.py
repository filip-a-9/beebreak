"""Nickname validation and the skeleton used for uniqueness. Owned by M3-07."""

from typing import Literal


class NicknameError(ValueError):
    def __init__(self, code: Literal["invalid_request", "nickname_reserved"]) -> None:
        super().__init__(code)
        self.code: Literal["invalid_request", "nickname_reserved"] = code


def validate_nickname(raw: str) -> str:
    """NFKC-normalized nickname; raises NicknameError."""
    raise NotImplementedError


def skeleton(nickname: str) -> str:
    raise NotImplementedError
