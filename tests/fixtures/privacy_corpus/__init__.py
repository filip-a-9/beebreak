"""Versioned privacy corpus (M6-07): synthetic, unmistakably fake values.

`v<N>.json` holds the values; any change to it bumps the version (a new file,
the old one kept so a run can name exactly what it used). Every base value
contains the marker `breakroom-canary`, so no secrets scanner flags it and no
one mistakes it for a real credential. Variants (URL-encoded, JSON-escaped,
base64, upper case, homoglyphs) are stored with the file, each pointing at the
base value it came from.

Each version's sha256 is pinned in `SHA256`, so editing a file without bumping
the version fails a test. `generate.py` rebuilds the file; a test checks that
it reproduces it byte for byte.

Reuse: `load_corpus()` here, `find_corpus_leaks()` / `needles()` in `.scan`.
"""

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path

CURRENT_VERSION = 1
SHA256 = {1: "e1ddc63199f3e8c7b0e115c7d5e158cad4766e7cd68aa241e233814dabc6007b"}
_DIR = Path(__file__).parent


@dataclass(frozen=True)
class CorpusValue:
    id: str
    category: str
    value: str
    variant: str | None  # None for a base value
    variant_of: str | None  # id of the base value


@dataclass(frozen=True)
class Corpus:
    version: int
    marker: str
    values: tuple[CorpusValue, ...]

    @property
    def bases(self) -> tuple[CorpusValue, ...]:
        return tuple(v for v in self.values if v.variant is None)

    def category(self, name: str) -> tuple[CorpusValue, ...]:
        return tuple(v for v in self.values if v.category == name)


def load_corpus(version: int = CURRENT_VERSION) -> Corpus:
    raw = (_DIR / f"v{version}.json").read_bytes()
    if hashlib.sha256(raw).hexdigest() != SHA256[version]:
        raise ValueError(f"corpus v{version} changed without a version bump")
    data = json.loads(raw.decode("utf-8"))
    return Corpus(
        version=data["version"],
        marker=data["marker"],
        values=tuple(
            CorpusValue(
                id=v["id"],
                category=v["category"],
                value=v["value"],
                variant=v["variant"],
                variant_of=v["variant_of"],
            )
            for v in data["values"]
        ),
    )
