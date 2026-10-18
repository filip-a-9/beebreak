"""Every Compose override a manual checklist writes out must start the server.

M5-C16 once shortened only the message retention, and the server refused to
start (the report window may not outlast it). This feeds each override's
`environment:` through the real `ServerConfig.from_env` validation.
"""

import base64
import os
import re
from pathlib import Path

import pytest

from beebreak_server.config import ServerConfig

DOCS = Path(__file__).parents[2] / "docs"
SECRET = base64.urlsafe_b64encode(b"s" * 48).decode().rstrip("=")
# `cat > reports/compose.NAME.yaml <<'EOF'` ... `EOF`
HEREDOC = re.compile(
    r"cat > (reports/compose\.[\w.-]+\.yaml) <<'EOF'\n(.*?)\nEOF\n", re.S
)
ENV_LINE = re.compile(r'^\s+(BEEBREAK_\w+):\s*"([^"]*)"\s*$', re.M)


def overrides() -> list[tuple[str, str]]:
    """(doc:file, heredoc body) for every override the checklists write."""
    found: list[tuple[str, str]] = []
    for doc in sorted(DOCS.glob("*_CHECKLIST.md")):
        for name, body in HEREDOC.findall(doc.read_text()):
            found.append((f"{doc.name}:{name}", body))
    return found


def test_the_checklists_still_write_overrides() -> None:
    # Guards the parser: a renamed heredoc would otherwise test nothing.
    assert len(overrides()) >= 2


@pytest.mark.parametrize(
    "body", [pytest.param(body, id=where) for where, body in overrides()]
)
def test_each_checklist_override_passes_server_config_validation(
    monkeypatch: pytest.MonkeyPatch, body: str
) -> None:
    env = dict(ENV_LINE.findall(body))
    assert env, "no BEEBREAK_* variables found"
    for name in [n for n in os.environ if n.startswith("BEEBREAK_")]:
        monkeypatch.delenv(name)
    monkeypatch.setenv("BEEBREAK_DATABASE_URL", "sqlite://")
    monkeypatch.setenv("BEEBREAK_SERVER_SECRET", SECRET)
    for name, value in env.items():
        monkeypatch.setenv(name, value)

    ServerConfig.from_env()
