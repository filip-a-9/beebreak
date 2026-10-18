"""`beebreak-server migrate|serve|purge`. Owned by M3-01."""

import argparse
import os
import sys
from typing import Any

from beebreak_server.logging_config import build_log_config

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8000
APP_FACTORY = "beebreak_server.app:app_from_env"

# Text by default; BEEBREAK_LOG_FORMAT=json switches `serve` to JSON lines.
LOG_CONFIG: dict[str, Any] = build_log_config("text")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="beebreak-server")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("migrate", help="apply database migrations (alembic upgrade head)")
    sub.add_parser("purge", help="run one retention purge pass and exit")
    serve = sub.add_parser("serve", help="run the HTTP/WebSocket server")
    serve.add_argument("--host", default=DEFAULT_HOST)
    serve.add_argument("--port", type=int, default=DEFAULT_PORT)
    serve.add_argument(
        "--workers",
        type=int,
        default=1,
        help="must be 1: hub, presence, limiters and matchmaking are in-process",
    )
    return parser


def _env_workers() -> int:
    """uvicorn also reads WEB_CONCURRENCY; treat anything but 1 as a request."""
    raw = os.environ.get("WEB_CONCURRENCY")
    if raw is None:
        return 1
    try:
        return int(raw)
    except ValueError:
        return 0


class _NoSockets:
    """Notifier for a one-off process: nobody is connected."""

    def send_to_user(self, user_id: Any, frame: Any) -> None:
        return None

    def is_user_connected(self, user_id: Any) -> bool:
        return False


def _purge() -> int:
    """One matchmaker pass: expire, end, purge. Used after a restore."""
    import logging.config
    from datetime import UTC, datetime

    from sqlalchemy.orm import sessionmaker

    from beebreak_server.config import ServerConfig
    from beebreak_server.db import make_engine
    from beebreak_server.matchmaking import Matchmaker

    try:
        config = ServerConfig.from_env()
    except ValueError as error:  # messages name the variable, never its value
        print(error, file=sys.stderr)
        return 2
    logging.config.dictConfig(build_log_config(config.log_format))
    engine = make_engine(config.database_url)
    try:
        factory = sessionmaker(engine, expire_on_commit=False)
        Matchmaker(factory, config, lambda: datetime.now(UTC), _NoSockets()).run_once()
    except Exception:
        # The driver's message may carry the URL, so print a fixed string.
        print("purge failed", file=sys.stderr)
        return 1
    finally:
        engine.dispose()
    return 0


def main(argv: list[str] | None = None) -> int:
    try:
        args = _parser().parse_args(argv)
    except SystemExit as exit_:
        return exit_.code if isinstance(exit_.code, int) else 2

    if args.command == "migrate":
        from beebreak_server.config import reject_weak_database_url
        from beebreak_server.db import upgrade_head

        url = os.environ.get("BEEBREAK_DATABASE_URL")
        if not url:
            print("BEEBREAK_DATABASE_URL is required", file=sys.stderr)
            return 2
        try:
            reject_weak_database_url(url)
        except ValueError as error:  # the message never carries the URL
            print(error, file=sys.stderr)
            return 2
        try:
            upgrade_head(url)
        except Exception:
            # The driver's message may carry the URL, so print a fixed string.
            print("migration failed", file=sys.stderr)
            return 1
        return 0

    if args.command == "purge":
        return _purge()

    if args.workers != 1 or _env_workers() != 1:
        print(
            "serve runs exactly one worker: the realtime hub, presence, rate "
            "limiters and the matchmaker lock live in this process",
            file=sys.stderr,
        )
        return 2

    from beebreak_server.config import ServerConfig

    try:
        config = ServerConfig.from_env()  # fail here, not inside uvicorn's factory
    except ValueError as error:  # messages name the variable, never its value
        print(error, file=sys.stderr)
        return 2

    import uvicorn

    proxies = config.trusted_proxies
    uvicorn.run(
        APP_FACTORY,
        factory=True,
        host=args.host,
        port=args.port,
        access_log=False,
        log_config=build_log_config(config.log_format),
        # A forwarded address lets its sender pick the limiter key, so it is
        # honoured only from the configured proxy peers; none by default.
        proxy_headers=bool(proxies),
        forwarded_allow_ips=",".join(proxies) if proxies else None,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
