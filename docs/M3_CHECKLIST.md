# M3 manual validation checklist

Spec refs: ARCHITECTURE §4 (privacy), §11 (security controls), §14 (M3 gate).
Gate: two authorized accounts connect, and private content is never sent as
task metadata.

Platform under test: WSL2 Linux, Docker, Claude Code, companion windows via
WSLg. Run every step from the repository root unless it says otherwise.
Record results in the report template at the end. Report blocked platforms as
pending, not as passed. Step IDs are stable; do not renumber.

Written against the M3 specs (`M3-01` to `M3-07`). In M3 the companion only
connects and sends heartbeats; no task metadata is sent at all, so the
privacy steps prove a **negative**.

## How the steps work

**Terminals.**

- T1: the Docker stack (`docker compose ...` commands).
- T2: companion A (default state and runtime directories).
- T3: companion B (separate directories, see M3-C05).
- T4: a Claude Code session used only by M3-C12.

**Two companions on one machine.** They must not share the IPC socket or the
credentials. Companion B gets its own `XDG_RUNTIME_DIR` and `XDG_STATE_HOME`.
Never set these variables in T2.

**Run the companion from the venv script**, not `uv run`:
`.venv/bin/breakroom-companion`.

**Window texts** (M3-05b): `Not signed in`, `Signing in…`, `Connecting…`,
`Connected as <nickname>`, `Offline – retrying`, `Finish setup` (signed in but
no nickname or consent yet). Buttons: Sign in / Settings / Sign out, depending
on state. A `Stored unencrypted` note shows when the refresh token is kept in
the 0600 file instead of the keyring.

**Admin commands** run inside the server container:

```
docker compose exec server breakroom-admin invite create EMAIL
docker compose exec server breakroom-admin invite list
docker compose exec server breakroom-admin devices
docker compose exec server breakroom-admin user disable NICKNAME
```

`invite create` prints the invite code once (`XXXX-XXXX`). `devices` prints
one line per active device: `nickname<TAB>device name<TAB>connected=yes|no<TAB>last seen`.

**Sign-in codes** arrive by email in Mailpit: <http://127.0.0.1:8025>. Codes
are 8 characters (Crockford base32, no I, L, O, U); case and the hyphen are
ignored.

**Test identities** (any addresses work, nothing is sent outside Docker):
`filip@example.com` (account A, nickname `Filip`), `bea@example.com`
(account B, nickname `Bea`), `eve@example.com` (negative tests only).

## Setup

**M3-C01 — Install and automated checks.**

```
uv sync && uv run ruff format --check . && uv run ruff check . && uv run mypy packages apps tests tools && uv run pytest
```

Then the Postgres suite. It drops and recreates tables, so it uses its own
database, not the one the stack uses. Start Postgres first (needs `.env`, see
M3-C02, step 1):

```
docker compose up -d postgres
set -a; . ./.env; set +a
docker compose exec postgres createdb -U breakroom breakroom_test
BREAKROOM_TEST_DATABASE_URL="postgresql+psycopg://breakroom:${POSTGRES_PASSWORD}@127.0.0.1:5432/breakroom_test" uv run pytest
```

Expect: both runs green; the second also runs the migration tests, which are
skipped without the variable (the summary shows fewer skips).

**M3-C02 — Environment, stack, health, ports.**

```
cp .env.example .env
python3 - <<'EOF'
import re, secrets
text = open(".env").read()
for name in ("POSTGRES_PASSWORD", "BREAKROOM_SERVER_SECRET"):
    text = re.sub(rf"^{name}=.*$", f"{name}={secrets.token_urlsafe(48)}", text, flags=re.M)
open(".env", "w").write(text)
EOF
docker compose up -d --build
docker compose ps
curl -s http://127.0.0.1:8000/v1/health
ss -ltnp | grep -E ':(5432|1025|8025|8000)\b'
```

Expect: `postgres`, `mailpit` and `server` are running (postgres healthy);
health prints `{"status":"ok"}`; the Mailpit page loads at
<http://127.0.0.1:8025>; every listener in the `ss` output is bound to
`127.0.0.1` (none to `0.0.0.0` or `*` or `[::]`). The `.env` file stays
untracked (`git status` does not list it).

**M3-C03 — Two invites.**

```
docker compose exec server breakroom-admin invite create filip@example.com
docker compose exec server breakroom-admin invite create bea@example.com
docker compose exec server breakroom-admin invite list
```

Expect: two codes printed (write them down as INVITE1 and INVITE2); the list
shows two `open` invites and shows only `*@example.com`, never a full address.

## Two accounts

**M3-C04 — Companion A: onboarding.** In T2:

```
.venv/bin/breakroom-companion
```

Expect: the window shows `Not signed in` and a Sign in button. Before you
sign in, nothing has reached the server:
`docker compose logs server | grep -c "/v1/"` prints 0 apart from the health
check from M3-C02 (the line count does not grow while the window sits idle).

Click Sign in. Enter `filip@example.com` and INVITE1, continue. Open Mailpit,
read the code from the newest message, type it. Choose the nickname `Filip`.
On the privacy page:

