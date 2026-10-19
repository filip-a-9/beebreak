# Beebreak

(Formerly Breakroom; renamed for a trademark conflict, see
[ADR 0019](docs/ADR/0019-rename-to-beebreak.md).)

A voluntary, spontaneous one-to-one chat break while AI coding agents work.

Developers with coding agents often have short intervals when the agent runs
on its own. Beebreak detects a likely interval, offers an optional social
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
server is running yet. The companion bundles the Nunito font (Copyright 2014
The Nunito Project Authors) under the SIL Open Font License 1.1, as static
weights cut from the upstream variable font; the licence text ships beside it
in `apps/companion/src/beebreak_companion/assets/fonts/OFL.txt`.

Supported platform: WSL2 only. Claude Code, the hooks, the MCP server and the
companion all run inside WSL2 (the companion's window uses WSLg).

Chat is not end-to-end encrypted. Whoever runs the server can read messages
while they exist. See [docs/DATA_HANDLING.md](docs/DATA_HANDLING.md) and the
draft [privacy notice](docs/PRIVACY_NOTICE.md).

## Install

WSL2 only. On a fresh Ubuntu distro, set up these first, in the same distro:

1. System packages, as one line (a WSL Ubuntu image already has some of them;
   `apt` skips what is installed):

   ```bash
   sudo apt-get update && sudo apt-get install -y --no-install-recommends curl ca-certificates libglib2.0-0t64 libegl1 libgl1 libxkbcommon0 libfontconfig1 libdbus-1-3 libxcb-cursor0 libxkbcommon-x11-0 libxcb-icccm4 libxcb-keysyms1 libxcb-shape0 libxcb-xkb1
   ```

2. [uv](https://docs.astral.sh/uv/getting-started/installation/) and
   [Claude Code](https://docs.claude.com/en/docs/claude-code/setup): follow
   each official install page.
3. `~/.local/bin` on your `PATH`. The uv installer usually adds it already;
   if `command -v uv` finds nothing, run `~/.local/bin/uv tool update-shell`.
   Then open a new terminal.

The companion supports Python 3.12 only, so the install below passes
`--python 3.12`. uv uses a `python3` 3.12 if the distro has one and downloads
one otherwise; without the flag it could pick a newer Python that does not work.

The repository is private, so testers install from a release bundle
(`beebreak-release.tar.gz`, built by `scripts/build_release.sh`). Compare the
tarball's SHA-256 with the one on the release page. The `SHA256SUMS` file inside
it only checks that the unpacked wheels were not corrupted; it is not tamper
protection.

Run the commands below from the directory that holds the tarball (a file in
`C:\Temp` on Windows is `/mnt/c/Temp` in WSL). The first line prints the
SHA-256 to compare:

```bash
sha256sum beebreak-release.tar.gz
mkdir -p ~/beebreak-release && tar xzf beebreak-release.tar.gz -C ~/beebreak-release
(cd ~/beebreak-release && sha256sum -c SHA256SUMS)

# The three wheels are named explicitly, so uv cannot substitute a package of
# the same name from PyPI. This also installs the beebreak-hook and
# beebreak-mcp commands the plugin calls.
R=~/beebreak-release
uv tool install --python 3.12 $R/beebreak_companion-*.whl \
  --with $R/beebreak_shared-*.whl \
  --with $R/beebreak_agent_bridge-*.whl \
  --with-executables-from beebreak-agent-bridge

beebreak-companion install        # asks for the server URL
claude plugin marketplace add ~/beebreak-release/marketplace
claude plugin install beebreak@beebreak
beebreak-companion                # start it (it reads the URL saved above)
```

A server with a private CA: add `--ca-file /path/to/ca.pem` to
`beebreak-companion install`. The file must be a PEM owned by you (or root)
that only its owner can write. It is recorded next to the server URL and
applied on every start unless `BEEBREAK_CA_FILE` is
already set. It is read once at start: restart the companion after changing it.

Start the companion by hand with `beebreak-companion`. If it stops with
`could not connect to display` (Qt then names `xcb` or `xcb-cursor0`), the
cause is usually not a missing library but no WSLg display: check that
`echo $DISPLAY` prints something like `:0`, and run `wsl --update` and
`wsl --shutdown` from Windows if it is empty. There is no login
autostart: WSLg has no desktop session, so nothing in WSL2 runs autostart
entries.

The companion follows the Windows light or dark app theme, read once at
start. Set `BEEBREAK_THEME=light` or `dark` to override it (default `auto`;
light when Windows can't be asked).

To check it works, start a Claude Code session, ask it to do anything, then
ask it to call the `beebreak_status` tool. `running` with a non-null
`last_event_at` means the hooks reach the companion; `not_running` means the
companion is not up; `last_event_at: null` means the hooks are not connected.
`ready: false` with a `not_ready_reason` (`signed_out`, `connecting`,
`offline`, `no_nickname` or `no_consent`) means no break is offered yet; the
companion window shows the same reason.

Without the `beebreak-hook` command on `PATH` the hooks do nothing and stay
silent (the `beebreak` MCP server shows as failed in `/mcp`).

Once the repository is public, the wheels step becomes
`uv tool install --python 3.12 --with-executables-from beebreak-agent-bridge "git+https://github.com/<owner>/<repo>#subdirectory=apps/companion"`
(untested until then) and the marketplace is `claude plugin marketplace add <owner>/<repo>`.

## Uninstall

```bash
beebreak-companion uninstall                # add --keep-account to keep the server login valid
claude plugin uninstall beebreak@beebreak
claude plugin marketplace remove beebreak
uv tool uninstall beebreak-companion
rm -rf ~/beebreak-release              # and the downloaded beebreak-release.tar.gz
```

Claude Code keeps a copy of uninstalled plugins in
`~/.claude/plugins/cache/beebreak/`; delete it too if you want no trace left.

`uninstall` stops a running companion, revokes this device on the server
(skipped with `--keep-account`; if the server is unreachable it says so and
continues), and removes the socket directory, state directory (state,
credentials file, install record), salt, keyring entry and any autostart
entry left by an older release. It prints what it removed and works on a
partial or already-removed install.

## Development quickstart

Requires Python 3.12+, [uv](https://docs.astral.sh/uv/) and Docker.

```bash
uv sync
cp .env.example .env     # then fill in the two secrets, see the file
docker compose up --build
docker compose exec server beebreak-admin invite create you@example.com
```

[compose.yaml](compose.yaml) starts Postgres, Mailpit (catches the sign-in
email at http://127.0.0.1:8025), a one-shot `migrate` service and then the
server. Every published port is bound to `127.0.0.1`. The server refuses to
start while the database password is still the example `change-me`.

Run the checks:

```bash
uv run ruff format --check . && uv run ruff check . && uv run mypy packages apps tests tools && uv run pytest
```

Postgres race and migration tests are skipped unless `BEEBREAK_TEST_DATABASE_URL`
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
container; never scale it. Backups, restore and operations are in `docs/RUNBOOK.md` (`scripts/backup.sh`, `scripts/restore.sh`).

## Links

- [Architecture and product decisions](docs/ARCHITECTURE.md)
- [Threat model](docs/THREAT_MODEL.md)
- [Data handling](docs/DATA_HANDLING.md)
- [Privacy notice (draft)](docs/PRIVACY_NOTICE.md)
- [Architecture decision records](docs/ADR/README.md)
- [Contributing](CONTRIBUTING.md)
- [Security policy](SECURITY.md)
