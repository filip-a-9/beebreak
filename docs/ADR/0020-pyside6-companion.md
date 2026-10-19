# ADR 0020: PySide6 for the companion

Status: Accepted (the maintainer, 2026-10-18).
Source: ARCHITECTURE §2, §16 item 1.

## Context
The companion is a small desktop window with notifications that runs next to
the coding agent. §2 chose PySide6 / Qt as the proposal ("larger packages and
platform packaging need validation") and §16 item 1 left the choice against a
lightweight webview or Tauri open until footprint, notification support and
packaging had been compared. The companion has since been built and run under
WSLg on PySide6 (`pyside6-essentials`, pinned in `apps/companion/pyproject.toml`).

## Decision
PySide6 for the companion.

## Alternatives
A webview or Tauri companion, as named in §16 item 1. Not built; no comparison
numbers were recorded.

## Consequences
Python end to end, sharing `packages/shared` with the hook and MCP server. The
wheels are large. Under WSLg there is no system tray, so the taskbar flash
(`QApplication.alert`) is the notice.

Licensing: Qt for Python is available under LGPL-3.0 (or GPL, or a commercial
licence). The companion uses it as an unmodified library from the PySide6
wheel, linked dynamically, so the project's own code stays under Apache-2.0
(ADR 0001). Anyone who redistributes the companion together with PySide6 must
meet the LGPL terms for that library: keep its notices and licence text, and
leave the user able to replace it with another build. Patching PySide6 or
bundling it into a single frozen binary would need a fresh look. Not legal
advice; a lawyer review could cover it and is not planned (ADR 0021).

## How it is tested
The companion tests with Qt offscreen in CI (ADR 0010) and the M6 checklist
under WSLg.

## Rollback
Replace the `apps/companion` UI layer; the IPC protocol and `packages/shared`
do not depend on Qt.