- the three boxes (task category, stack tags, time remaining) are all
  **unchecked**;
- the sentence "In this version nothing is shared; these settings apply once
  matching arrives." is shown;
- "Show what is shared" opens a preview that shows only presence fields
  (schema version, presence id, status and the three consent-gated fields,
  empty or null with all boxes off), and a list of what is never shared. No
  project name, path, prompt or command appears.

Save with all boxes off. Expect: the dialog finishes; the window shows
`Connecting…` then `Connected as Filip`. If the window shows `Finish setup`
instead, the nickname or privacy page was skipped.

**M3-C05 — Companion B: separate directories and one consent box.** In T3:

```
export B_ROOT="$(mktemp -d)"
mkdir -m 700 "$B_ROOT/run" "$B_ROOT/state"
XDG_RUNTIME_DIR="$B_ROOT/run" XDG_STATE_HOME="$B_ROOT/state" .venv/bin/breakroom-companion
```

Expect: a second window opens (no "already running" message). Sign in with
`bea@example.com` and INVITE2, code from Mailpit, nickname `Bea`. On the
privacy page tick **only** "stack tags" and open "Show what is shared".
Expect: the preview shows stack tags and no task category or time remaining
(those are null or empty). Untick it, tick it again: the preview follows. Save.
Expect: `Connected as Bea`.

**M3-C06 — Gate: both accounts connected.**

```
docker compose exec server breakroom-admin devices
```

Expect: both windows say `Connected as Filip` and `Connected as Bea`; the
output has a `Filip` line and a `Bea` line, each with `connected=yes`.

**M3-C07 — Restart A without signing in again; credential location.**
Close companion A (Ctrl+C in T2, or close the window) and start it again the
same way.

```
ls -l ~/.local/state/breakroom/credentials.json
ls -ld ~/.local/state/breakroom
uv run python -c "import keyring; print(keyring.get_keyring())"
```

Expect: the window goes `Connecting…` then `Connected as Filip` with no
sign-in dialog and no new email in Mailpit. Note which storage is used:

- keyring (a real Secret Service backend): `credentials.json` does not
  exist, and the window shows no "Stored unencrypted" note;
- file fallback (WSL usually has no Secret Service; the backend prints
  `fail` or `null`): `credentials.json` is `-rw-------` in a `drwx------`
  directory and the window shows the "Stored unencrypted" note.

Either is a pass. A file that is readable by group or others is a `fail`.

**M3-C08 — Server outage and recovery.**

```
docker compose stop server
```

Wait 30 s. Expect: both windows show `Offline – retrying` (no crash, no
dialog, no growing error text). Then:

```
docker compose start server
curl -s http://127.0.0.1:8000/v1/health
```

Expect: within about a minute (reconnect back-off tops out at 60 s) both
windows are `Connected as ...` again without any sign-in, and `breakroom-admin
devices` lists both as `connected=yes`.

## Negative cases

**M3-C09 — Invite reuse, code lockout, nickname skeleton.**

(a) Invite bound to one email. Count the Mailpit messages, then try INVITE1
for another address:

```
curl -s http://127.0.0.1:8025/api/v1/messages | python3 -c "import json,sys; print(json.load(sys.stdin)['total'])"
curl -s -o /dev/null -w '%{http_code}\n' -X POST http://127.0.0.1:8000/v1/auth/start \
  -H 'content-type: application/json' \
  -d '{"email":"eve@example.com","invite_code":"INVITE1"}'
curl -s http://127.0.0.1:8025/api/v1/messages | python3 -c "import json,sys; print(json.load(sys.stdin)['total'])"
```

Replace INVITE1 with the real code. Expect: the request answers `202` (the
server never reveals whether an email was sent) and the two message counts are
equal: **no email arrives**.

(b) Five wrong codes. Sign in again as Filip through the API so the companion
windows are left alone:

```
CH=$(curl -s -X POST http://127.0.0.1:8000/v1/auth/start -H 'content-type: application/json' \
  -d '{"email":"filip@example.com"}' | python3 -c "import json,sys; print(json.load(sys.stdin)['challenge_id'])")
for i in 1 2 3 4 5; do
  curl -s -o /dev/null -w '%{http_code} ' -X POST http://127.0.0.1:8000/v1/auth/verify \
    -H 'content-type: application/json' \
    -d "{\"challenge_id\":\"$CH\",\"code\":\"ZZZZZZZZ\",\"device_name\":\"probe\"}"
done; echo
```

Then read the real code of that email in Mailpit and send it as a sixth
attempt with the same command. Expect: five rejections (`400`, or `429` if a
rate limit trips first) and the sixth, with the **correct** code, also
rejected: the challenge is locked. No tokens in any response.

(c) Nickname skeleton. In companion B open Settings and try to change the
nickname to `fi1ip`. Expect: rejected as already taken (it collides with
`Filip` after `1` becomes `l` and `i` becomes `l`); the window keeps
`Connected as Bea`. Also try `admin`: rejected as reserved.

