"""Migration checks.

The SQLite tests always run. The PostgreSQL test needs
BREAKROOM_TEST_DATABASE_URL pointing at a throwaway database; it drops the
app's tables first.
"""

import uuid
from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import Engine, inspect, text
from sqlalchemy.orm import sessionmaker

from breakroom_server.cli import LOG_CONFIG
from breakroom_server.cli import main as cli_main
from breakroom_server.db import alembic_config, make_engine, upgrade_head
from breakroom_server.models import Base
from tests.server.conftest import (
    START_TIME,
    TEST_DATABASE_URL,
    add_ready_user,
    requires_postgres,
    seed_match,
)

TABLES = {
    "users",
    "beta_invites",
    "auth_challenges",
    "devices",
    "refresh_tokens",
    "user_preferences",
}
M4_TABLES = {"queue_entries", "matches", "match_participants", "blocks"}
M4_INDEXES = {
    "queue_entries": {
        "uq_queue_entries_user_queued",
        "ix_queue_entries_status_expires",
    },
    "match_participants": {"uq_match_participants_user_active"},
}
M5_TABLES = {"messages", "reports", "report_messages"}
M5_INDEXES = {"reports": {"ix_reports_status_created"}}
M5_COLUMNS = {
    "matches": {"extensions_used", "extension_requested_by", "last_seq"},
    "match_participants": {"offline_since", "messages_sent"},
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
    assert set(inspect(engine).get_table_names()) >= TABLES | M4_TABLES | M5_TABLES
    for table, names in (M4_INDEXES | M5_INDEXES).items():
        assert names <= {i["name"] for i in inspect(engine).get_indexes(table)}
    engine.dispose()
    assert _diff(url) == []


def _columns(engine: Engine, table: str) -> set[str]:
    return {c["name"] for c in inspect(engine).get_columns(table)}


def _check_0002_to_0003_round_trip(url: str) -> None:
    """Upgrade from 0002 with an active match, then down and up again."""
    from alembic import command

    config = alembic_config(url)
    command.upgrade(config, "0002")
    engine = make_engine(url)
    try:
        assert not M5_TABLES & set(inspect(engine).get_table_names())
        sessions = sessionmaker(engine, expire_on_commit=False)
        a = add_ready_user(sessions, "Ada")
        b = add_ready_user(sessions, "Bob")
        match_id = uuid.uuid4()
        matches = sa.table(
            "matches",
            sa.column("id", sa.Uuid()),
            sa.column("status", sa.String()),
            sa.column("created_at", sa.DateTime(timezone=True)),
            sa.column("ends_at", sa.DateTime(timezone=True)),
        )
        participants = sa.table(
            "match_participants",
            sa.column("match_id", sa.Uuid()),
            sa.column("user_id", sa.Uuid()),
            sa.column("active", sa.Boolean()),
        )
        with engine.begin() as conn:
            conn.execute(
                sa.insert(matches).values(
                    id=match_id,
                    status="active",
                    created_at=START_TIME,
                    ends_at=START_TIME,
                )
            )
            conn.execute(
                sa.insert(participants),
                [
                    {"match_id": match_id, "user_id": u.user_id, "active": True}
                    for u in (a, b)
                ],
            )

        command.upgrade(config, "head")
        assert _diff(url) == []
        with engine.connect() as conn:
            row = conn.execute(
                sa.text(
                    "SELECT status, end_reason, extensions_used, "
                    "extension_requested_by, last_seq FROM matches"
                )
            ).one()
            assert tuple(row) == ("active", None, 0, None, 0)
            sent = conn.execute(
                sa.text("SELECT messages_sent, offline_since FROM match_participants")
            ).all()
            assert [tuple(r) for r in sent] == [(0, None), (0, None)]
            assert (
                conn.execute(
                    sa.text("SELECT count(*) FROM match_participants WHERE active")
                ).scalar_one()
                == 2
            )

        # Matches that ended with the M5 reasons map to the nearest M4 reason.
        c = add_ready_user(sessions, "Cy")
        d = add_ready_user(sessions, "Di")
        e = add_ready_user(sessions, "Eve")
        f = add_ready_user(sessions, "Fay")
        for first, second, reason in ((c, d, "disconnected"), (e, f, "reported")):
            seed_match(
                sessions,
                first,
                second,
                status="ended",
                ended_at=START_TIME,
                end_reason=reason,
            )

        command.downgrade(config, "0002")
        tables = set(inspect(engine).get_table_names())
        assert tables >= TABLES | M4_TABLES and not M5_TABLES & tables
        for table, names in M5_COLUMNS.items():
            assert not names & _columns(engine, table)
        with engine.connect() as conn:
            reasons = sorted(
                r[0]
                for r in conn.execute(
                    sa.text(
                        "SELECT end_reason FROM matches WHERE end_reason IS NOT NULL"
                    )
                )
            )
            assert reasons == ["blocked", "ended"]
            active: int = conn.execute(
                sa.text("SELECT count(*) FROM matches WHERE status = 'active'")
            ).scalar_one()
            assert active == 1

        command.upgrade(config, "head")
        assert set(inspect(engine).get_table_names()) >= M5_TABLES
        for table, names in M5_COLUMNS.items():
            assert names <= _columns(engine, table)
        assert _diff(url) == []
    finally:
        engine.dispose()


def test_sqlite_upgrades_from_0002_keeps_active_match_and_round_trips(
    tmp_path: Path,
) -> None:
    _check_0002_to_0003_round_trip(f"sqlite:///{tmp_path / 'm5.db'}")


def test_sqlite_upgrades_from_0001_and_downgrades_back(tmp_path: Path) -> None:
    from alembic import command

    url = f"sqlite:///{tmp_path / 'step.db'}"
    config = alembic_config(url)
    command.upgrade(config, "0001")
    engine = make_engine(url)
    try:
        assert not M4_TABLES & set(inspect(engine).get_table_names())
        command.upgrade(config, "head")
        assert set(inspect(engine).get_table_names()) >= M4_TABLES
        command.downgrade(config, "0001")
        tables = set(inspect(engine).get_table_names())
        assert tables >= TABLES and not M4_TABLES & tables
    finally:
        engine.dispose()


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
            {
                "factory": True,
                "host": "127.0.0.1",
                "port": 8000,
                "access_log": False,
                "log_config": LOG_CONFIG,
                "proxy_headers": False,
            },
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
        assert set(inspect(engine).get_table_names()) >= (
            TABLES | M4_TABLES | M5_TABLES
        )
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

        config = alembic_config(TEST_DATABASE_URL)
        command.downgrade(config, "0001")
        tables = set(inspect(engine).get_table_names())
        assert tables >= TABLES and not (M4_TABLES | M5_TABLES) & tables
        command.upgrade(config, "head")
        assert set(inspect(engine).get_table_names()) >= M4_TABLES | M5_TABLES
        assert _diff(TEST_DATABASE_URL) == []

        command.downgrade(config, "base")
        left = set(inspect(engine).get_table_names())
        assert not (TABLES | M4_TABLES | M5_TABLES) & left
    finally:
        Base.metadata.drop_all(engine)
        with engine.begin() as conn:
            conn.execute(text("DROP TABLE IF EXISTS alembic_version"))
        engine.dispose()


@requires_postgres
def test_postgres_upgrades_from_0002_keeps_active_match_and_round_trips() -> None:
    assert TEST_DATABASE_URL is not None
    engine = make_engine(TEST_DATABASE_URL)
    Base.metadata.drop_all(engine)
    with engine.begin() as conn:
        conn.execute(text("DROP TABLE IF EXISTS alembic_version"))
    try:
        _check_0002_to_0003_round_trip(TEST_DATABASE_URL)
    finally:
        Base.metadata.drop_all(engine)
        with engine.begin() as conn:
            conn.execute(text("DROP TABLE IF EXISTS alembic_version"))
        engine.dispose()
