# M4 manual validation checklist

Spec refs: ARCHITECTURE §4 (privacy), §9.1–9.3 (queue and match API), §10
(retention), §11 (security controls), §14 (M4 gate). Gate: two real clients
match exactly once, and when nobody is available the search expires safely.

Platform under test: WSL2 Linux, Docker, Claude Code, companion windows via
WSLg. Run every step from the repository root unless it says otherwise.
Record results in the report template at the end. Report blocked platforms as
pending, not as passed. Step IDs are stable; do not renumber.

Written against the M4 specs (`M4-01` to `M4-08`) and the M3 checklist, whose
stack and two accounts this one reuses. Two companions run on one machine
against the local Docker stack; separate machines wait for M6 (TLS and
hosting).

## How the steps work

**Terminals.**

- T1: the Docker stack (`docker compose ...` and `breakroom-admin` commands).
- T2: companion A (default state and runtime directories), account `Filip`.
- T3: companion B (separate directories, see M4-C03), account `Bea`.
- T4: a Claude Code session used only by M4-C11 and M4-C12, attached to A.

**Two companions on one machine.** They must not share the IPC socket or the
credentials. Companion B gets its own `XDG_RUNTIME_DIR` and `XDG_STATE_HOME`.
Never set these variables in T2. Companion A's hooks and MCP server reach A
only, because T4 runs with the default directories.

**Run the companion from the venv script**, not `uv run`:
`.venv/bin/breakroom-companion`.

**Window texts.** Account row (M3): `Not signed in`, `Signing in…`,
`Connecting…`, `Connected as <nickname>`, `Offline – retrying`, `Finish setup`.
Break area (M4-06), shown only while connected with a nickname and consent
saved:

- button `Find a break partner`;
- while searching: `Looking for a break partner…` and a `Cancel` button;
- when matched: `Matched with <nickname>`, a line `In common: <labels>` only
  when both people shared something in common (for example `In common:
  Python`), and `End` and `Block` buttons;
- `Nobody is around right now. Try again later.` with an `OK` button when the
  search expired;
- `Break ended.` with an `OK` button when a match ended (either side ending it,
  a block, or the 15 minute limit);
- `Block` asks `Block this person?` / `You won't be matched with this person
  again.` with Yes and No.

An invitation reads `Your agent is busy. Want to take a short break?` with
`Accept` and `Decline`.

**Admin commands** run inside the server container:

```
docker compose exec server breakroom-admin queue list
docker compose exec server breakroom-admin matches
docker compose exec server breakroom-admin block list NICKNAME
docker compose exec server breakroom-admin block remove BLOCKER BLOCKED
docker compose exec server breakroom-admin user disable NICKNAME
```

`queue list` prints one line per queued entry, tab separated:
`nickname`, `queued`, admitted time, expiry time, `fields=<names>`. The last
column lists the **names** of the presence columns that are set
(`presence_id`, `task_category`, `stack_tags`, `remaining_time_bucket`), or
`fields=-`. It never prints a value. `matches` prints one line per active
match: the two nicknames (sorted), the creation time and the end time.
`block list NICKNAME` prints the nicknames that user blocked, one per line.
`block remove BLOCKER BLOCKED` prints nothing and exits 1 with `no such block`
when there is no such row. The server's sweep runs every second, so allow a
couple of seconds before reading `queue list` or `matches` after an action.

**Timings** (server defaults): a search lasts 90 s, a match 15 min, and a
join is rate limited to 20 per user per 15 min (stay under it: the steps
below use fewer than 12).

**Postgres queries** used to read the stored rows:

```
docker compose exec -T postgres psql -U breakroom breakroom -c "SELECT u.nickname, q.status, q.task_category, q.stack_tags, q.remaining_time_bucket, q.presence_id IS NOT NULL AS has_presence_id FROM queue_entries q JOIN users u ON u.id = q.user_id ORDER BY q.admitted_at"
```

(`Q` in the steps below.) In this output `stack_tags` is comma-joined in
vocabulary order (`python,docker`).

**Test identities** are the M3 ones: `filip@example.com` (A, nickname `Filip`)
and `bea@example.com` (B, nickname `Bea`).

## Setup

**M4-C01 — Install and automated checks.**

```
uv sync && uv run ruff format --check . && uv run ruff check . && uv run mypy packages apps tests tools && uv run pytest
```

Then the Postgres suite (race tests run there). It drops and recreates tables,
so it uses its own database, not the one the stack uses. Start Postgres first
(needs `.env`, see M3-C02):

