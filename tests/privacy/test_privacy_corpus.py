"""The privacy corpus and its scanner (M6-07), checked on their own.

No server here: these prove the corpus is well formed and that the scan finds a
value however it was re-encoded. The channel tests live in test_outbound_leaks.
"""

import base64
import hashlib
import json
import urllib.parse
from pathlib import Path

import httpx
import pytest

from tests.fixtures.privacy_corpus import (
    CURRENT_VERSION,
    SHA256,
    Corpus,
    load_corpus,
)
from tests.fixtures.privacy_corpus.generate import build
from tests.fixtures.privacy_corpus.scan import (
    find_corpus_leaks,
    find_leaks,
    find_marker_leaks,
    needles,
)

CORPUS = load_corpus()
BASE = CORPUS.bases[0]

CATEGORIES = {
    "api_key",
    "private_key_header",
    "env_line",
    "path_absolute",
    "path_windows",
    "path_wsl",
    "repo_name",
    "branch_name",
    "prompt",
    "shell_command",
    "stack_trace",
    "hostname",
    "session_id",
}
VARIANTS = {"url", "json", "base64", "upper", "homoglyph"}


def test_corpus_version_matches_its_file() -> None:
    assert CORPUS.version == CURRENT_VERSION
    path = Path(__file__).parents[1] / "fixtures" / "privacy_corpus"
    assert (path / f"v{CORPUS.version}.json").is_file()


def test_every_required_category_and_variant_kind_is_present() -> None:
    assert {v.category for v in CORPUS.bases} == CATEGORIES
    for category in CATEGORIES:
        assert len([v for v in CORPUS.bases if v.category == category]) >= 3
    assert {v.variant for v in CORPUS.values if v.variant} == VARIANTS


def test_ids_are_unique_and_variants_point_at_a_base() -> None:
    ids = [v.id for v in CORPUS.values]
    assert len(ids) == len(set(ids))
    base_ids = {v.id for v in CORPUS.bases}
    for v in CORPUS.values:
        if v.variant is None:
            assert v.variant_of is None
        else:
            assert v.variant_of in base_ids


def test_every_base_value_is_marked_fake() -> None:
    for v in CORPUS.bases:
        assert CORPUS.marker in v.value.lower(), v.id


def test_each_variant_is_the_encoding_it_claims_to_be() -> None:
    by_id = {v.id: v for v in CORPUS.values}
    for v in CORPUS.values:
        if v.variant is None:
            continue
        base = by_id[v.variant_of or ""].value
        match v.variant:
            case "url":
                assert urllib.parse.unquote(v.value) == base
            case "json":
                assert json.loads(f'"{v.value}"') == base
            case "base64":
                assert base64.b64decode(v.value).decode() == base
            case "upper":
                assert v.value == base.upper()
            case "homoglyph":
                assert v.value != base
                assert v.value.lower() != base.lower()
                assert not v.value.isascii()


def test_corpus_is_hashable_data_only() -> None:
    assert isinstance(CORPUS, Corpus)
    assert len(CORPUS.values) > 100


# --- the scanner ---


def _hits(blob: bytes, value: str = BASE.value) -> list[str]:
    return find_leaks({"c": [blob]}, [("v", value)])


@pytest.mark.parametrize(
    "encode",
    [
        lambda s: s,
        lambda s: json.dumps(s)[1:-1],
        lambda s: json.dumps(s, ensure_ascii=False)[1:-1],
        lambda s: urllib.parse.quote(s, safe=""),
        lambda s: s.upper(),
        lambda s: s.lower(),
    ],
    ids=["plain", "json", "json-utf8", "url", "upper", "lower"],
)
def test_scanner_finds_a_text_reencoding(encode) -> None:  # type: ignore[no-untyped-def]
    value = 'Path "C:\\Users\\breakroom-canary\\x" é\nline2'
    assert _hits(f"prefix {encode(value)} suffix".encode(), value)


@pytest.mark.parametrize("prefix", [b"", b"a", b"ab"])
@pytest.mark.parametrize("suffix", [b"", b"z", b"yz"])
def test_scanner_finds_base64_at_any_alignment(prefix: bytes, suffix: bytes) -> None:
    wrapped = base64.b64encode(prefix + BASE.value.encode() + suffix)
    assert _hits(b"token=" + wrapped)


def test_scanner_finds_hex() -> None:
    assert _hits(BASE.value.encode().hex().encode())


def test_scanner_reports_nothing_for_unrelated_bytes() -> None:
    assert _hits(b"nothing to see here, ordinary traffic 0123456789") == []
    assert _hits(base64.b64encode(b"some other long unrelated payload")) == []


def test_scanner_reports_the_channel_and_the_corpus_id() -> None:
    channels = {
        "clean": [b"ok"],
        "dirty": [b"x" + BASE.value.encode()],
    }
    hits = find_corpus_leaks(channels, CORPUS.values)
    assert f"dirty: {BASE.id}" in hits
    assert not any(h.startswith("clean: ") for h in hits)


def test_every_corpus_value_is_found_when_planted() -> None:
    for v in CORPUS.values:
        assert find_corpus_leaks({"c": [v.value.encode()]}, [v]), v.id


def test_needles_skip_hex_and_base64_forms_too_short_to_search_for() -> None:
    forms = needles("abc")
    assert b"abc" in forms
    assert b"616263" not in forms  # hex of "abc"
    assert b"YWJj" not in forms  # base64 of "abc"


def test_corpus_file_matches_its_pinned_hash() -> None:
    path = Path(__file__).parents[1] / "fixtures" / "privacy_corpus"
    for version, digest in SHA256.items():
        raw = (path / f"v{version}.json").read_bytes()
        assert hashlib.sha256(raw).hexdigest() == digest
    assert CURRENT_VERSION in SHA256


def test_generator_reproduces_v1_byte_for_byte() -> None:
    path = Path(__file__).parents[1] / "fixtures" / "privacy_corpus" / "v1.json"
    assert build().encode("utf-8") == path.read_bytes()


# --- encodings the first scanner missed ---


def test_scanner_finds_form_encoded_spaces_in_a_real_httpx_request() -> None:
    value = next(v.value for v in CORPUS.bases if " " in v.value)
    request = httpx.Request("GET", "http://127.0.0.1/search", params={"q": value})
    assert value.encode() not in request.url.query
    assert _hits(request.url.query, value)


def test_scanner_finds_quote_plus_form() -> None:
    value = "two words, breakroom-canary & more"
    assert _hits(urllib.parse.quote_plus(value).encode(), value)


def test_scanner_finds_upper_case_hex() -> None:
    assert _hits(BASE.value.encode().hex().upper().encode())


def test_scanner_finds_urlsafe_base64() -> None:
    value = "\u00ff\u00fe??>>breakroom-canary~~\u00ff"
    wrapped = base64.urlsafe_b64encode(b"zz" + value.encode()).rstrip(b"=")
    assert b"-" in wrapped or b"_" in wrapped
    assert _hits(b"t=" + wrapped, value)


def test_scanner_finds_a_truncated_fragment_through_the_marker() -> None:
    channels = {"c": [b"... breakroom-canary-0001-9f8e ..."]}
    assert find_corpus_leaks(channels, CORPUS.bases[:1]) == []  # not the full value
    assert find_marker_leaks(channels, CORPUS.marker) == ["c: marker"]


def test_scanner_finds_the_homoglyph_marker() -> None:
    homoglyph = next(v for v in CORPUS.values if v.variant == "homoglyph")
    channels = {"c": [homoglyph.value.encode()]}
    assert "c: marker.homoglyph" in find_marker_leaks(channels, CORPUS.marker)
