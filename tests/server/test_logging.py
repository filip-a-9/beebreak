"""M6-08 logging: JSON lines, no user content, config switch."""

import base64
import io
import json
import logging
import sys
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from urllib.parse import quote

import pytest
from fastapi.testclient import TestClient

from breakroom_server.config import ServerConfig
from breakroom_server.db import SessionFactory
from breakroom_server.logging_config import (
    JsonFormatter,
    build_log_config,
    request_id_var,
)
from breakroom_server.mailer import RecordingMailer
from tests.fixtures.privacy_corpus import load_corpus
from tests.fixtures.privacy_corpus.scan import find_corpus_leaks
from tests.server.conftest import (
    TEST_SECRET,
    ApiUser,
    ready_user,
    seed_match,
)
from tests.server.test_serve_process import Served, _env, _request, _start

ENV_SECRET = base64.urlsafe_b64encode(TEST_SECRET).decode()


@pytest.fixture
def server_config(database_url: str) -> ServerConfig:
    """Limits high enough that every corpus value reaches the handlers (the
    fixed test clock never refills a limiter window)."""
    return ServerConfig(
        database_url=database_url,
        secret=TEST_SECRET,
        rate_start_email=10_000,
        rate_start_ip=10_000,
        rate_verify_ip=10_000,
        rate_message_user=10_000,
        rate_report_user=10_000,
    )


@pytest.fixture
def captured() -> Iterator[io.StringIO]:
    """Everything the app's loggers write, through the production formatter."""
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(JsonFormatter())
    logger = logging.getLogger("breakroom_server")
    old_level = logger.level
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    try:
        yield stream
    finally:
        logger.removeHandler(handler)
        logger.setLevel(old_level)


def _lines(stream: io.StringIO) -> list[dict[str, Any]]:
    return [json.loads(line) for line in stream.getvalue().splitlines() if line]


def test_a_line_has_the_fixed_keys_and_is_one_json_object() -> None:
    record = logging.LogRecord("x.y", logging.WARNING, "f", 1, "hi %d", (3,), None)
    line = JsonFormatter().format(record)
    assert "\n" not in line
    data = json.loads(line)
    assert set(data) == {"timestamp", "level", "logger", "message"}
    assert data["level"] == "WARNING"
    assert data["logger"] == "x.y"
    assert data["message"] == "hi 3"
    assert data["timestamp"].endswith("+00:00")


def test_request_id_appears_when_known_and_never_otherwise() -> None:
    record = logging.LogRecord("x", logging.INFO, "f", 1, "m", (), None)
    assert "request_id" not in json.loads(JsonFormatter().format(record))
    token = request_id_var.set("req-123")
    try:
        assert json.loads(JsonFormatter().format(record))["request_id"] == "req-123"
    finally:
        request_id_var.reset(token)


def test_an_exception_never_reaches_the_line() -> None:
    try:
        raise RuntimeError("secret-in-exception-text")
    except RuntimeError:
        record = logging.LogRecord("x", logging.ERROR, "f", 1, "m", (), sys.exc_info())
    assert "secret-in-exception-text" not in JsonFormatter().format(record)


def _raise_and_format() -> dict[str, Any]:
    try:
        raise KeyError("secret-in-exception-text")
    except KeyError:
        record = logging.LogRecord("x", logging.ERROR, "f", 1, "m", (), sys.exc_info())
    parsed: dict[str, Any] = json.loads(JsonFormatter().format(record))
    return parsed


def test_an_exception_line_names_the_type_and_the_frames() -> None:
    data = _raise_and_format()
    assert data["exc_type"] == "KeyError"
    assert any(
        f.endswith(":_raise_and_format") and "test_logging.py" in f
        for f in data["frames"]
    )


def test_the_json_config_resolves_to_the_json_formatter() -> None:
    spec = build_log_config("json")["formatters"]["default"]["()"]
    module, _, name = spec.rpartition(".")
    imported = __import__(module, fromlist=[name])
    assert getattr(imported, name) is JsonFormatter
    assert "()" not in build_log_config("text")["formatters"]["default"]


def test_unknown_format_is_rejected() -> None:
    with pytest.raises(ValueError):
        build_log_config("xml")


