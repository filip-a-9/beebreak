"""`beebreak-companion` command line: run (default), install, uninstall. M6-02.

Install and uninstall import no Qt, so they work without a display.
"""

import argparse
import os
import sys

from beebreak_companion.endpoint import EndpointRefused, validate_server_url
from beebreak_companion.install import (
    CaFileRefused,
    install,
    read_saved_ca_file,
    read_saved_server_url,
    uninstall,
    validate_ca_file,
)
from beebreak_companion.remote_config import (
    CA_FILE_ENV,
    DEFAULT_SERVER_URL,
    SERVER_URL_ENV,
)

PLUGIN_COMMANDS = (
    "claude plugin marketplace add <path to the unpacked release>/marketplace",
    "claude plugin install beebreak@beebreak",
)
PROMPT_ATTEMPTS = 3
EXIT_USAGE = 2
EXIT_INCOMPLETE = 1


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="beebreak-companion")
    sub = parser.add_subparsers(dest="command")
    sub.add_parser("run", help="start the companion (default)")
    inst = sub.add_parser("install", help="set up the companion for this user")
    inst.add_argument("--server-url", help="skip the prompt")
    inst.add_argument("--ca-file", help="PEM CA file for a private server CA")
    uninst = sub.add_parser("uninstall", help="remove everything Beebreak created")
    uninst.add_argument(
        "--keep-account",
        action="store_true",
        help="do not revoke this device on the server",
    )
    return parser


def _ask_server_url() -> str | None:
    for _ in range(PROMPT_ATTEMPTS):
        try:
            raw = input(f"Server URL [{DEFAULT_SERVER_URL}]: ").strip()
        except EOFError:
            raw = ""
        try:
            return validate_server_url(raw or DEFAULT_SERVER_URL)
        except EndpointRefused:
            print("That server URL is not allowed (https, or http on loopback).")
    return None


def _install(server_url: str | None, ca_file: str | None) -> int:
    if ca_file is not None:
        try:
            ca_file = validate_ca_file(ca_file)
        except CaFileRefused:
            print("That CA file is not allowed (a PEM file only you can write).")
            return EXIT_USAGE
    if server_url is None:
        url = _ask_server_url()
        if url is None:
            return EXIT_USAGE
    else:
        try:
            url = validate_server_url(server_url)
        except EndpointRefused:
            print("That server URL is not allowed (https, or http on loopback).")
            return EXIT_USAGE
    for line in install(url, ca_file=ca_file):
        print(line)
    print("Next, add the Claude Code plugin:")
    for command in PLUGIN_COMMANDS:
        print(f"  {command}")
    return 0


def apply_saved_settings() -> None:
    """Recorded server URL and CA file apply unless the environment sets them."""
    if SERVER_URL_ENV not in os.environ:
        url = read_saved_server_url()
        if url is not None:
            os.environ[SERVER_URL_ENV] = url
    if CA_FILE_ENV not in os.environ:
        ca_file = read_saved_ca_file()
        if ca_file is not None:
            os.environ[CA_FILE_ENV] = ca_file


def _run() -> int:
    apply_saved_settings()
    from beebreak_companion.app import main as run_companion

    return run_companion()


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.command == "install":
        return _install(args.server_url, args.ca_file)
    if args.command == "uninstall":
        apply_saved_settings()  # the revoke must trust the same CA as `run`
        report = uninstall(keep_account=args.keep_account)
        for line in report.lines:
            print(line)
        return 0 if report.completed else EXIT_INCOMPLETE
    return _run()


if __name__ == "__main__":
    sys.exit(main())
