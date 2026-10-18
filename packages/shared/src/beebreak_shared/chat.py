"""Chat text rules shared by the companion and the server (M5).

The companion normalizes before sending (to disable Send and show the counter);
the server normalizes again and is the authority. Both use `normalize_text`.
"""

import re
import unicodedata

MAX_MESSAGE_CHARS = 1000
MAX_NOTE_CHARS = 500

# Bidi embedding/override/isolate controls and the LRM/RLM marks: they can make
# text render in an order other than the one it was written in.
_BIDI_CONTROLS = frozenset(
    [chr(c) for c in range(0x202A, 0x202F)]
    + [chr(c) for c in range(0x2066, 0x206A)]
    + [chr(0x200E), chr(0x200F)]
)
_EXTRA_NEWLINES = re.compile(r"\n{3,}")


def _kept(char: str) -> bool:
    if char == "\n":
        return True
    if char in _BIDI_CONTROLS:
        return False
    return unicodedata.category(char) != "Cc"


def normalize_text(raw: str, *, max_chars: int = MAX_MESSAGE_CHARS) -> str | None:
    """Plain chat text, or None when it is empty or too long.

    NFC; control characters removed except newline; bidi controls removed;
    three or more consecutive newlines collapsed to two; surrounding whitespace
    stripped. The length limit counts code points after all of this.
    """
    text = unicodedata.normalize("NFC", raw)
    text = "".join(c for c in text if _kept(c))
    text = _EXTRA_NEWLINES.sub("\n\n", text).strip()
    if not text or len(text) > max_chars:
        return None
    return text
