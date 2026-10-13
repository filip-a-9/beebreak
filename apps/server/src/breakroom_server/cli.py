"""`breakroom-server migrate|serve`. Owned by M3-01."""

import argparse
import os
import sys
from typing import Any

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8000
APP_FACTORY = "breakroom_server.app:app_from_env"

# uvicorn logs client addresses and WebSocket query strings at INFO, so its
# loggers stay at WARNING; the app writes its own access lines (breakroom_server).
LOG_CONFIG: dict[str, Any] = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {
        "plain": {"format": "%(asctime)s %(levelname)s %(name)s %(message)s"},
    },
    "handlers": {
        "stderr": {
            "class": "logging.StreamHandler",
            "formatter": "plain",
            "stream": "ext://sys.stderr",
        },
    },
    "loggers": {
        "uvicorn": {"handlers": ["stderr"], "level": "WARNING", "propagate": False},
        "uvicorn.error": {"level": "WARNING"},
        "uvicorn.access": {"handlers": [], "level": "CRITICAL", "propagate": False},
        "breakroom_server": {
            "handlers": ["stderr"],
            "level": "INFO",
            "propagate": False,
        },
    },
}


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="breakroom-server")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("migrate", help="apply database migrations (alembic upgrade head)")
    serve = sub.add_parser("serve", help="run the HTTP/WebSocket server")
    serve.add_argument("--host", default=DEFAULT_HOST)
    serve.add_argument("--port", type=int, default=DEFAULT_PORT)
    return parser


def main(argv: list[str] | None = None) -> int:
    try:
        args = _parser().parse_args(argv)
    except SystemExit as exit_:
        return exit_.code if isinstance(exit_.code, int) else 2

    if args.command == "migrate":
        from breakroom_server.db import upgrade_head

        url = os.environ.get("BREAKROOM_DATABASE_URL")
        if not url:
            print("BREAKROOM_DATABASE_URL is required", file=sys.stderr)
            return 2
        try:
            upgrade_head(url)
        except Exception:
            # The driver's message may carry the URL, so print a fixed string.
            print("migration failed", file=sys.stderr)
            return 1
        return 0

    import uvicorn

    uvicorn.run(
        APP_FACTORY,
        factory=True,
        host=args.host,
        port=args.port,
        access_log=False,
        log_config=LOG_CONFIG,
        # No proxy in M3: a forwarded address would let a client pick its limiter key.
        proxy_headers=False,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
