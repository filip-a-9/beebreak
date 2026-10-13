"""Migration checks.

The SQLite tests always run. The PostgreSQL test needs
BREAKROOM_TEST_DATABASE_URL pointing at a throwaway database; it drops the
app's tables first.
"""

from pathlib import Path

import pytest
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import inspect, text

from breakroom_server.cli import main as cli_main
from breakroom_server.db import alembic_config, make_engine, upgrade_head
from breakroom_server.models import Base
from tests.server.conftest import TEST_DATABASE_URL, requires_postgres

TABLES = {
    "users",
    "beta_invites",
    "auth_challenges",
    "devices",
    "refresh_tokens",
    "user_preferences",
}


def _diff(url: str) -> list[object]:
    engine = make_engine(url)
    try:
        with engine.connect() as conn:
            ctx = MigrationContext.configure(conn, opts={"compare_type": True})
            return list(compare_metadata(ctx, Base.metadata))
    finally:
        engine.dispose()


def test_single_head_and_models_match_migration_on_sqlite(tmp_path: Path) -> None:
    url = f"sqlite:///{tmp_path / 'm.db'}"
    heads = ScriptDirectory.from_config(alembic_config(url)).get_heads()
    assert len(heads) == 1

    upgrade_head(url)
    engine = make_engine(url)
    assert set(inspect(engine).get_table_names()) >= TABLES
    engine.dispose()
    assert _diff(url) == []


def test_cli_migrate_uses_database_url_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    url = f"sqlite:///{tmp_path / 'cli.db'}"
    monkeypatch.setenv("BREAKROOM_DATABASE_URL", url)
    assert cli_main(["migrate"]) == 0
    engine = make_engine(url)
    assert set(inspect(engine).get_table_names()) >= TABLES
    engine.dispose()
    assert cli_main(["migrate"]) == 0  # idempotent


def test_cli_migrate_without_url_exits_2(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv("BREAKROOM_DATABASE_URL", raising=False)
    assert cli_main(["migrate"]) == 2
    assert capsys.readouterr().out == ""


def test_cli_migrate_failure_does_not_print_the_url(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    url = f"sqlite:///{tmp_path / 'missing-dir' / 'x.db'}"
    monkeypatch.setenv("BREAKROOM_DATABASE_URL", url)
    assert cli_main(["migrate"]) == 1
    captured = capsys.readouterr()
    assert "missing-dir" not in captured.out + captured.err


def test_serve_runs_uvicorn_factory_on_loopback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[tuple[object, ...], dict[str, object]]] = []
    import uvicorn

    monkeypatch.setattr(uvicorn, "run", lambda *a, **k: calls.append((a, k)))
    assert cli_main(["serve"]) == 0
    assert calls == [
        (
            ("breakroom_server.app:app_from_env",),
            {"factory": True, "host": "127.0.0.1", "port": 8000, "access_log": False},
        )
    ]


@requires_postgres
def test_postgres_upgrade_matches_models_and_downgrades() -> None:
    assert TEST_DATABASE_URL is not None
    engine = make_engine(TEST_DATABASE_URL)
    Base.metadata.drop_all(engine)
    with engine.begin() as conn:
        conn.execute(text("DROP TABLE IF EXISTS alembic_version"))
    try:
        upgrade_head(TEST_DATABASE_URL)
        assert set(inspect(engine).get_table_names()) >= TABLES
        assert _diff(TEST_DATABASE_URL) == []

        # Real timestamptz columns, not naive timestamps.
        with engine.connect() as conn:
            kinds = {
                row[0]
                for row in conn.execute(
                    text(
                        "SELECT data_type FROM information_schema.columns "
                        "WHERE table_name = 'users' AND column_name = 'created_at'"
                    )
                )
            }
        assert kinds == {"timestamp with time zone"}

        from alembic import command

        command.downgrade(alembic_config(TEST_DATABASE_URL), "base")
        assert not TABLES & set(inspect(engine).get_table_names())
    finally:
        Base.metadata.drop_all(engine)
        with engine.begin() as conn:
            conn.execute(text("DROP TABLE IF EXISTS alembic_version"))
        engine.dispose()
