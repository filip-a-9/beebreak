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

Coming in M6: install and uninstall instructions for the plugin and the
companion.

## Development quickstart

Requires Python 3.12+, [uv](https://docs.astral.sh/uv/) and Docker.

```bash
uv sync
cp .env.example .env     # then fill in the two secrets, see the file
docker compose up --build
docker compose exec server breakroom-admin invite create you@example.com
```

[compose.yaml](compose.yaml) starts Postgres, Mailpit (catches the sign-in
email at http://127.0.0.1:8025) and the server. Every published port is bound
to `127.0.0.1`.

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

Coming in M6: deployment and operations (production compose file, backups,
runbook).

## Links

- [Architecture and product decisions](docs/ARCHITECTURE.md)
- [Threat model](docs/THREAT_MODEL.md)
- [Data handling](docs/DATA_HANDLING.md)
- [Privacy notice (draft)](docs/PRIVACY_NOTICE.md)
- [Architecture decision records](docs/ADR/README.md)
- [Contributing](CONTRIBUTING.md)
- [Security policy](SECURITY.md)