```
docker compose up -d postgres
set -a; . ./.env; set +a
docker compose exec postgres createdb -U breakroom breakroom_test
BREAKROOM_TEST_DATABASE_URL="postgresql+psycopg://breakroom:${POSTGRES_PASSWORD}@127.0.0.1:5432/breakroom_test" uv run pytest tests/server tests/privacy tests/e2e
```

Expect: both runs green; the second also runs the migration and race tests
that are skipped without the variable. If `createdb` says the database exists,
carry on. `tests/privacy/` and `tests/e2e/` must be part of both runs.

**M4-C02 — Stack up, migration at head.** If the M3 stack is still running
from before the upgrade, rebuild it.

```
cp -n .env.example .env     # first time only; fill the two secrets as in M3-C02
docker compose up -d --build
docker compose ps
curl -s http://127.0.0.1:8000/v1/health
docker compose exec -T postgres psql -U breakroom breakroom -c "SELECT version_num FROM alembic_version"
docker compose exec -T postgres psql -U breakroom breakroom -c "\dt"
```

Expect: `postgres`, `mailpit` and `server` running; health prints
`{"status":"ok"}`; the migration query prints `0002`; the table list contains
`queue_entries`, `matches`, `match_participants` and `blocks`. (The server
container runs `breakroom-server migrate` on every start.)

**M4-C03 — Companions A and B signed in, tags chosen.** If the two accounts
from M3 still exist, start both companions and wait for `Connected as Filip`
and `Connected as Bea`. Otherwise redo M3-C03 to M3-C05 first (invites and
sign-in). If M3-C11 left `Filip` disabled, run
`docker compose exec server breakroom-admin user enable Filip` and sign A in
again.

Start A with the demo thresholds from M2-C02, so M4-C11 can use it. In T2:

```
export BREAKROOM_ACTIVITY_THRESHOLD_S=20
export BREAKROOM_MIN_TOOL_EVENTS=2
export BREAKROOM_COOLDOWN_S=45
export BREAKROOM_SUGGESTION_TIMEOUT_S=30
export BREAKROOM_STALE_AFTER_S=30
export BREAKROOM_WAITING_MAX_S=90
.venv/bin/breakroom-companion
```

Start B in T3 as in M3-C05 (`$B_ROOT` with its own `run` and `state`
directories, no demo variables needed).

In A, click **Settings**. On the privacy page tick **Share stack tags** and
**Share roughly how much time is left**, leave the task-kind box off, and tick
the tag **Python** only. Save; the page shows `You are all set.`; click Close.
In B, click **Settings**, tick **Share stack tags** only, tick the tags
**Python** and **Docker**, leave the other two boxes off, Save, Close.

Expect: the tag list appears only while the stack box is ticked; at 5 ticked
tags the remaining ones are greyed out (try it, then untick back to the
choices above); "Show what is shared" lists the ticked fields and tags and
nothing else. Both windows show `Connected as ...` and a **Find a break
partner** button. Check the server stores no tags yet:

```
docker compose exec -T postgres psql -U breakroom breakroom -c "SELECT count(*) FROM queue_entries"
```

prints `0` (or only rows from earlier runs): tags stay on the computer until a
search starts.

## The gate

**M4-C04 — Gate: two clients match exactly once.** Within a few seconds of
each other click **Find a break partner** in A and in B. Then:

```
docker compose exec server breakroom-admin matches
docker compose exec server breakroom-admin queue list
```

Expect: both windows show `Looking for a break partner…` for a moment, then A
shows `Matched with Bea` and B shows `Matched with Filip`. The shared line is
`In common: Python` on both (Python is the only tag both shared; neither
shows Docker, a time label or a task kind). `matches` prints exactly one line
naming `Bea` and `Filip`; `queue list` prints nothing. Repeat the whole step
once more (End first, see M4-C05) and again expect one line, not two. A and B
must not be matched with anyone else at any time.

**M4-C05 — End from one side ends it for both.** In A click **End**.

```
docker compose exec server breakroom-admin matches
```

Expect: both windows show `Break ended.` with an OK button; `matches` prints
nothing. Click OK in both: the Find button is back. B can also end it: repeat
M4-C04 and End from B; A shows `Break ended.` too.

**M4-C06 — Gate: scarcity.** B stays idle (do not click Find in B). In A click
**Find a break partner** and watch for about 95 s.

```
docker compose exec server breakroom-admin queue list
```

Expect: right after the click `queue list` shows one `Filip` line with
`fields=presence_id,stack_tags,remaining_time_bucket` (names only: no `python`,
no `unknown`, no `Bea`). A shows `Looking for a break partner…` and Cancel.
After about 90 s A shows `Nobody is around right now. Try again later.` and
no match is made; `queue list` is empty again. No retry starts by itself.
Click OK; Find returns.

