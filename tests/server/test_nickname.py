import pytest

from breakroom_server.nickname import NicknameError, skeleton, validate_nickname


@pytest.mark.parametrize(
    "raw",
    ["Filip", "a_b-c", "abc", "a" * 24, "  Filip  ", "modest", "rooted", "systems"],
)
def test_valid_nicknames_pass(raw: str) -> None:
    assert validate_nickname(raw) == raw.strip()


@pytest.mark.parametrize(
    "raw",
    ["ab", "a" * 25, "with space", "café", "😀😀😀", "", "   ", "a.b.c", "x\ny\nz"],
)
def test_invalid_nicknames_are_rejected(raw: str) -> None:
    with pytest.raises(NicknameError) as exc:
        validate_nickname(raw)
    assert exc.value.code == "invalid_request"


def test_fullwidth_is_normalized() -> None:
    assert validate_nickname("\uff26\uff49\uff4c\uff49\uff50") == "Filip"


@pytest.mark.parametrize(
    "raw",
    ["Admin", "ADM1N", "r00t", "admin_x", "Breakroom-HQ", "NULL", "Mod", "5taff9"],
)
def test_reserved_nicknames_are_rejected(raw: str) -> None:
    with pytest.raises(NicknameError) as exc:
        validate_nickname(raw)
    assert exc.value.code == "nickname_reserved"


@pytest.mark.parametrize("raw", ["---", "___", "-_-", "_-_-_"])
def test_separator_only_nicknames_are_rejected(raw: str) -> None:
    with pytest.raises(NicknameError) as exc:
        validate_nickname(raw)
    assert exc.value.code == "nickname_reserved"


def test_error_message_does_not_contain_the_input() -> None:
    with pytest.raises(NicknameError) as exc:
        validate_nickname("bad name!")
    assert "bad name" not in str(exc.value)


def test_skeleton_collisions() -> None:
    assert skeleton("Filip_") == skeleton("fi1ip")
    assert skeleton("filip") != skeleton("philip")
