"""Choosing light or dark: BEEBREAK_THEME plus Windows' setting via reg.exe.

reg.exe is faked at the process boundary with a script on PATH.
"""

import stat
from pathlib import Path

import pytest

from beebreak_companion import config, theme

LIGHT_OUT = (
    "\r\nHKEY_CURRENT_USER\\Software\\Microsoft\\Windows\\CurrentVersion\\Themes"
    "\\Personalize\r\n    AppsUseLightTheme    REG_DWORD    0x1\r\n\r\n"
)
DARK_OUT = LIGHT_OUT.replace("0x1", "0x0")


def _fake_reg(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, body: str, code: int = 0
) -> None:
    script = tmp_path / "reg.exe"
    script.write_text(f"#!/bin/sh\n{body}\nexit {code}\n")
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("PATH", f"{tmp_path}:/bin")


def _prints(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, text: str) -> None:
    payload = tmp_path / "out.txt"
    payload.write_bytes(text.encode())
    _fake_reg(tmp_path, monkeypatch, f"cat {payload}")


def test_dark_value_gives_dark(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _prints(tmp_path, monkeypatch, DARK_OUT)
    assert theme.resolve_scheme("auto") == "dark"


def test_light_value_gives_light(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _prints(tmp_path, monkeypatch, LIGHT_OUT)
    assert theme.resolve_scheme("auto") == "light"


@pytest.mark.parametrize("forced", ["light", "dark"])
def test_forced_scheme_ignores_windows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, forced: str
) -> None:
    other = DARK_OUT if forced == "light" else LIGHT_OUT
    _prints(tmp_path, monkeypatch, other)
    assert theme.resolve_scheme(forced) == forced


def test_missing_reg_exe_falls_back_to_light(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PATH", str(tmp_path))  # empty directory
    assert theme.resolve_scheme("auto") == "light"


def test_failing_reg_exe_falls_back_to_light(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    payload = tmp_path / "out.txt"
    payload.write_bytes(DARK_OUT.encode())
    _fake_reg(tmp_path, monkeypatch, f"cat {payload}", code=1)
    assert theme.resolve_scheme("auto") == "light"


@pytest.mark.parametrize("garbage", ["", "nonsense", "AppsUseLightTheme REG_DWORD 0x7"])
def test_unparseable_output_falls_back_to_light(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, garbage: str
) -> None:
    _prints(tmp_path, monkeypatch, garbage)
    assert theme.resolve_scheme("auto") == "light"


def test_slow_reg_exe_times_out_to_light(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _fake_reg(tmp_path, monkeypatch, "exec /bin/sleep 5")
    monkeypatch.setenv("BEEBREAK_THEME_PROBE_TIMEOUT_S", "0.3")
    assert theme.resolve_scheme("auto") == "light"


def test_reg_exe_is_asked_for_the_apps_theme_value(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    log = tmp_path / "args.txt"
    _fake_reg(tmp_path, monkeypatch, f'echo "$@" > {log}')
    theme.resolve_scheme("auto")
    args = log.read_text()
    assert "AppsUseLightTheme" in args
    assert "Themes\\Personalize" in args


def test_setting_defaults_to_auto() -> None:
    assert config.theme_setting() == "auto"


@pytest.mark.parametrize("value", ["light", "dark", "auto"])
def test_setting_reads_env(monkeypatch: pytest.MonkeyPatch, value: str) -> None:
    monkeypatch.setenv("BEEBREAK_THEME", value)
    assert config.theme_setting() == value


@pytest.mark.parametrize("bad", ["", "blue", "DARKish"])
def test_invalid_setting_falls_back_to_auto(
    monkeypatch: pytest.MonkeyPatch, bad: str
) -> None:
    monkeypatch.setenv("BEEBREAK_THEME", bad)
    assert config.theme_setting() == "auto"


def test_setting_ignores_case_and_spaces(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BEEBREAK_THEME", " Dark ")
    assert config.theme_setting() == "dark"


def test_probe_timeout_is_positive_with_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert config.theme_probe_timeout_s() > 0
    monkeypatch.setenv("BEEBREAK_THEME_PROBE_TIMEOUT_S", "-1")
    assert config.theme_probe_timeout_s() > 0
    monkeypatch.setenv("BEEBREAK_THEME_PROBE_TIMEOUT_S", "0.25")
    assert config.theme_probe_timeout_s() == 0.25
