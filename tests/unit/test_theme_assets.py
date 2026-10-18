"""The bundled font and the mark, read the way an installed wheel serves them."""

from importlib import resources
from pathlib import Path

import pytest
from PySide6.QtGui import QColor, QFontDatabase
from pytestqt.qtbot import QtBot

from beebreak_companion import theme

STATIC_FONTS = {
    "Nunito-Regular.ttf",
    "Nunito-SemiBold.ttf",
    "Nunito-Bold.ttf",
    "Nunito-ExtraBold.ttf",
}


def test_font_files_resolve_through_importlib_resources() -> None:
    folder = resources.files("beebreak_companion.assets.fonts")
    names = {p.name for p in folder.iterdir()}
    assert names >= STATIC_FONTS
    assert "OFL.txt" in names
    assert not any("wght" in n for n in names)  # the variable font is not bundled
    for name in STATIC_FONTS:
        assert len(folder.joinpath(name).read_bytes()) > 10_000


def test_mark_is_one_file_in_the_package() -> None:
    svg = resources.files("beebreak_companion.assets").joinpath("mark.svg")
    assert svg.read_text().lstrip().startswith("<svg")


def test_loading_registers_the_four_weights(qtbot: QtBot) -> None:
    assert theme.load_fonts() is True
    styles = set(QFontDatabase.styles("Nunito"))
    assert {"Regular", "SemiBold", "Bold", "ExtraBold"} <= styles


def test_missing_font_folder_falls_back_without_raising(
    qtbot: QtBot, tmp_path: Path
) -> None:
    assert theme.load_fonts(tmp_path) is False


def test_corrupt_font_file_falls_back_without_raising(
    qtbot: QtBot, tmp_path: Path
) -> None:
    (tmp_path / "Nunito-Regular.ttf").write_bytes(b"not a font")
    assert theme.load_fonts(tmp_path) is False


def _pixel(scheme: str, x: int, y: int) -> QColor:
    image = theme.mark_pixmap(scheme, 120).toImage()  # type: ignore[arg-type]
    return QColor(image.pixelColor(x, y))


def test_light_mark_uses_the_light_colours(qtbot: QtBot) -> None:
    assert _pixel("light", 35, 35).name().upper() == "#2A221B"  # upper bubble
    assert _pixel("light", 30, 95).name().upper() == "#E2A548"  # lower bubble


def test_dark_mark_recolours_only_the_upper_bubble(qtbot: QtBot) -> None:
    assert _pixel("dark", 35, 35).name().upper() == "#F1E8DC"
    assert _pixel("dark", 30, 95).name().upper() == "#E2A548"


def test_typing_dots_are_holes(qtbot: QtBot) -> None:
    assert _pixel("light", 58, 84).alpha() == 0
    assert _pixel("light", 58, 95).alpha() == 255


def test_mark_icon_is_not_empty(qtbot: QtBot) -> None:
    assert not theme.mark_icon("light").isNull()


@pytest.mark.parametrize("size", [22, 64])
def test_mark_pixmap_has_the_requested_size(qtbot: QtBot, size: int) -> None:
    assert theme.mark_pixmap("light", size).width() == size
