import pytest

from breakroom_server.tokens import (
    CODE_ALPHABET,
    hash_token,
    hmac_code,
    new_code,
    new_token,
    normalize_code,
)


def test_tokens_carry_prefix_and_differ() -> None:
    a, b = new_token("bra_"), new_token("bra_")
    assert a.startswith("bra_") and new_token("brr_").startswith("brr_")
    assert a != b
    assert len(a) >= 4 + 43


def test_hash_token_is_sha256_digest() -> None:
    assert len(hash_token("bra_x")) == 32
    assert hash_token("bra_x") == hash_token("bra_x") != hash_token("bra_y")


def test_new_code_uses_alphabet() -> None:
    code = new_code()
    assert len(code) == 8
    assert set(code) <= set(CODE_ALPHABET)
    assert not set(code) & set("ILOU")


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("ABCD1234", "ABCD1234"),
        ("abcd-1234", "ABCD1234"),
        (" abcd1234 ", "ABCD1234"),
        ("OILA2345", "011A2345"),
        ("ABCD123", None),
        ("ABCD12345", None),
        ("AB-CD-1234", None),
        ("ABCDU234", None),
        ("ABCD!234", None),
        ("", None),
    ],
)
def test_normalize_code(raw: str, expected: str | None) -> None:
    assert normalize_code(raw) == expected


def test_hmac_code_depends_on_secret() -> None:
    assert hmac_code(b"k" * 32, "ABCD1234") != hmac_code(b"j" * 32, "ABCD1234")
    assert hmac_code(b"k" * 32, "ABCD1234") == hmac_code(b"k" * 32, "ABCD1234")