**M4-C07 — Cancel.** In A click Find, wait 2 s, click **Cancel**.

```
docker compose exec server breakroom-admin queue list
```

Expect: within a second of Cancel the window shows the Find button again and
`queue list` is empty. A second Cancel does nothing and shows no error.

**M4-C08 — Reconnect keeps the search.** In A click Find. Stop A at once with
Ctrl+C in T2 and start it again within 60 s (same command as in M4-C03, in the
same shell so the demo variables stay set).

```
docker compose exec server breakroom-admin queue list
```

Expect: after the restart A goes `Connecting…`, `Connected as Filip`, and then
shows `Looking for a break partner…` again (the server's state is replayed on
connect). `queue list` shows exactly one `Filip` line the whole time. If you
click Find in B now, they match. If you wait the full 90 s from the original
click, A shows `Nobody is around right now. Try again later.` instead; click
OK.

**M4-C09 — Gate: server restart while both are searching.** Click Find in A
and in B, then restart the server immediately (a match usually happens before
the restart; if both already show `Matched with ...`, End the break and try
again with the restart command typed ahead):

```
docker compose restart server
```

Then:

```
docker compose exec server breakroom-admin matches
docker compose exec server breakroom-admin matches | cut -f1,2 | tr '\t' '\n' | sort | uniq -d
```

Expect: both windows show `Offline – retrying` and then `Connected as ...`
again without any sign-in. At most one match results (both windows show
`Matched with ...` for each other, or both show `Nobody is around right now.
Try again later.` if the 90 s ran out). `matches` has at most one line, and
the second command (which prints a nickname that appears in two lines) prints
nothing. Neither window ever shows a match with a different person.

## Block and rematch

**M4-C10 — Gate: block.** Match A and B (Find in both). In B click **Block**
and answer **Yes** to `Block this person?`.

```
docker compose exec server breakroom-admin block list Bea
docker compose exec server breakroom-admin matches
```

Expect: both windows show `Break ended.`; A shows no hint that it was blocked
(the same text as a normal End). `block list Bea` prints `Filip`; `matches`
prints nothing. Click OK in both, then click Find in both and wait about 95 s:
no match is made and both show `Nobody is around right now. Try again later.`
Change nothing about nicknames; the block is by account. Then:

```
docker compose exec server breakroom-admin block remove Bea Filip
docker compose exec server breakroom-admin block list Bea
```

Expect: `block remove` prints nothing and exits 0 (a second run exits 1 with
`no such block`); `block list Bea` prints nothing. Click OK, Find in both:
they match again (`Matched with ...`). End the break afterwards.

**M4-C11 — Invitation path, with a real Claude session.** Needs the demo
thresholds from M4-C03 in A, and an idle A (no break, no search). In T4, from a
scratch directory outside the checkout, start Claude with the plugin and
pre-allow the sleep command, as in M2-C07:

```
claude --plugin-dir <path to this repo>/integrations/claude-code --allowedTools "Bash(sleep:*)"
```

Send: `Run the Bash command "sleep 60 && echo done"`. About 20 s into the sleep
A shows the invitation `Your agent is busy. Want to take a short break?`.
Click **Accept**. Expect: the invitation goes away and A shows `Looking for a
break partner…` (no extra click; accepting starts the search with the
invitation's time label, which `queue list` shows only as a field name:
`fields=presence_id,stack_tags,remaining_time_bucket`). In B click Find: both
show `Matched with ...`. While A is searching or matched, send Claude another
long `sleep` in T4: the invitation does **not** appear in A. End the break in
A, and immediately send Claude `Run the Bash command "sleep 60 && echo
done"` again: **no invitation** appears for the 45 s cooldown after the break
ended. After the cooldown, another sleep brings an invitation again.

**M4-C12 — MCP: cancel only, no peer data.** In the same T4 session (A
connected; B available), ask Claude: `Call the breakroom_status tool and show
the raw result`. Expect the keys `companion`, `invitation`, `last_event_at`,
`activity`, `estimate` and `queue`, and `queue` is `none`.

- While A is searching (click Find in A, B idle): ask `Call the
  breakroom_leave_queue tool and show the raw result`. Expect
  `{"result": "cancelled"}`; A's window drops the search (Find is back) and
  `queue list` is empty within a second. Ask for `breakroom_status` during a
  search and expect `queue` `searching`.
- While A is matched (Find in A and B): ask for `breakroom_leave_queue` again.
  Expect `{"result": "not_searching"}`; the match continues and `matches`
  still has the line. `breakroom_status` shows `queue` `matched`.
- Neither result contains `Bea`, `python`, a bucket or a tag; Claude has no
  tool to start a search or end a match (`/mcp` lists `breakroom_status` and
  `breakroom_leave_queue` only).