**M3-C10 — Sign out.** In companion B click Sign out.

```
docker compose exec server breakroom-admin devices
ls -l "$B_ROOT/state/breakroom/credentials.json" 2>&1
```

Expect: B's window says `Not signed in`; `devices` no longer lists `Bea`
(Filip still `connected=yes`); B's credentials are gone (file missing, or if
B used a keyring, signing in again asks for a fresh code instead of resuming).

**M3-C11 — Admin disables an account.**

```
docker compose exec server breakroom-admin user disable Filip
```

Expect: within about 30 s companion A drops to `Not signed in` (the server
closes the session on the next frame; the companion's own keep-alive is 25 s)
and its stored credential is removed; `breakroom-admin devices` no longer
lists Filip. A new sign-in as Filip is refused.

## Privacy and report

**M3-C12 — Nothing private reaches the server.** The automated end-to-end
test (`tests/privacy/test_outbound_leaks.py`) does this against a fake corpus;
this step does it with a real Claude Code session.

Prepare: re-enable and re-sign-in so a companion is connected, and use a
project directory with a unique name outside the Breakroom checkout:

```
docker compose exec server breakroom-admin user enable Filip
mkdir -p ~/m3-canary-project && cd ~/m3-canary-project
```

Sign companion A in again (new code from Mailpit; INVITE not needed for an
existing account) and wait for `Connected as Filip`. In T4, from
`~/m3-canary-project`, start an **interactive** session with the plugin
(`claude --plugin-dir <path to this repo>/integrations/claude-code`) and send
two prompts:

1. `ZEBRA-7731: list the files in this directory with the Bash command "ls"`
2. `My key is sk-test-DO-NOT-LEAK-12345. Run the Bash command "echo sk-test-DO-NOT-LEAK-12345" and tell me what it printed.`

Let both turns finish while companion A stays connected. Then, from the
Breakroom checkout:

```
PATTERN="ZEBRA-7731|sk-test-DO-NOT-LEAK|m3-canary-project|$HOME"
echo "server logs: $(docker compose logs server 2>&1 | grep -ciE "$PATTERN")"
echo "pg_dump:     $(docker compose exec -T postgres pg_dump -U breakroom breakroom | grep -ciE "$PATTERN")"
echo "mailpit:     $(for id in $(curl -s http://127.0.0.1:8025/api/v1/messages | python3 -c "import json,sys; print(' '.join(m['ID'] for m in json.load(sys.stdin)['messages']))"); do curl -s http://127.0.0.1:8025/api/v1/message/$id; done | grep -ciE "$PATTERN")"
```

(The repo name `breakroom` is deliberately not a pattern: it occurs in logger
names, the database name and the role. The unique project directory name stands
in for it.)

Expect: all three counts are `0`. If `$HOME` is short (for example `/root`)
and a count is non-zero, inspect the matching lines before judging: only a
line carrying prompt text, command text, a path from the session, or the
project name is a hit. Any real hit is a `fail`: keep the logs, do not commit
them, and report it. Also read a few `docker compose logs server` lines by eye:
only method, path, status, timing and opaque IDs.

**M3-C13 — Report.**

```
mkdir -p reports
uv run pytest --junitxml=reports/m3-junit.xml
```

Fill in `reports/m3-report.json` from the template below, using `pass`,
`fail` or `blocked` per step. Note platforms still pending (native Windows,
macOS, Linux without WSL), per §14. `reports/` is never committed. Tear the
stack down when finished: `docker compose down -v` (deletes the dev database).

```json
{
  "date": "YYYY-MM-DD",
  "claude_code_version": "output of `claude --version`",
  "platform": "WSL2 Linux, kernel version, WSLg yes/no, Docker version",
  "steps": {
    "M3-C01": {"result": "pass", "note": ""},
    "M3-C02": {"result": "pass", "note": ""},
    "M3-C03": {"result": "pass", "note": ""},
    "M3-C04": {"result": "pass", "note": ""},
    "M3-C05": {"result": "pass", "note": ""},
    "M3-C06": {"result": "pass", "note": ""},
    "M3-C07": {"result": "pass", "note": ""},
    "M3-C08": {"result": "pass", "note": ""},
    "M3-C09": {"result": "pass", "note": ""},
    "M3-C10": {"result": "pass", "note": ""},
    "M3-C11": {"result": "pass", "note": ""},
    "M3-C12": {"result": "pass", "note": ""},
    "M3-C13": {"result": "pass", "note": ""}
  },
  "observations": {
    "credential_storage": "keyring | file",
    "stored_unencrypted_note_shown": false,
    "ports_bound_to_loopback_only": true,
    "postgres_suite_passed": true,
    "privacy_grep_hits": {"server_logs": 0, "pg_dump": 0, "mailpit": 0}
  },
  "pending_platforms": ["native Windows", "macOS", "Linux without WSL"]
}
```

M3 gate (§14): two authorized accounts connect, and private content is never
sent as task metadata. Passing means M3-C04 to M3-C08 and M3-C12 pass and the
automated privacy tests (`tests/privacy/`) are green.
