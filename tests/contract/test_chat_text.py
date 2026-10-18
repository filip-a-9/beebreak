import pytest

from beebreak_shared.chat import MAX_MESSAGE_CHARS, MAX_NOTE_CHARS, normalize_text

# Written with chr(): invisible characters must not hide in the source.
RLO = chr(0x202E)  # right-to-left override
RLM = chr(0x200F)  # right-to-left mark
ACUTE = chr(0x0301)  # combining acute accent
E_ACUTE = chr(0x00E9)


def test_plain_text_is_unchanged() -> None:
    assert normalize_text("hello there") == "hello there"


def test_surrounding_whitespace_is_stripped() -> None:
    assert normalize_text("  \n hi \n ") == "hi"


@pytest.mark.parametrize("raw", ["", "   ", "\n\n", "\t\x00\x07", RLO + RLM])
def test_empty_after_normalization_is_none(raw: str) -> None:
    assert normalize_text(raw) is None


def test_control_characters_are_removed_except_newline() -> None:
    assert normalize_text("a\x00b\x07c\td\re\nf\x7fg") == "abcde\nfg"


def test_crlf_becomes_newline() -> None:
    assert normalize_text("a\r\nb") == "a\nb"


@pytest.mark.parametrize(
    "control",
    [chr(c) for c in (0x202A, 0x202B, 0x202C, 0x202D, 0x202E)]
    + [chr(c) for c in (0x2066, 0x2067, 0x2068, 0x2069, 0x200E, 0x200F)],
)
def test_bidi_controls_are_removed(control: str) -> None:
    assert normalize_text(f"ab{control}cd") == "abcd"


def test_other_format_characters_are_kept() -> None:
    # Zero-width joiner is part of emoji sequences; it is not a bidi control.
    family = "\U0001f468" + chr(0x200D) + "\U0001f469"
    assert normalize_text(family) == family


def test_three_or_more_newlines_collapse_to_two() -> None:
    assert normalize_text("a\n\n\n\n\nb\n\nc") == "a\n\nb\n\nc"


def test_newlines_split_by_removed_controls_still_collapse() -> None:
    assert normalize_text("a\n\x00\n" + RLO + "\n\nb") == "a\n\nb"


def test_nfc() -> None:
    assert normalize_text("e" + ACUTE) == E_ACUTE


def test_length_counts_code_points_after_normalization() -> None:
    assert normalize_text("x" * MAX_MESSAGE_CHARS) == "x" * MAX_MESSAGE_CHARS
    assert normalize_text("x" * (MAX_MESSAGE_CHARS + 1)) is None
    # Combining sequences that compose under NFC count once.
    composed = normalize_text(("e" + ACUTE) * MAX_MESSAGE_CHARS)
    assert composed == E_ACUTE * MAX_MESSAGE_CHARS
    # Removed characters do not count.
    assert normalize_text("x" * MAX_MESSAGE_CHARS + "\x00" * 50) is not None


def test_four_byte_characters_count_once() -> None:
    text = "\U0001f600" * MAX_MESSAGE_CHARS
    assert normalize_text(text) == text


def test_note_limit() -> None:
    assert normalize_text("n" * MAX_NOTE_CHARS, max_chars=MAX_NOTE_CHARS) is not None
    assert normalize_text("n" * (MAX_NOTE_CHARS + 1), max_chars=MAX_NOTE_CHARS) is None


def test_html_is_left_as_text() -> None:
    # Plain text: markup is not stripped or escaped here; the UI renders it as
    # plain text (rule 17).
    assert normalize_text("<b>hi</b>") == "<b>hi</b>"
