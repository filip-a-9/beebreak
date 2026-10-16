# Breakroom

A voluntary, spontaneous one-to-one chat break while AI coding agents work.

Developers with coding agents often have short intervals when the agent runs
on its own. Breakroom detects a likely interval, offers an optional social
break, and matches two consenting developers for a short one-to-one
conversation. It should feel like a coffee break at work, not a meeting
planner, social network or agent-to-agent discussion tool. Entry is always
opt-in.

Non-goals (from [ARCHITECTURE.md](docs/ARCHITECTURE.md) §1): group chats,
audio or video, public timelines, reactions or leaderboards; precise
completion-time predictions; uploading repository contents, prompts, plan text,
shell commands or file paths; a hard dependency on local model weights; agent-to-agent
conversation; a commercial subscription system.

## Status

Pre-beta, invite-only hosted network. The source is Apache-2.0 (see
[LICENSE](LICENSE)); access to the network is a separate invite. No hosted
server is running yet.

Supported platform: WSL2 only. Claude Code, the hooks, the MCP server and the
companion all run inside WSL2 (the companion's window uses WSLg).

Chat is not end-to-end encrypted. Whoever runs the server can read messages
while they exist. See [docs/DATA_HANDLING.md](docs/DATA_HANDLING.md) and the
draft [privacy notice](docs/PRIVACY_NOTICE.md).

## Install

WSL2 only. Needs [uv](https://docs.astral.sh/uv/) and Claude Code in the same
WSL distro, and `~/.local/bin` on your `PATH` (`uv tool update-shell` adds it).

The repository is private, so testers install from a release bundle
(`breakroom-release.tar.gz`, built by `scripts/build_release.sh`). Compare the
tarball's SHA-256 with the one on the release page. The `SHA256SUMS` file inside
it only checks that the unpacked wheels were not corrupted; it is not tamper
protection.

```bash
mkdir -p ~/breakroom-release && tar xzf breakroom-release.tar.gz -C ~/breakroom-release
(cd ~/breakroom-release && sha256sum -c SHA256SUMS)

# The three wheels are named explicitly, so uv cannot substitute a package of
# the same name from PyPI. This also installs the breakroom-hook and
# breakroom-mcp commands the plugin calls.
R=~/breakroom-release
uv tool install $R/breakroom_companion-*.whl \
  --with $R/breakroom_shared-*.whl \
  --with $R/breakroom_agent_bridge-*.whl \
  --with-executables-from breakroom-agent-bridge

breakroom-companion install        # asks for the server URL
claude plugin marketplace add ~/breakroom-release/marketplace
claude plugin install breakroom@breakroom
breakroom-companion                # start it (it reads the URL saved above)
```

A server with a private CA: add `--ca-file /path/to/ca.pem` to
`breakroom-companion install`. The file must be a PEM owned by you (or root)
that only its owner can write. It is recorded next to the server URL and
applied on every start, including autostart, unless `BREAKROOM_CA_FILE` is
already set. It is read once at start: restart the companion after changing it.

Optional: `breakroom-companion install --autostart` adds
`~/.config/autostart/breakroom.desktop`. WSL2 may not process autostart entries
at all (WSLg has no desktop session), so it may not start the companion at
login; this is checked in the M6-10 checklist. Start the companion by hand
otherwise.

Without the `breakroom-hook` command on `PATH` the hooks do nothing and stay
silent (the `breakroom` MCP server shows as failed in `/mcp`).

Once the repository is public, the wheels step becomes
`uv tool install --with-executables-from breakroom-agent-bridge "git+https://github.com/<owner>/<repo>#subdirectory=apps/companion"`
(untested until then) and the marketplace is `claude plugin marketplace add <owner>/<repo>`.

## Uninstall

```bash
breakroom-companion uninstall                # add --keep-account to keep the server login valid
claude plugin uninstall breakroom@breakroom
claude plugin marketplace remove breakroom
uv tool uninstall breakroom-companion
rm -rf ~/breakroom-release
```

`uninstall` stops a running companion, revokes this device on the server
(skipped with `--keep-account`; if the server is unreachable it says so and
continues), and removes the socket directory, state directory (state,
credentials file, install record), salt, keyring entry and autostart entry. It
prints what it removed and works on a partial or already-removed install.

## Development quickstart

Requires Python 3.12+, [uv](https://docs.astral.sh/uv/) and Docker.

```bash
uv sync
cp .env.example .env     # then fill in the two secrets, see the file
docker compose up --build
docker compose exec server breakroom-admin invite create you@example.com
```

[compose.yaml](compose.yaml) starts Postgres, Mailpit (catches the sign-in
email at http://127.0.0.1:8025), a one-shot `migrate` service and then the
server. Every published port is bound to `127.0.0.1`. The server refuses to
start while the database password is still the example `change-me`.

Run the checks:

```bash
uv run ruff format --check . && uv run ruff check . && uv run mypy packages apps tests tools && uv run pytest
```

Postgres race and migration tests are skipped unless `BREAKROOM_TEST_DATABASE_URL`
is set.

To try the Claude Code plugin from a checkout, see
[integrations/claude-code/README.md](integrations/claude-code/README.md).

## Layout

| Path | Contents |
|---|---|
| `packages/shared` | Contracts, IPC protocol, paths |
| `packages/agent_bridge` | Hook, MCP server, IPC client |
| `apps/companion` | Qt desktop app and local IPC server |
| `apps/server` | FastAPI service, matchmaking, admin CLI |
| `integrations/claude-code` | Claude Code plugin |
| `tools` | Development-only scripts, never deployed |
| `tests` | All tests |
| `docs` | Specification, threat model, data handling, ADRs |

## Operations

[compose.prod.yaml](compose.prod.yaml) runs the server behind Caddy (TLS on
80/443) on any Docker host: copy `.env.example` to `.env`, replace every
placeholder (the file refuses to start with one missing), then
`docker compose -f compose.prod.yaml up -d --build`. Run exactly one server
container; never scale it. Backups and a runbook are still to come in M6.

## Links

- [Architecture and product decisions](docs/ARCHITECTURE.md)
- [Threat model](docs/THREAT_MODEL.md)
- [Data handling](docs/DATA_HANDLING.md)
- [Privacy notice (draft)](docs/PRIVACY_NOTICE.md)
- [Architecture decision records](docs/ADR/README.md)
- [Contributing](CONTRIBUTING.md)
- [Security policy](SECURITY.md)
