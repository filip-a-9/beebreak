"""The theme applied to real widgets: palette, style, font, and state colours."""

from collections.abc import Iterator

import pytest
from PySide6.QtGui import QColor
from PySide6.QtWidgets import QApplication, QLabel
from pytestqt.qtbot import QtBot

from beebreak_companion import theme

# Pill backgrounds from the design mockup (light) and the chosen dark set.
LIGHT_PILLS = {
    "searching": "#F6E7C8",
    "connected": "#E4EBDD",
    "offline": "#F5E1DA",
    "off": "#EFE9E0",
}


@pytest.fixture
def themed(qtbot: QtBot) -> Iterator[QApplication]:
    app = QApplication.instance()
    assert isinstance(app, QApplication)
    style, sheet, palette, font = (
        app.style().objectName(),
        app.styleSheet(),
        app.palette(),
        app.font(),
    )
    yield app
    app.setStyleSheet(sheet)
    app.setPalette(palette)
    app.setFont(font)
    app.setStyle(style)


def test_apply_sets_style_palette_font_and_icon(themed: QApplication) -> None:
    scheme = theme.apply_theme(themed, "dark")
    assert scheme == "dark"
    themed.setStyleSheet("")  # the sheet wraps the style; look at the base
    assert themed.style().name().lower() == "fusion"
    assert themed.palette().window().color().name().upper() == "#1E1A16"
    assert themed.palette().windowText().color().name().upper() == "#F1E8DC"
    assert themed.font().family() == "Nunito"
    assert not themed.windowIcon().isNull()


def _pill_pixel(qtbot: QtBot, state: str) -> str:
    label = QLabel("ok")
    label.setObjectName("statePill")
    label.setFixedSize(220, 36)
    qtbot.addWidget(label)
    theme.set_state(label, state)
    label.show()
    image = label.grab().toImage()
    return QColor(image.pixelColor(190, 18)).name().upper()


@pytest.mark.parametrize("state", list(LIGHT_PILLS))
def test_each_state_gets_its_own_pill_colour(
    themed: QApplication, qtbot: QtBot, state: str
) -> None:
    theme.apply_theme(themed, "light")
    assert _pill_pixel(qtbot, state) == LIGHT_PILLS[state]


def test_changing_the_state_repaints_the_same_widget(
    themed: QApplication, qtbot: QtBot
) -> None:
    theme.apply_theme(themed, "light")
    label = QLabel("ok")
    label.setObjectName("statePill")
    label.setFixedSize(220, 36)
    qtbot.addWidget(label)
    seen = []
    for state in ("searching", "offline", "searching"):
        theme.set_state(label, state)
        label.show()
        seen.append(QColor(label.grab().toImage().pixelColor(190, 18)).name().upper())
    assert seen == ["#F6E7C8", "#F5E1DA", "#F6E7C8"]


def test_dark_theme_pills_differ_from_light(themed: QApplication, qtbot: QtBot) -> None:
    theme.apply_theme(themed, "dark")
    assert _pill_pixel(qtbot, "searching") != LIGHT_PILLS["searching"]


def test_primary_button_is_honey_with_dark_text(
    themed: QApplication, qtbot: QtBot
) -> None:
    from PySide6.QtWidgets import QPushButton

    theme.apply_theme(themed, "light")
    button = QPushButton("")
    button.setObjectName("primary")
    button.setFixedSize(200, 46)
    qtbot.addWidget(button)
    button.show()
    pixel = QColor(button.grab().toImage().pixelColor(100, 23))
    assert pixel.name().upper() == "#E2A548"
