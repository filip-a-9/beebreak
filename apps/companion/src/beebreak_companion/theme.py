"""The "Tea & Honey" look: tokens, palette, stylesheet, font and logo mark.

One application-wide stylesheet on the Fusion style, applied once at startup.
State code never touches colours: it sets the dynamic property `state` with
`set_state` and the stylesheet keys on it.
"""

import logging
import re
import subprocess
from dataclasses import dataclass
from importlib import resources
from importlib.resources.abc import Traversable
from typing import Literal

from PySide6.QtCore import QByteArray, Qt
from PySide6.QtGui import (
    QColor,
    QFont,
    QFontDatabase,
    QIcon,
    QPainter,
    QPalette,
    QPixmap,
)
from PySide6.QtSvg import QSvgRenderer
from PySide6.QtWidgets import QApplication, QWidget

from beebreak_companion.config import theme_probe_timeout_s, theme_setting

log = logging.getLogger(__name__)

Scheme = Literal["light", "dark"]

FONT_FAMILY = "Nunito"
FONT_FILES = (
    "Nunito-Regular.ttf",
    "Nunito-SemiBold.ttf",
    "Nunito-Bold.ttf",
    "Nunito-ExtraBold.ttf",
)
RADIUS = 18
_MARK_FG = "#2A221B"  # the foreground colour baked into mark.svg
_REG_KEY = r"HKCU\Software\Microsoft\Windows\CurrentVersion\Themes\Personalize"
_REG_VALUE = "AppsUseLightTheme"


@dataclass(frozen=True)
class Pill:
    bg: str
    fg: str
    dot: str


@dataclass(frozen=True)
class Tokens:
    bg: str
    paper: str
    fg: str
    muted: str
    line: str
    field: str  # borders that identify a control (3:1 against bg and paper)
    soft: str
    bubble: str
    accent: str
    accent_text: str
    on_accent: str
    danger: str
    # Keyed by the `state` property value.
    pills: dict[str, Pill]


TOKENS: dict[Scheme, Tokens] = {
    "light": Tokens(
        bg="#FBF6EE",
        paper="#FFFCF6",
        fg="#2A221B",
        muted="#6E6255",
        line="#EADFCF",
        field="#8A7E6E",
        soft="#F6E7C8",
        bubble="#F2EADD",
        accent="#E2A548",
        accent_text="#5E3D0B",
        on_accent="#2A1C08",
        danger="#A3402A",
        pills={
            "searching": Pill("#F6E7C8", "#5E3D0B", "#C98A2B"),
            "connected": Pill("#E4EBDD", "#34482A", "#5F7F4A"),
            "offline": Pill("#F5E1DA", "#7A3420", "#B4532F"),
            "off": Pill("#EFE9E0", "#6E6255", "#A59886"),
        },
    ),
    "dark": Tokens(
        bg="#1E1A16",
        paper="#25201B",
        fg="#F1E8DC",
        muted="#ABA090",
        line="#3B342C",
        field="#7E7262",
        soft="#3A2E1C",
        bubble="#302922",
        accent="#E8B45E",
        accent_text="#F2CD8E",
        on_accent="#22180A",
        danger="#E8907A",
        pills={
            "searching": Pill("#3A2E1C", "#F2CD8E", "#E8B45E"),
            "connected": Pill("#28331F", "#BFD6B0", "#8DB475"),
            "offline": Pill("#3F2620", "#F0B5A3", "#E8907A"),
            "off": Pill("#2E2823", "#ABA090", "#8A7F70"),
        },
    ),
}

_active: Scheme = "light"


def active_scheme() -> Scheme:
    """The scheme `apply_theme` last applied (light before any)."""
    return _active


# --- light or dark ---


