"""Contrast of every text/background pair the theme uses (WCAG 2.x).

The luminance and ratio below are written out here on purpose: the test must not
call the theme's own helpers to judge the theme.
"""

import re

import pytest

from beebreak_companion import theme

SCHEMES = ("light", "dark")
NORMAL = 4.5

# Token-name pairs (text, background) the stylesheet uses.
TOKEN_PAIRS = [
    ("fg", "bg"),
    ("fg", "paper"),
    ("fg", "bubble"),
    ("fg", "soft"),
    ("muted", "bg"),
    ("muted", "paper"),
    ("muted", "bubble"),
    ("muted", "soft"),
    ("accent_text", "soft"),
    ("accent_text", "paper"),
    ("on_accent", "accent"),
    ("danger", "bg"),
    ("danger", "paper"),
    ("danger", "bubble"),
]
# Non-text pairs (control borders and indicator marks), WCAG 1.4.11.
NON_TEXT = 3.0
NON_TEXT_PAIRS = [
    ("field", "bg"),
    ("field", "paper"),
    ("accent_text", "bg"),
    ("accent_text", "paper"),
    ("on_accent", "accent"),
]
PILL_STATES = ("searching", "connected", "offline", "off")


def _channel(value: int) -> float:
    c = value / 255
    return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4


def _luminance(hex_color: str) -> float:
    r, g, b = (int(hex_color[i : i + 2], 16) for i in (1, 3, 5))
    return 0.2126 * _channel(r) + 0.7152 * _channel(g) + 0.0722 * _channel(b)


def _ratio(a: str, b: str) -> float:
    hi, lo = sorted((_luminance(a), _luminance(b)), reverse=True)
    return (hi + 0.05) / (lo + 0.05)


@pytest.mark.parametrize("scheme", SCHEMES)
@pytest.mark.parametrize(("text", "ground"), TOKEN_PAIRS)
def test_token_pair_is_readable(scheme: theme.Scheme, text: str, ground: str) -> None:
    tokens = theme.TOKENS[scheme]
    assert _ratio(getattr(tokens, text), getattr(tokens, ground)) >= NORMAL


@pytest.mark.parametrize("scheme", SCHEMES)
@pytest.mark.parametrize("state", PILL_STATES)
def test_state_pill_is_readable(scheme: theme.Scheme, state: str) -> None:
    pill = theme.TOKENS[scheme].pills[state]
    assert _ratio(pill.fg, pill.bg) >= NORMAL


@pytest.mark.parametrize("scheme", SCHEMES)
def test_pill_states_have_distinct_colours(scheme: theme.Scheme) -> None:
    pills = theme.TOKENS[scheme].pills
    assert set(pills) == set(PILL_STATES)
    assert len({p.bg for p in pills.values()}) == len(PILL_STATES)


@pytest.mark.parametrize("scheme", SCHEMES)
def test_stylesheet_pairs_are_all_declared(scheme: theme.Scheme) -> None:
    """Any rule that sets both a text and a background colour uses a pair this
    file measured."""
    tokens = theme.TOKENS[scheme]
    measured = {
        (getattr(tokens, t).lower(), getattr(tokens, g).lower()) for t, g in TOKEN_PAIRS
    }
    measured |= {(p.fg.lower(), p.bg.lower()) for p in tokens.pills.values()}
    sheet = theme.build_stylesheet(tokens)
    seen = 0
    for block in sheet.split("}"):
        text = re.search(r"(?<![-\w])color:\s*(#[0-9A-Fa-f]{6})", block)
        ground = re.search(r"background(?:-color)?:\s*(#[0-9A-Fa-f]{6})", block)
        if text and ground:
            seen += 1
            assert (text.group(1).lower(), ground.group(1).lower()) in measured
    assert seen > 5


@pytest.mark.parametrize("scheme", SCHEMES)
@pytest.mark.parametrize(("a", "b"), NON_TEXT_PAIRS)
def test_non_text_pair_is_visible(scheme: theme.Scheme, a: str, b: str) -> None:
    tokens = theme.TOKENS[scheme]
    assert _ratio(getattr(tokens, a), getattr(tokens, b)) >= NON_TEXT


def _rule(sheet: str, selector: str) -> str:
    for block in sheet.split("}"):
        if block.strip().startswith(selector):
            return block
    raise AssertionError(f"no rule for {selector}")


@pytest.mark.parametrize("scheme", SCHEMES)
@pytest.mark.parametrize("control", ["QCheckBox", "QRadioButton", "QListView"])
def test_indicators_are_drawn_with_the_visible_tokens(
    scheme: theme.Scheme, control: str
) -> None:
    tokens = theme.TOKENS[scheme]
    sheet = theme.build_stylesheet(tokens)
    unchecked = _rule(sheet, f"{control}::indicator {{")
    assert tokens.field.lower() in unchecked.lower()
    checked = _rule(sheet, f"{control}::indicator:checked")
    assert tokens.accent.lower() in checked.lower()
    assert tokens.on_accent.lower() in checked.lower()
    hover = _rule(sheet, f"{control}::indicator:hover")
    assert tokens.accent_text.lower() in hover.lower()


@pytest.mark.parametrize("scheme", SCHEMES)
def test_text_fields_and_lists_use_the_field_border(scheme: theme.Scheme) -> None:
    tokens = theme.TOKENS[scheme]
    sheet = theme.build_stylesheet(tokens)
    for selector in ("QPlainTextEdit, QLineEdit {", "QListWidget {", "QPushButton {"):
        assert tokens.field.lower() in _rule(sheet, selector).lower()


def test_light_tokens_match_the_mockup() -> None:
    t = theme.TOKENS["light"]
    assert (t.bg, t.paper, t.fg, t.accent) == (
        "#FBF6EE",
        "#FFFCF6",
        "#2A221B",
        "#E2A548",
    )
    assert (t.line, t.soft, t.bubble) == ("#EADFCF", "#F6E7C8", "#F2EADD")
