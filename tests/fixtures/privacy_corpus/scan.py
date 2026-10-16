"""Find a protected value, or any re-encoding of it, in captured bytes.

A leak that went through JSON, a URL, base64 or a case change must still be
caught, so each value is searched for in all of those forms.
"""

import base64
import json
import urllib.parse
from collections.abc import Iterable

from tests.fixtures.privacy_corpus import CorpusValue

MIN_NEEDLE = 8  # shorter hex/base64 forms match by accident
# Latin letters swapped for Cyrillic look-alikes; the corpus generator uses it too.
HOMOGLYPHS = str.maketrans(
    {
        "a": "\u0430",
        "e": "\u0435",
        "o": "\u043e",
        "p": "\u0440",
        "c": "\u0441",
        "x": "\u0445",
    }
)
_URLSAFE = bytes.maketrans(b"+/", b"-_")


def _b64_forms(data: bytes) -> set[bytes]:
    """Base64 of `data` at each of the three alignments it can have inside a
    larger encoded buffer, keeping only the characters that do not depend on
    the neighbouring bytes."""
    out = set()
    for offset in range(3):
        encoded = base64.b64encode(b"\0" * offset + data)
        total = offset + len(data)
        end = len(encoded) if total % 3 == 0 else (total // 3) * 4
        out.add(encoded[(0, 2, 3)[offset] : end])
    return out | {f.translate(_URLSAFE) for f in out}


def needles(value: str) -> set[bytes]:
    """Every byte string that would mean `value` was written out."""
    forms = {value, value.lower(), value.upper()}
    for text in list(forms):
        forms |= {
            json.dumps(text)[1:-1],
            json.dumps(text, ensure_ascii=False)[1:-1],
            urllib.parse.quote(text, safe=""),
            urllib.parse.quote(text),
            urllib.parse.quote_plus(text),  # form encoding: spaces become "+"
        }
    found: set[bytes] = set()
    for text in forms:
        raw = text.encode("utf-8")
        found.add(raw)
        hexed = raw.hex().encode()
        found |= {
            n for n in {hexed, hexed.upper(), *_b64_forms(raw)} if len(n) >= MIN_NEEDLE
        }
    return found


def find_leaks(
    channels: dict[str, list[bytes]], values: Iterable[tuple[str, str]]
) -> list[str]:
    """`channel: label` for each (label, value) found in a channel's blobs."""
    searches = [(label, needles(value)) for label, value in values]
    hits = []
    for channel, blobs in channels.items():
        joined = b"\n\0\n".join(blobs)
        for label, options in searches:
            if any(n in joined for n in options):
                hits.append(f"{channel}: {label}")
    return hits


def find_corpus_leaks(
    channels: dict[str, list[bytes]], corpus: Iterable[CorpusValue]
) -> list[str]:
    return find_leaks(channels, ((v.id, v.value) for v in corpus))


def find_marker_leaks(channels: dict[str, list[bytes]], marker: str) -> list[str]:
    """The fake-value marker itself (and its homoglyph form) anywhere at all.

    Catches a truncated or partly redacted fragment of a corpus value that no
    full-value search would find.
    """
    return find_leaks(
        channels,
        [("marker", marker), ("marker.homoglyph", marker.translate(HOMOGLYPHS))],
    )