- With A closed (Ctrl+C), the same call returns `companion_not_running` and
  `breakroom_status` shows `queue` as `null`. Start A again.

## Privacy and report

**M4-C13 — Gate: privacy.** The automated suite (`tests/privacy/`) does this
against a fake corpus; this step reads the real stored rows. Start with no
entries and both companions idle and connected.

1. A alone: click Find in A, then run `Q` (the query above). Expect one row:
   `Filip`, `queued`, `task_category` empty, `stack_tags` `python`,
   `remaining_time_bucket` `unknown`, `has_presence_id` `t`. Nothing from
   Settings that A left off appears (no task kind).
2. Click Cancel in A, run `Q`: that row is `cancelled` and `stack_tags`,
   `remaining_time_bucket` are empty and `has_presence_id` is `f`.
3. B alone: click Find in B, run `Q`: the new row is `Bea`, `queued`,
   `stack_tags` `python,docker`, `remaining_time_bucket` empty (time sharing is
   off in B), `task_category` empty. Then click Find in A so they match.
4. After the match, run `Q`: both rows are `matched` and all of
   `task_category`, `stack_tags`, `remaining_time_bucket` are empty and
   `has_presence_id` is `f`. (The `matches` table still holds the shared tag
   `python`; that is the match record, deleted 30 days after it ends.)

```
docker compose exec -T postgres psql -U breakroom breakroom -c "SELECT shared_tags, shared_category FROM matches"
echo "access lines (must be > 0): $(docker compose logs server 2>&1 | grep -c breakroom_server.access)"
echo "queue requests logged (must be > 0): $(docker compose logs server 2>&1 | grep breakroom_server.access | grep -c '/v1/queue')"
echo "server logs: $(docker compose logs server 2>&1 | grep -cE 'python|docker|10_20m|5_10m|Filip|Bea|filip@|bea@|bra_|brr_')"
```

Expect: the last command prints `0` (the third also keeps your tokens, emails
and nicknames out of the logs); the two earlier counts are above `0` (otherwise
the zero proves nothing). If the last count is not `0`, look at the matching
lines before judging: a traceback path such as `/usr/local/lib/python3.12/...`
is not a hit; a tag value, bucket, nickname or token is a `fail`. Also read a
few `docker compose logs server` lines by eye: only method, route, status,
timing and opaque IDs.

**M4-C14 — Report.**

```
mkdir -p reports
uv run pytest --junitxml=reports/m4-junit.xml
```

Fill in `reports/m4-report.json` from the template below, using `pass`,
`fail` or `blocked` per step. Note platforms still pending (native Windows,
macOS, Linux without WSL, and two separate machines until M6). `reports/` is
never committed. Tear the stack down when finished: `docker compose down -v`
(deletes the dev database).

```json
{
  "date": "YYYY-MM-DD",
  "claude_code_version": "output of `claude --version`",
  "platform": "WSL2 Linux, kernel version, WSLg yes/no, Docker version",
  "steps": {
    "M4-C01": {"result": "pass", "note": ""},
    "M4-C02": {"result": "pass", "note": ""},
    "M4-C03": {"result": "pass", "note": ""},
    "M4-C04": {"result": "pass", "note": ""},
    "M4-C05": {"result": "pass", "note": ""},
    "M4-C06": {"result": "pass", "note": ""},
    "M4-C07": {"result": "pass", "note": ""},
    "M4-C08": {"result": "pass", "note": ""},
    "M4-C09": {"result": "pass", "note": ""},
    "M4-C10": {"result": "pass", "note": ""},
    "M4-C11": {"result": "pass", "note": ""},
    "M4-C12": {"result": "pass", "note": ""},
    "M4-C13": {"result": "pass", "note": ""},
    "M4-C14": {"result": "pass", "note": ""}
  },
  "observations": {
    "migration_version": "0002",
    "gate_match_lines": 1,
    "shared_line_shown": "In common: Python",
    "search_expired_after_s": 90,
    "restart_matches_max": 1,
    "block_hint_shown_to_blocked": false,
    "leave_queue_while_searching": "cancelled",
    "leave_queue_while_matched": "not_searching",
    "postgres_suite_passed": true,
    "privacy_grep_hits": {"server_logs": 0}
  },
  "pending_platforms": ["native Windows", "macOS", "Linux without WSL", "two separate machines (M6)"]
}
```

M4 gate (§14): two real clients match exactly once, and when nobody is
available the search expires safely. Passing means M4-C04, M4-C06, M4-C09,
M4-C10 and M4-C13 pass, and the race and privacy tests are green on Postgres
(`tests/server`, `tests/privacy`, `tests/e2e`).
