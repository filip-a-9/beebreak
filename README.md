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

## There is no public server

Beebreak is open source and nothing more. **The maintainer runs no server and
collects no data.** There is no hosted network to join, and no demo instance.
To use Beebreak, you or someone you trust runs a server, and everyone who
wants to chat points their companion at it. Whoever runs a server is its
operator and is responsible for the data on it ([ADR 0021](docs/ADR/0021-no-hosted-instance.md)).

Chat is not end-to-end encrypted. The operator can read messages while they
exist. See [docs/DATA_HANDLING.md](docs/DATA_HANDLING.md) and the
[privacy notice template](docs/PRIVACY_NOTICE.md), which each operator fills in.

The source is Apache-2.0 (see [LICENSE](LICENSE)). The companion bundles the
Nunito font (Copyright 2014 The Nunito Project Authors) under the SIL Open Font
License 1.1, as static weights cut from the upstream variable font; the licence
text ships beside it in
`apps/companion/src/beebreak_companion/assets/fonts/OFL.txt`.

Beebreak is provided "AS IS", without warranties or conditions of any kind
(Apache-2.0, sections 7 and 8). Operators run their servers at their own risk.

Supported platform: WSL2 only. Claude Code, the hooks, the MCP server and the
companion all run inside WSL2 (the companion's window uses WSLg). Status:
pre-release.

## Run a server

Needs Docker with Docker Compose 2.24 or newer, on a host with a DNS name and
ports 80 and 443 open.

```bash
git clone --branch v0.1.0 https://github.com/filip-a-9/beebreak && cd beebreak
cp .env.example .env && chmod 600 .env   # replace every placeholder; see the runbook
docker compose -f compose.prod.yaml up -d --build
curl https://<your host>/v1/health       # {"status":"ok"}
```

[compose.prod.yaml](compose.prod.yaml) runs the server behind Caddy (TLS on
80/443). Compose refuses to start when a required value in `.env` is missing,
and the server refuses the example passwords. It does not check your host name
or mail settings: check those by hand. Use the same tag for the server and the
clients (the install below uses `v0.1.0` too). Run
exactly one server container; never scale it. Backups, restore, GitHub sign-in
and moderation are in [docs/RUNBOOK.md](docs/RUNBOOK.md).

Before you let other people in, read two lists in the runbook:
[Before you open your server to others](docs/RUNBOOK.md#before-you-open-your-server-to-others)
(the privacy notice and contact fields you fill in) and
[Untested on a real host](docs/RUNBOOK.md#untested-on-a-real-host) (what was
tested locally only).

### Getting an invite

Sign-in is by invitation. The operator creates it:

```bash
docker compose -f compose.prod.yaml exec server beebreak-admin invite create <email> --send
```

The command prints the one-time code once; `--send` also emails it. The
person enters the email and the code in the companion. This needs email
sign-in (SMTP) on the server.

Or the server uses GitHub sign-in instead: the operator registers their own
GitHub OAuth App with device flow enabled, and invites a person by GitHub
username with `docker compose -f compose.prod.yaml exec server beebreak-admin invite create --github <username>`. See
[GitHub sign-in](docs/RUNBOOK.md#github-sign-in-optional) in the runbook. If
you are the operator, invite yourself first. If you are not, ask whoever runs
your server.

## Install the plugin and the companion

You need an invite and the address of the server you were invited to. Install
on WSL2, in the distro where you run Claude Code. On a fresh Ubuntu distro, set
up these first:

1. System packages, as one line (a WSL Ubuntu image already has some of them;
   `apt` skips what is installed):

   ```bash
   sudo apt-get update && sudo apt-get install -y --no-install-recommends curl ca-certificates libglib2.0-0t64 libegl1 libgl1 libxkbcommon0 libfontconfig1 libdbus-1-3 libxcb-cursor0 libxkbcommon-x11-0 libxcb-icccm4 libxcb-keysyms1 libxcb-shape0 libxcb-xkb1 git
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

Both parts install from a git tag of this repository (`v0.1.0` below; use the
latest tag on the releases page):

```bash
V=v0.1.0
REPO=git+https://github.com/filip-a-9/beebreak@$V

# --with adds the agent bridge. --with-executables-from puts beebreak-hook and
# beebreak-mcp in ~/.local/bin, where the plugin looks for them. A custom
# UV_TOOL_BIN_DIR or XDG_BIN_HOME puts them elsewhere and breaks the plugin.
uv tool install --python 3.12 \
  --with "beebreak-agent-bridge @ $REPO#subdirectory=packages/agent_bridge" \
  --with-executables-from beebreak-agent-bridge \
  "$REPO#subdirectory=apps/companion"

beebreak-companion install        # asks for the server URL
claude plugin marketplace add filip-a-9/beebreak#$V
claude plugin install beebreak@beebreak
beebreak-companion                # start it (it reads the URL saved above)
```

`beebreak-companion install` asks for the URL of your server and prints the
URL it saved. Then the companion window asks you to sign in. The invite code
comes from the server operator, who creates it with `beebreak-admin invite
create` (see [First admin invite](docs/RUNBOOK.md#first-admin-invite) in the
runbook and "Getting an invite" above). If you do not have one, ask them.

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

With no tray under WSL2, a new match, or a new chat message while the chat window is not
focused raises a Windows toast with fixed text only (it names nobody and shows
no message, since Windows keeps toast history). Set `BEEBREAK_WINDOWS_TOAST=0`
to turn it off, or `BEEBREAK_POWERSHELL` to point at a different
`powershell.exe`.

Closing the last companion window quits the companion (it asks first while a
search or a chat is active). There is no tray icon, so to bring it back run
`beebreak-companion` again.

The companion follows the Windows light or dark app theme, read once at
start. Set `BEEBREAK_THEME=light` or `dark` to override it (default `auto`;
light when Windows can't be asked).

Without the `beebreak-hook` command on `PATH` the hooks do nothing and stay
silent (the `beebreak` MCP server shows as failed in `/mcp`).

### When a break is offered

The companion offers a break only while an agent session has been working on
its own and you are not needed. With the defaults, an offer appears when:

- a Claude Code run has been active for at least 60 seconds with at least two
  tool events, and no session is waiting for you;
- the remaining-time estimate is not `under_5m` (a run that is about to finish
  is not interrupted);
- you have turned break suggestions on, are signed in and ready (see
  `ready` below), and no cooldown is running.

Each agent run is offered at most once. If the run has open tasks and none
has finished yet, the companion waits up to 5 minutes for the first one, so
it can tell how long the run still has. An offer stays for 60 seconds and
then expires. A 15 minute cooldown starts when you answer an offer, when it
expires, or when a break ends; an offer withdrawn because a session needs you
starts none. A session counts as `unknown` after 3 minutes without events.
Because of these rules, a user can wait a few minutes of agent work before
the first offer.
The environment variables `BEEBREAK_ACTIVITY_THRESHOLD_S`,
`BEEBREAK_MIN_TOOL_EVENTS`, `BEEBREAK_COOLDOWN_S` and
`BEEBREAK_SUGGESTION_TIMEOUT_S` change these values (seconds, positive
integers), for example when you try Beebreak out.

While you search, the window shows how long the server keeps you in the
queue (90 seconds unless the operator changes `BEEBREAK_QUEUE_TTL_S`).

### Check it works

Start a Claude Code session, ask it to do anything, then ask it to call the
`beebreak_status` tool. It prints these fields:

| Field | Meaning |
|---|---|
| `companion` | `running` if the companion answered on its local socket, `not_running` if not. When it is `not_running`, every other field is `null`. |
| `invitation` | `disabled` (break suggestions are switched off), `idle` (on, nothing to offer now) or `suggested` (the companion is offering a break now). `null` when the companion is not running. |
| `last_event_at` | Time of the last hook event the companion received. `null` means no hook has reached the companion yet: the hooks are not connected. |
| `activity` | The combined state of your Claude Code sessions, from hook events only: `active` (working), `waiting_input` (waiting for you, which wins over the others), `idle` (turn stopped) or `unknown` (no recent events). `null` when the companion is not running. |
| `estimate` | A coarse bucket for the remaining agent time (`under_5m`, `5_10m`, `10_20m`, `20m_plus` or `unknown`), set only while `invitation` is `suggested`. Otherwise `null`. |
| `queue` | `none`, `searching` (looking for a break partner) or `chatting` (you are in a break chat; Claude cannot see or take part in it). `null` from an older companion or when it is not running. |
| `ready` | `true` when the companion can offer a break, `false` when it cannot. `null` from an older companion or when it is not running. |
| `not_ready_reason` | When `ready` is `false`: `signed_out`, `connecting`, `offline`, `no_nickname` or `no_consent`. The companion window shows the same reason. `null` otherwise. |

The tool is read-only. `companion: running` with a non-null `last_event_at` means the hooks reach the
companion. `ready: false` with a `not_ready_reason` means no break is offered
yet.

## Uninstall

```bash
beebreak-companion uninstall                # add --keep-account to keep the server login valid
claude plugin uninstall beebreak@beebreak
claude plugin marketplace remove beebreak
uv tool uninstall beebreak-companion
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

## Links

- [Architecture and product decisions](docs/ARCHITECTURE.md)
- [Threat model](docs/THREAT_MODEL.md)
- [Data handling](docs/DATA_HANDLING.md)
- [Privacy notice (template for operators)](docs/PRIVACY_NOTICE.md)
- [Architecture decision records](docs/ADR/README.md)
- [Runbook for operators](docs/RUNBOOK.md)
- [Contributing](CONTRIBUTING.md)
- [Security policy](SECURITY.md)