def test_log_format_defaults_to_text_and_reads_the_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("BREAKROOM_DATABASE_URL", "sqlite://")
    monkeypatch.setenv("BREAKROOM_SERVER_SECRET", ENV_SECRET)
    assert ServerConfig.from_env().log_format == "text"
    monkeypatch.setenv("BREAKROOM_LOG_FORMAT", "json")
    assert ServerConfig.from_env().log_format == "json"
    monkeypatch.setenv("BREAKROOM_LOG_FORMAT", "yaml")
    with pytest.raises(ValueError):
        ServerConfig.from_env()


def test_an_access_line_carries_a_fresh_request_id_per_request(
    client: TestClient, captured: io.StringIO
) -> None:
    client.get("/v1/health")
    client.get("/v1/health")
    ids = [
        line["request_id"]
        for line in _lines(captured)
        if line["logger"] == "breakroom_server.access"
    ]
    assert len(ids) == 2 and ids[0] != ids[1]
    assert all(len(i) == 32 and int(i, 16) >= 0 for i in ids)


def test_the_scanner_sees_a_corpus_value_in_captured_json(
    captured: io.StringIO,
) -> None:
    value = load_corpus().bases[0]
    logging.getLogger("breakroom_server.probe").info("leak %s", value.value)
    hits = find_corpus_leaks({"log": [captured.getvalue().encode()]}, [value])
    assert hits


def test_no_corpus_value_reaches_the_log_through_real_requests(
    client: TestClient,
    session_factory: SessionFactory,
    mailer: RecordingMailer,
    captured: io.StringIO,
) -> None:
    ada = ready_user(client, session_factory, mailer, "ada@example.com", "Ada")
    bob = ready_user(client, session_factory, mailer, "bob@example.com", "Bob")
    match_id = seed_match(session_factory, ada, bob)
    corpus = load_corpus()

    for item in corpus.values:
        _send_everywhere(client, ada, match_id, item.value)

    # Well-formed sign-ins whose device name is the corpus value, so the request
    # gets past validation and reaches the handler.
    reached = [_sign_in_status(client, mailer, item.value) for item in corpus.bases]
    assert 200 in reached

    lines = _lines(captured)
    assert any(line["logger"] == "breakroom_server.access" for line in lines)
    leaks = find_corpus_leaks({"log": [captured.getvalue().encode()]}, corpus.values)
    assert leaks == []


def _sign_in_status(client: TestClient, mailer: RecordingMailer, device: str) -> int:
    mailer.sent.clear()
    started = client.post("/v1/auth/start", json={"email": "ada@example.com"})
    assert started.status_code == 202
    (_, _, code) = mailer.sent[-1]
    return client.post(
        "/v1/auth/verify",
        json={
            "challenge_id": started.json()["challenge_id"],
            "code": code,
            "device_name": device,
        },
    ).status_code


def _send_everywhere(
    client: TestClient, user: ApiUser, match_id: uuid.UUID, value: str
) -> None:
    """The value in a query string, a path, and every user-content field."""
    quoted = quote(value, safe="")
    client.get(f"/v1/me?q={quoted}&access_token={quoted}", headers=user.headers)
    client.get(f"/v1/{quoted}/{quoted}")
    client.post("/v1/auth/start", json={"email": value, "invite_code": value})
    client.post("/v1/auth/verify", json={"challenge_id": value, "code": value})
    client.patch("/v1/me", json={"nickname": value}, headers=user.headers)
    client.post(
        f"/v1/matches/{match_id}/messages",
        json={"client_message_id": str(uuid.uuid4()), "body": value},
        headers=user.headers,
    )
    client.post(
        "/v1/reports",
        json={
            "match_id": str(match_id),
            "reason": "harassment",
            "note": value,
            "message_seqs": [],
        },
        headers=user.headers,
    )


@pytest.fixture
def served_json(tmp_path: Path) -> Iterator[Served]:
    yield from _start(_env(tmp_path, {"BREAKROOM_LOG_FORMAT": "json"}))


def test_serve_writes_json_lines_without_the_query_string(
    served_json: Served,
) -> None:
    assert _request(f"{served_json.http}/v1/me?access_token=bra_SECRETx") == 401
    stderr = served_json.stop()
    lines = [json.loads(line) for line in stderr.splitlines() if line]
    access = [line for line in lines if line["logger"] == "breakroom_server.access"]
    assert access, stderr
    assert {"timestamp", "level", "logger", "message"} <= set(access[0])
    assert "SECRETx" not in stderr and "access_token" not in stderr