def windows_scheme() -> Scheme | None:
    """Windows' app theme via interop, or None when it cannot be read."""
    try:
        done = subprocess.run(
            ["reg.exe", "query", _REG_KEY, "/v", _REG_VALUE],
            capture_output=True,
            timeout=theme_probe_timeout_s(),
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if done.returncode != 0:
        return None
    match = re.search(
        rf"{_REG_VALUE}\s+REG_DWORD\s+0x([0-9a-fA-F]+)",
        done.stdout.decode("utf-8", errors="ignore"),
    )
    if match is None:
        return None
    value = int(match.group(1), 16)
    if value == 0:
        return "dark"
    return "light" if value == 1 else None


def resolve_scheme(setting: str) -> Scheme:
    """`light`/`dark` as given; `auto` follows Windows and falls back to light."""
    if setting == "light":
        return "light"
    if setting == "dark":
        return "dark"
    return windows_scheme() or "light"


# --- palette and stylesheet ---


def build_palette(t: Tokens) -> QPalette:
    p = QPalette()
    roles = {
        QPalette.ColorRole.Window: t.bg,
        QPalette.ColorRole.WindowText: t.fg,
        QPalette.ColorRole.Base: t.paper,
        QPalette.ColorRole.AlternateBase: t.bubble,
        QPalette.ColorRole.Text: t.fg,
        QPalette.ColorRole.Button: t.paper,
        QPalette.ColorRole.ButtonText: t.fg,
        QPalette.ColorRole.BrightText: t.fg,
        QPalette.ColorRole.Highlight: t.accent,
        QPalette.ColorRole.HighlightedText: t.on_accent,
        QPalette.ColorRole.PlaceholderText: t.muted,
        QPalette.ColorRole.ToolTipBase: t.paper,
        QPalette.ColorRole.ToolTipText: t.fg,
        QPalette.ColorRole.Link: t.accent_text,
        QPalette.ColorRole.Mid: t.line,
    }
    for role, color in roles.items():
        p.setColor(role, QColor(color))
    for role in (
        QPalette.ColorRole.WindowText,
        QPalette.ColorRole.Text,
        QPalette.ColorRole.ButtonText,
    ):
        p.setColor(QPalette.ColorGroup.Disabled, role, QColor(t.muted))
    return p


def build_stylesheet(t: Tokens) -> str:
    """The whole look. Every rule with a text and a background colour uses a
    pair measured in tests/unit/test_theme_contrast.py."""
    r = RADIUS
    pills = "\n".join(
        f'QLabel[state="{name}"] {{ background: {pill.bg}; color: {pill.fg};'
        f" border: 1px solid {pill.dot}; border-radius: 13px;"
        f" padding: 3px 12px; font-weight: 700; }}"
        for name, pill in t.pills.items()
    )
    # A filled accent box with a dark centre dot: reads as "on" without an image.
    dot = (
        "qradialgradient(cx:0.5, cy:0.5, radius:0.5, fx:0.5, fy:0.5, "
        f"stop:0 {t.on_accent}, stop:0.4 {t.on_accent}, "
        f"stop:0.45 {t.accent}, stop:1 {t.accent})"
    )
    return f"""
QToolTip {{ background: {t.paper}; color: {t.fg}; border: 1px solid {t.line}; }}
QLabel#wordmark {{ font-size: 18px; font-weight: 800; }}
QLabel#peerName {{ font-size: 16px; font-weight: 700; }}
QLabel#muted {{ color: {t.muted}; font-size: 12px; }}
QLabel#footer {{ color: {t.muted}; font-size: 11px; }}
QLabel#caption {{ color: {t.muted}; font-size: 11px; }}
QLabel#captionFailed {{ color: {t.danger}; font-size: 11px; font-weight: 700; }}
QLabel#avatar {{ background: {t.soft}; color: {t.accent_text};
  border-radius: 18px; font-weight: 800; font-size: 15px; }}
QLabel#timePill {{ background: {t.soft}; color: {t.accent_text};
  border: 1px solid {t.soft}; border-radius: 12px; padding: 3px 10px;
  font-weight: 700; font-size: 12px; }}
QLabel#error {{ color: {t.danger}; font-weight: 700; }}
{pills}
QLabel[role="mine"] {{ background: {t.accent}; color: {t.on_accent};
  border: 1px solid {t.accent};
  border-radius: {r}px; padding: 8px 12px; }}
QLabel[role="peer"] {{ background: {t.bubble}; color: {t.fg};
  border: 1px solid {t.bubble};
  border-radius: {r}px; padding: 8px 12px; }}
QLabel[role="pending"] {{ background: {t.soft}; color: {t.fg};
  border: 1px dashed {t.accent}; border-radius: {r}px; padding: 8px 12px; }}
QFrame#card {{ background: {t.paper}; border: 1px solid {t.line};
  border-radius: {r}px; }}
QFrame#banner {{ background: {t.soft}; border-radius: 14px; }}
QFrame#banner QLabel {{ color: {t.fg}; }}
QPushButton {{ background: {t.paper}; color: {t.fg}; border: 1px solid {t.field};
  border-radius: 17px; padding: 6px 16px; min-height: 20px; font-weight: 700; }}
QPushButton:hover {{ background: {t.soft}; color: {t.fg}; }}
QPushButton:disabled {{ background: {t.bg}; color: {t.muted}; }}
QPushButton#primary {{ background: {t.accent}; color: {t.on_accent};
  border: 1px solid {t.accent}; min-height: 26px; }}
QPushButton#primary:hover {{ background: {t.accent}; color: {t.on_accent};
  border: 1px solid {t.on_accent}; }}
QPushButton#primary:disabled {{ background: {t.bg}; color: {t.muted};
  border: 1px solid {t.line}; }}
QPushButton#danger {{ background: {t.paper}; color: {t.danger};
  border: 1px solid {t.danger}; }}
QPushButton#danger:hover {{ background: {t.bubble}; color: {t.danger}; }}
QPushButton#danger:disabled {{ background: {t.bg}; color: {t.muted};
  border: 1px solid {t.line}; }}
QPushButton#retry {{ color: {t.danger}; border: none; padding: 0 4px;
  min-height: 0; font-size: 11px; background: transparent; }}
QPlainTextEdit, QLineEdit {{ background: {t.paper}; color: {t.fg};
  border: 1px solid {t.field}; border-radius: {r}px; padding: 6px 12px; }}
QPlainTextEdit:focus, QLineEdit:focus {{ border: 1px solid {t.accent}; }}
QListWidget {{ background: {t.paper}; color: {t.fg}; border: 1px solid {t.field};
  border-radius: 12px; padding: 4px; }}
QMenu {{ background: {t.paper}; color: {t.fg}; border: 1px solid {t.line};
  padding: 4px; }}
QMenu::item {{ padding: 4px 16px; border-radius: 6px; }}
QMenu::item:selected {{ background: {t.soft}; color: {t.fg}; }}
QCheckBox::indicator {{ width: 14px; height: 14px; background: {t.paper};
  border: 2px solid {t.field}; border-radius: 5px; }}
QRadioButton::indicator {{ width: 14px; height: 14px; background: {t.paper};
  border: 2px solid {t.field}; border-radius: 9px; }}
QListView::indicator {{ width: 14px; height: 14px; background: {t.paper};
  border: 2px solid {t.field}; border-radius: 5px; }}
QListView::indicator:hover {{ border: 2px solid {t.accent_text}; }}
QListView::indicator:checked {{ border: 2px solid {t.on_accent};
  background: {dot}; }}
QCheckBox::indicator:hover {{ border: 2px solid {t.accent_text}; }}
QRadioButton::indicator:hover {{ border: 2px solid {t.accent_text}; }}
QCheckBox::indicator:checked {{ border: 2px solid {t.on_accent}; background: {dot}; }}
QRadioButton::indicator:checked {{ border: 2px solid {t.on_accent};
  background: {dot}; }}
QCheckBox::indicator:disabled, QRadioButton::indicator:disabled {{
  background: {t.bg}; border: 2px solid {t.line}; }}
QScrollArea {{ border: none; background: transparent; }}
QWidget#transcript {{ background: transparent; }}
QScrollBar:vertical {{ background: transparent; width: 10px; margin: 0; }}
QScrollBar::handle:vertical {{ background: {t.line}; border-radius: 5px;
  min-height: 24px; }}
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{ height: 0; }}
"""


# --- font ---


def load_fonts(folder: Traversable | None = None) -> bool:
    """Register the bundled Nunito weights; False (and the system font stays)
    when any of them cannot be read."""
    try:
        base = folder or resources.files("beebreak_companion.assets.fonts")
        for name in FONT_FILES:
            data = base.joinpath(name).read_bytes()
            if QFontDatabase.addApplicationFontFromData(QByteArray(data)) < 0:
                log.warning("bundled font could not be loaded")
                return False
    except OSError:
        log.warning("bundled font files are missing")
        return False
    return True


# --- mark ---


def mark_svg(scheme: Scheme) -> str:
    """The logo with its foreground recoloured for `scheme`."""
    svg = resources.files("beebreak_companion.assets").joinpath("mark.svg")
    return svg.read_text().replace(_MARK_FG, TOKENS[scheme].fg)


def mark_pixmap(scheme: Scheme, size: int) -> QPixmap:
    renderer = QSvgRenderer(QByteArray(mark_svg(scheme).encode()))
    pixmap = QPixmap(size, size)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    renderer.render(painter)
    painter.end()
    return pixmap


def mark_icon(scheme: Scheme) -> QIcon:
    icon = QIcon()
    for size in (16, 24, 32, 48, 64, 128, 256):
        icon.addPixmap(mark_pixmap(scheme, size))
    return icon


# --- applying ---


def set_state(widget: QWidget, state: str) -> None:
    """Give `widget` a UI state; the stylesheet decides what it looks like."""
    if widget.property("state") == state:
        return
    widget.setProperty("state", state)
    _repolish(widget)


def set_role(widget: QWidget, role: str) -> None:
    """Like `set_state`, for what a widget is (a message bubble's side)."""
    if widget.property("role") == role:
        return
    widget.setProperty("role", role)
    _repolish(widget)


def _repolish(widget: QWidget) -> None:
    style = widget.style()
    style.unpolish(widget)
    style.polish(widget)
    widget.update()


def apply_theme(app: QApplication, setting: str | None = None) -> Scheme:
    """Style, palette, stylesheet, font and icon, once at startup."""
    global _active
    scheme = resolve_scheme(setting if setting is not None else theme_setting())
    _active = scheme
    tokens = TOKENS[scheme]
    app.setStyle("Fusion")
    app.setPalette(build_palette(tokens))
    if load_fonts():
        app.setFont(QFont(FONT_FAMILY, 10))
    app.setStyleSheet(build_stylesheet(tokens))
    app.setWindowIcon(mark_icon(scheme))
    return scheme
