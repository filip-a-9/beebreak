# M5 manual validation checklist

Spec refs: ARCHITECTURE §4 (privacy), §6.4–6.5 (chat window and notifications),
§9.1–9.2 (chat and report API), §10 (retention), §11 (security controls), §14
(M5 gate); `docs/THREAT_MODEL.md` T36–T49. Gate: a two-user end-to-end chat
survives a transient disconnect; expiry and the moderation controls (End,
Block, Report) are tested.

Platform under test: WSL2 Linux, Docker, Claude Code, companion windows via
WSLg. Run every step from the repository root unless it says otherwise.
Record results in the report template at the end. Report blocked platforms as
pending, not as passed. Step IDs are stable; do not renumber.

Written against the M5 specs (`M5-01` to `M5-08`) and the M4 checklist, whose
stack and two accounts this one reuses. Run `docs/M4_CHECKLIST.md` first if it
has not been run: M5 builds on the match lifecycle it proves. Two companions
run on one machine against the local Docker stack; separate machines wait for
M6 (TLS and hosting).

## How the steps work

**Terminals.**

- T1: the Docker stack (`docker compose ...`, `breakroom-admin` commands, the
  network proxy of M5-C03).
- T2: companion A (default state and runtime directories), account `Filip`.
- T3: companion B (separate directories and a different server address, see
  M5-C03), account `Bea`.
- T4: a Claude Code session used only by M5-C17, attached to A.

**Two companions on one machine.** As in M4: companion B gets its own
`XDG_RUNTIME_DIR` and `XDG_STATE_HOME`; never set them in T2. Run the
companion from the venv script, not `uv run`: `.venv/bin/breakroom-companion`.

**Companion B reaches the server through a local proxy.** M5-C07 and M5-C08
cut B's network without stopping B. The only portable way on WSL2 that spares
A is to give B its own address: B talks to `http://127.0.0.1:8001`, a `socat`
forwarder to the server on port 8000, and "cutting B's network" means killing
the forwarder. Credentials are stored per server address, so B signs in again
once (M5-C03). A keeps `http://127.0.0.1:8000`.

**Window texts.** Main window (M4 texts still hold; M5 changes the matched
state): while matched it shows `Chatting with <nickname>`, a line `In common:
<labels>` when both shared something, and the button `Open chat` (with an
unread count: `Open chat (2)`). It has no End button any more; `Break ended.`
with `OK` appears when a chat has ended, and `Block` stays there only after
the chat has ended.

Chat window (title `Breakroom chat`, compact, separate from the main window;
it opens by itself on a match without taking focus). It shows the peer's
nickname, the `In common:` line, the time left (`15 min left`, `< 1 min left`),
the transcript (own messages right-aligned, the peer's left), an input box,
and the buttons `Send`, `End`, `Block`, `Report`, `Close` (`Close` only after
the chat ended). Enter sends, Shift+Enter inserts a newline; a counter
`n / 1000` shows from 900 characters. Exact strings:

- footer, always visible: `Not end-to-end encrypted. Messages are deleted 24 h after the chat ends.`
- own message marks: `sending…`, and a button `not sent, retry`;
- connection lines: `Reconnecting…` (this companion is offline) and
  `<nickname> is reconnecting…` (the peer is offline);
- grace banner in the last 2 minutes: `2 minutes left. Extend by 10 minutes?`
  with the button `Extend` (with the short profile of M5-C09 the extension
  reads `2 minutes`); once no extensions are left: `2 minutes left.`;
- extension banner, requester: `Waiting for <nickname>…` with `Cancel`;
  peer: `<nickname> wants 10 more minutes.` with `Yes` and `No`;
- `End` asks `End this chat?` / `The chat ends for both of you.` with Yes and
  No; `Block` asks `Block this person?` / `You won't be matched with this
  person again.` with Yes and No;
- ended lines (replace the input): `The chat ended.`, `Time is up.`,
  `The connection was lost.`

Report dialog (title `Report`): `Reporting ends the chat and blocks <nickname>.
Only the messages you tick are sent to the moderators.`; radio buttons
`Harassment or abuse`, `Spam or advertising`, `Sexual content`,
`Hate speech`, `Something else`; a note box with placeholder `Optional note`
and a counter `n / 500`; the heading `Messages to include (none are included
unless you tick them):` over a list of checkboxes (`You: ...` and
`<nickname>: ...`), all unticked; buttons `Report and block` (enabled once a
reason is chosen) and `Cancel`.

**Admin commands** (inside the server container) added in M5:

```
docker compose exec server breakroom-admin report list
docker compose exec server breakroom-admin report list --all
docker compose exec server breakroom-admin report show REPORT_ID
docker compose exec server breakroom-admin report resolve REPORT_ID dismiss
docker compose exec server breakroom-admin report resolve REPORT_ID disable
```

`report list` prints open reports, oldest first, one tab-separated line each:
id, created time, reason, reporter nickname, reported nickname, number of
evidence messages, status. It never prints a note or a message (`--all` adds
resolved ones). `report show` starts with `WARNING: the output below contains
user-written content.`, then `id`, `match`, `created`, `reason`, `reporter`,
`reported`, `status`, `resolved`, `note`, and one `[seq] author time: body`
line per copied message. `resolve ... dismiss` sets `dismissed`;
`resolve ... disable` sets `actioned` and disables the reported account
(`user enable NICKNAME` undoes it). Resolving twice or an unknown ID exits 1.
The M4 commands (`matches`, `queue list`, `block list NICKNAME`,
`block remove BLOCKER BLOCKED`, `user enable|disable`) still apply. The sweep
runs every second; allow a couple of seconds before reading after an action.

**Timings** (server defaults): a chat lasts 15 min, the grace period is the last
2 min, one extension adds 10 min and at most 2 are allowed, a person offline
for 60 s ends the chat (`disconnected`), 20 messages per 10 s per user, message
bodies are deleted 24 h after the chat ends, a report is accepted up to 24 h
after the chat ended, report evidence is deleted 90 days after resolution.
Three steps use shortened timings through a Compose override file (M5-C09,
M5-C10, M5-C16); each says when to go back to the defaults. A join is limited
to 20 per user per 15 min and this checklist uses about 10; if a Find answers
with an error, run `docker compose restart server` (the limits are in memory).

**Canary texts.** Messages and the report note in the steps below contain these
strings so that M5-C18 can search the server logs for them:
`CANARY-M5-7f3a` (messages) and `CANARY-NOTE-M5-7f3a` (report note).

**Block list.** A block outlives the chat and stops the same two people being
matched. After every step that blocks (M5-C12, C13, C14) remove the block
(`block remove BLOCKER BLOCKED`, given in the step) before the next Find.

**Postgres queries** used to read the stored rows:

```
docker compose exec -T postgres psql -U breakroom breakroom -c "SELECT m.seq, u.nickname, length(m.body) AS chars FROM messages m JOIN users u ON u.id = m.author_id WHERE m.match_id = (SELECT id FROM matches ORDER BY created_at DESC LIMIT 1) ORDER BY m.seq"
docker compose exec -T postgres psql -U breakroom breakroom -c "SELECT status, end_reason, extensions_used, last_seq FROM matches ORDER BY created_at DESC LIMIT 3"
docker compose exec -T postgres psql -U breakroom breakroom -c "SELECT r.status, r.reason, (r.note IS NOT NULL) AS has_note, (SELECT count(*) FROM report_messages rm WHERE rm.report_id = r.id) AS evidence FROM reports r ORDER BY r.created_at"
```

(`MQ`, `XQ` and `RQ` in the steps below.) `MQ` lists the latest match's
messages by sequence number, author and length, never a body.

**Test identities** are the M3 ones: `filip@example.com` (A, nickname `Filip`)
and `bea@example.com` (B, nickname `Bea`); sign-in codes arrive in Mailpit at
`http://127.0.0.1:8025`.

## Setup

**M5-C01 — Install and automated checks.**

```
uv sync && uv run ruff format --check . && uv run ruff check . && uv run mypy packages apps tests tools && uv run pytest
```

Then the Postgres suite, as in M4-C01 (own database, needs `.env`):

```
docker compose up -d postgres
set -a; . ./.env; set +a
docker compose exec postgres createdb -U breakroom breakroom_test
BREAKROOM_TEST_DATABASE_URL="postgresql+psycopg://breakroom:${POSTGRES_PASSWORD}@127.0.0.1:5432/breakroom_test" uv run pytest tests/server tests/privacy tests/e2e
```

Expect: both runs green. `tests/privacy/test_chat_privacy.py`,
`tests/privacy/test_outbound_leaks.py` and `tests/e2e/test_two_companions_chat.py`
must be part of both runs (check the collected file names). If `createdb` says
the database exists, carry on.

**M5-C02 — Stack up, migration `0003`.** If the M4 stack is still running,
rebuild it so the server gets the chat routes.

```
mkdir -p reports
cp -n .env.example .env     # first time only; fill the two secrets as in M3-C02
docker compose up -d --build
docker compose ps
curl -s http://127.0.0.1:8000/v1/health
docker compose exec -T postgres psql -U breakroom breakroom -c "SELECT version_num FROM alembic_version"
docker compose exec -T postgres psql -U breakroom breakroom -c "\dt"
docker compose exec server breakroom-admin report list
```

Expect: `postgres`, `mailpit` and `server` running; health prints
`{"status":"ok"}`; the migration query prints `0003`; the table list now also
contains `messages`, `reports` and `report_messages`; `report list` prints
nothing and exits 0 (the server container runs `breakroom-server migrate` on
every start).

**M5-C03 — Companions signed in, B behind the proxy, one match.** `socat` is
needed once: `sudo apt-get install -y socat`. In T1 start the forwarder for B
and check it:

```
socat TCP-LISTEN:8001,fork,reuseaddr,bind=127.0.0.1 TCP:127.0.0.1:8000 &
curl -s http://127.0.0.1:8001/v1/health
```

Expect `{"status":"ok"}`. Start A in T2 (`.venv/bin/breakroom-companion`;
no demo variables are needed). Start B in T3 with its own directories and the
proxy address; reuse `$B_ROOT` from M4 or make a new one:

```
export B_ROOT="${B_ROOT:-$(mktemp -d)}"
mkdir -p -m 700 "$B_ROOT/run" "$B_ROOT/state"
BREAKROOM_SERVER_URL=http://127.0.0.1:8001 XDG_RUNTIME_DIR="$B_ROOT/run" XDG_STATE_HOME="$B_ROOT/state" .venv/bin/breakroom-companion
```

Always start B with this exact command from here on. B is a new server
address for the credentials, so B shows `Not signed in`: sign in as
`bea@example.com` with the code from Mailpit (the nickname `Bea` and the
consent from M4 still exist; if the nickname or settings pages appear, enter
`Bea` and tick **Share stack tags** with the tag **Python**). In A, if the
M4 settings were lost, tick **Share stack tags** and the tag **Python**.
Wait for `Connected as Filip` and `Connected as Bea`. Then click **Find a break
partner** in A and in B.

```
docker compose exec server breakroom-admin matches
```

Expect: both main windows show `Chatting with Bea` / `Chatting with Filip`
and the `Open chat` button; a second window titled `Breakroom chat` opened
by itself on each side without stealing the keyboard focus (the terminal you
were typing in keeps it); it shows the peer's nickname, `In common: Python`,
`15 min left` and the footer. `matches` prints exactly one line naming `Bea`
and `Filip`. Leave this chat running for M5-C04 to M5-C07.

## Chat

**M5-C04 — Chat both ways; the window is independent; unread count.** In
A's chat window type `hello from A CANARY-M5-7f3a` and press Enter; then click
into B's chat window and answer `hello from B CANARY-M5-7f3a`. Send two more
messages from each side, alternating. Type a line, press Shift+Enter, type
another line, press Enter (one message, two lines). Try Send with an empty
box and with only spaces.

```
docker compose exec -T postgres psql -U breakroom breakroom -c "SELECT m.seq, u.nickname, length(m.body) AS chars FROM messages m JOIN users u ON u.id = m.author_id WHERE m.match_id = (SELECT id FROM matches ORDER BY created_at DESC LIMIT 1) ORDER BY m.seq"
```

Expect: each message appears on both sides within a second, in the same
order, right-aligned for the author and left-aligned for the peer; the input
clears on Send; Send is disabled (and Enter does nothing) for an empty or
all-space box; the two-line message shows as two lines. `MQ` lists seqs `1`
to `N` with no gaps, alternating nicknames as sent. No `sending…` mark stays
behind. Then, in A, close the chat window with the window's own close button
(it has no `Close` button while the chat is live): the chat goes on, A's main
window still shows `Chatting with Bea`, and the window is gone. Have B send
three messages: A's main window shows `Open chat (3)` (the unread count).
Click **Open chat**: the chat window comes back with all messages and the
focus is in the input box; the button reads `Open chat` again. Send one more
message from A: it shows on both sides and nothing earlier is lost.

**M5-C05 — Disclosure footer.** In both chat windows, at every state you
see in this step (empty chat, scrolled transcript, a long message, after
closing and reopening), the footer line reads exactly `Not end-to-end
encrypted. Messages are deleted 24 h after the chat ends.` It is never
hidden, never truncated to nothing and never needs scrolling to reach (resize
the window smaller and larger once).

**M5-C06 — Plain text, and a notification without text.** In B's chat window
send exactly this one line:

```
<b>bold</b> <a href="http://example.com">link</a> <img src="x"> &amp; CANARY-M5-7f3a
```

Expect in A: the line appears literally, with the angle brackets and `&amp;`
visible, no bold, no link styling, no image. Click on the text: nothing
opens. Open the report dialog in A (`Report`, then `Cancel`): the same line
is listed literally in the message list and nothing is ticked. Also check the
nickname lines show `Bea` and `Filip` only.

Notification: in A click the main window (so the chat window is not the
active window) and have B send `ping CANARY-M5-7f3a`. Expect: the main
window's button becomes `Open chat (1)` (or the count above plus one), and, if
WSLg has a system tray, a notification titled `Breakroom` reading exactly
`New message from Bea` and never the text `ping` or `CANARY`. Without a tray
the companion only flashes the chat window and no text is shown anywhere
outside the window; record which of the two you saw. A second message from B
within 10 s raises no second notification. Click **Open chat** to read them.

## Disconnects

**M5-C07 — B offline for about 20 s: reconnect without losing anything.**
The chat of M5-C03 must still be live (more than 3 minutes left; if not,
start a new match first). In T1 cut B's network, wait, and restore it:

```
pkill -f 'socat TCP-LISTEN:8001'
sleep 20
socat TCP-LISTEN:8001,fork,reuseaddr,bind=127.0.0.1 TCP:127.0.0.1:8000 &
curl -s http://127.0.0.1:8001/v1/health
```

(B's companion keeps running; only its connection to the server is dropped.)
Within the 20 s, in A send three messages (`missed 1 CANARY-M5-7f3a`,
`missed 2 ...`, `missed 3 ...`), and in B try to send one message `from B while
offline`.

Expect: within a few seconds of the cut A's chat window shows
`Bea is reconnecting…` and B's shows `Reconnecting…` (B's main window
`Offline – retrying`); the chat stays live and A can keep typing. B's
message shows `sending…` or `not sent, retry` on B (either is fine; if
`not sent, retry`, click it after reconnect). After the restore B shows
`Connected as Bea` again, the lines disappear on both sides, and B receives
`missed 1` to `missed 3` in order (fetched by sequence number), and A receives
B's offline message exactly once (no duplicate). `MQ` shows consecutive
seqs with no duplicates and `XQ` shows the match still `active`.

**M5-C08 — B offline for more than 60 s: the chat ends.** Start a new match
if the previous one is not live (Find in both, End first if needed). Then:

```
pkill -f 'socat TCP-LISTEN:8001'
sleep 75
docker compose exec server breakroom-admin matches
socat TCP-LISTEN:8001,fork,reuseaddr,bind=127.0.0.1 TCP:127.0.0.1:8000 &
```

Expect: A shows `Bea is reconnecting…` from about the first second, then
about 60 s after the cut A's chat window shows `The connection was lost.`
(input disabled, a `Close` button, `Report` still available), and the main
window shows `Break ended.`; `matches` prints nothing; `XQ` shows the latest
match `ended` with `disconnected`. After the restore B reconnects and its
chat window shows the chat as ended: record the line it shows in the report
(`b_line_after_outage`). The expected line is `The chat ended.` or `The
connection was lost.`: B was offline when the server ended the chat, so only
the synchronisation after the reconnect tells it. The transcript up to the
cut stays readable on both sides. Click `Close` in both chat windows and OK in
both main windows.

## Time: grace, extension, expiry

These two steps use a shortened chat so the 2-minute grace period fits in a few
minutes. In T1:

```
cat > reports/compose.m5-short.yaml <<'EOF'
services:
  server:
    environment:
      BREAKROOM_MATCH_TTL_S: "240"
      BREAKROOM_CHAT_GRACE_S: "120"
      BREAKROOM_CHAT_EXTENSION_S: "120"
EOF
docker compose logs server >> reports/m5-server-logs.txt 2>&1
docker compose -f compose.yaml -f reports/compose.m5-short.yaml up -d server
```

Expect both companions back to `Connected as ...` within a few seconds.
(Recreating the server clears the container log, which is why it is saved
first; M5-C18 reads the saved file too.)

**M5-C09 — Grace banner and extension accepted.** Find in both. The chat
window shows `4 min left`. After about 2 minutes (when 2 minutes are left)
both windows show the banner `2 minutes left. Extend by 2 minutes?` with an
`Extend` button (not before). In A click **Extend**.

Expect: A shows `Waiting for Bea…` with a `Cancel` button; B shows `Filip
wants 2 more minutes.` with `Yes` and `No`. Click **Yes** in B. Both banners
disappear and the countdown goes up to about `4 min left`. Wait (the banner
returns when 2 minutes are left) and extend once more the same way; after the
second accepted extension the banner reads only `2 minutes left.` (no
button). `XQ` shows `extensions_used` `2` while it runs. The chat ends only
at the extended deadline (original 4 min plus 2 plus 2, so about 8 minutes
after the match), not at 4 minutes: both windows then show `Time is up.` and
`XQ` shows `ended` with `expired`. Click `Close` and OK in both windows. A
check of the cap: it cannot be tried in the window (the button is gone); the
automated tests cover the third request.

**M5-C10 — Extension declined, then expiry at the original deadline.** Find
in both again (same short profile). When the banner appears, click **Extend** in
B; in A click **No**. Expect: B's banner drops back to the plain `Extend`
offer or `2 minutes left.`, nothing is extended, no error is shown. Let the
clock run out: both windows show `Time is up.` at 4 minutes after the match
(`XQ` shows `expired`, `extensions_used` `0`). Also try once: B clicks
**Extend**, then B clicks **Cancel** (withdraw) before A answers: A's banner
disappears. Click `Close` and OK in both windows. Then go back to the defaults:

```
docker compose logs server >> reports/m5-server-logs.txt 2>&1
docker compose up -d server
```

(No `-f` override: Compose recreates the server with the default timings.)
Wait for both to show `Connected as ...`.

## End, Block, Report

**M5-C11 — End from either side.** Find in both. In A click **End** in the
chat window and answer **No** to `End this chat?`: nothing happens. Click End
again, **Yes**.

Expect: both chat windows show `The chat ended.`, input disabled, `Close`
visible; both main windows show `Break ended.`; `matches` prints nothing;
`XQ` shows `ended` with `ended`. Send is impossible. Click `Close` in both
chat windows (the chat window disappears) and OK in both main windows. Repeat
with B ending it.

**M5-C12 — Block.** Find in both. In B's chat window click **Block**, answer
`Block this person?` with **Yes**.

```
docker compose exec server breakroom-admin block list Bea
```

Expect: both chat windows show `The chat ended.` (A gets no hint that it was
blocked); `block list Bea` prints `Filip`; `XQ` shows the match `ended`
with the stored reason `blocked` (never shown to A). Click `Close` in both chat
windows and OK in both main windows (Find is hidden until then). Click Find in
both and wait
about 95 s: no match is made. Then remove the block and match again:

```
docker compose exec server breakroom-admin block remove Bea Filip
```

Click OK in both, Find in both: they match. End that chat (End in A).

**M5-C13 — Report with one ticked message, then the admin CLI.** Find in
both. Send these messages in this order: B: `harassing line CANARY-M5-7f3a`;
A: `reply one`; B: `second line from B`; A: `reply two`. In A click
**Report**. In the dialog: expect nothing ticked and `Report and block`
disabled; choose `Harassment or abuse`, type the note `CANARY-NOTE-M5-7f3a`,
tick only `Bea: harassing line CANARY-M5-7f3a`, click **Report and block**.

```
docker compose exec server breakroom-admin report list
docker compose exec server breakroom-admin report show REPORT_ID
docker compose exec -T postgres psql -U breakroom breakroom -c "SELECT r.status, r.reason, (r.note IS NOT NULL) AS has_note, (SELECT count(*) FROM report_messages rm WHERE rm.report_id = r.id) AS evidence FROM reports r ORDER BY r.created_at"
docker compose exec server breakroom-admin block list Filip
```

(Take `REPORT_ID` from the first column of `report list`.) Expect: A's chat
window shows `The chat ended.` and its `Report` button is disabled (no `Block`
button either); B's chat window shows `The chat ended.` and B sees nothing
about a report or block; the main windows show `Break ended.`. `report list`
prints one line: `harassment`, `Filip`, `Bea`, evidence count `1`, `open`,
and no note or message text. `report show` starts with the WARNING line, shows
`reason: harassment`, `reporter: Filip`, `reported: Bea`, `note:
CANARY-NOTE-M5-7f3a` and exactly one message line, `[1] reported <time>:
harassing line CANARY-M5-7f3a` (seq 1 here; only the ticked one, none of the
others). `RQ` shows `open`, `harassment`, `has_note` `t`, `evidence` `1`, and `XQ`
shows the match `ended` with the stored reason `reported` (neither window shows
it).
`block list Filip` prints `Bea`. Remove the block before going on:
`docker compose exec server breakroom-admin block remove Filip Bea`. Click
`Close` and OK in both windows.

**M5-C14 — Late report from the ended chat window.** Find in both and send a
message from each side (`late one`, `late two`). In B click **End**. In A the
chat window shows `The chat ended.` but A keeps its window open (do not click
`Close`) for at least 30 s; its `Report` and `Block` buttons are still
there. In A click **Report**, choose `Spam or advertising`, tick `Bea: late
two`, no note, **Report and block**.

```
docker compose exec server breakroom-admin report list
docker compose exec server breakroom-admin block remove Filip Bea
```

Expect: the report is accepted (no error dialog), A's `Report` button is
disabled, `report list` now shows two open reports (`harassment` and `spam`),
the new one with evidence count `1`. Also check the rule from the other
side: in B's chat window (ended, still open) the `Report` button works for B
as well; do not use it. Click `Close` and OK in both windows.

**M5-C15 — Resolve: dismiss one, disable with the other.** Take the two ids
from `report list`.

```
docker compose exec server breakroom-admin report resolve ID_OF_SPAM dismiss
docker compose exec server breakroom-admin report resolve ID_OF_HARASSMENT disable
docker compose exec server breakroom-admin report list
docker compose exec server breakroom-admin report list --all
docker compose exec server breakroom-admin report resolve ID_OF_HARASSMENT disable; echo "exit $?"
docker compose exec server breakroom-admin devices
```

Expect: both `resolve` commands print nothing and exit 0; `report list`
prints nothing; `--all` shows `dismissed` for the spam one and `actioned` for
the harassment one; the repeated resolve exits 1 with `report already
resolved`; `devices` no longer lists Bea's device. Within a few seconds B
drops to `Offline – retrying` or `Not signed in` (its account is disabled).
Then re-enable and sign B in again (code from Mailpit):

```
docker compose exec server breakroom-admin user enable Bea
```

and expect `Connected as Bea` after the sign-in.

## Retention, Claude Code, privacy

**M5-C16 — Message bodies are purged, report evidence is kept.** This step
shortens the message retention to 60 s (the purge runs about once a minute).

```
cat > reports/compose.m5-retention.yaml <<'EOF'
services:
  server:
    environment:
      BREAKROOM_MESSAGE_RETENTION_S: "60"
EOF
docker compose logs server >> reports/m5-server-logs.txt 2>&1
docker compose -f compose.yaml -f reports/compose.m5-retention.yaml up -d server
```

Wait for both companions to show `Connected as ...`. Find in both, send `purge me
CANARY-M5-7f3a` from A, then End. Run `MQ` at once: it lists the messages.
Wait about 2 minutes and run:

```
docker compose exec -T postgres psql -U breakroom breakroom -c "SELECT count(*) FROM messages"
docker compose exec -T postgres psql -U breakroom breakroom -c "SELECT count(*) FROM report_messages"
docker compose exec -T postgres psql -U breakroom breakroom -c "SELECT status, end_reason FROM matches ORDER BY created_at DESC LIMIT 3"
```

Expect: `messages` count `0` (the earlier chats' bodies are gone too, all of
them ended more than a minute ago); `report_messages` still `2` (the two
ticked messages of M5-C13 and M5-C14; the reports are resolved only seconds to
minutes ago, far inside 90 days); the `matches` rows are still there (they
go at 30 days). Return to the defaults and keep the stack:

```
docker compose logs server >> reports/m5-server-logs.txt 2>&1
docker compose up -d server
```

**M5-C17 — Claude Code sees `chatting` and nothing else.** In T4, from a
scratch directory outside the checkout, start Claude with the plugin as in
M4-C11 (no need to pre-allow anything):

```
claude --plugin-dir <path to this repo>/integrations/claude-code
```

Find in both (A and B connected) so a chat is live. Ask: `Call the
breakroom_status tool and show the raw result`. Expect the keys `companion`,
`invitation`, `last_event_at`, `activity`, `estimate` and `queue` (nothing
new), `queue` is `chatting`, and the result contains no `Bea`, no deadline, no
message text and no count. Ask `Call the breakroom_leave_queue tool and show
the raw result`: expect `{"result": "not_searching"}` and the chat is still
live in both windows. Ask Claude `What did Bea say in my chat?`: it has no way
to know, and `/mcp` lists `breakroom_status` and `breakroom_leave_queue` only
(no send, read, extend or end tool). Send a message `claude must not see this
CANARY-M5-7f3a` in the chat and ask for `breakroom_status` again: unchanged.
End the chat in A afterwards; `breakroom_status` shows `queue` `none` again.

**M5-C18 — Privacy grep of the server logs.** The automated suite
(`tests/privacy/`) does this against a fake corpus; this step searches the
real logs of this run. The earlier containers' logs were saved to
`reports/m5-server-logs.txt` before each restart; add the current one:

```
{ cat reports/m5-server-logs.txt; docker compose logs server 2>&1; } > reports/m5-server-all.txt
echo "access lines (must be > 0): $(grep -c breakroom_server.access reports/m5-server-all.txt)"
echo "message requests logged (must be > 0): $(grep breakroom_server.access reports/m5-server-all.txt | grep -c '/messages')"
echo "report requests logged (must be > 0): $(grep breakroom_server.access reports/m5-server-all.txt | grep -c '/v1/reports')"
echo "canary hits: $(grep -c 'CANARY' reports/m5-server-all.txt)"
echo "server logs: $(grep -cE 'CANARY|harassing line|late two|hello from|Filip|Bea|filip@|bea@|bra_|brr_|<b>bold' reports/m5-server-all.txt)"
```

Expect: the three counts above `0` (otherwise the zero proves nothing); both
`canary hits` and `server logs` print `0`. If `server logs` is not `0`, read
the matching lines before judging: a nickname, token, email, any message
text or the note is a `fail`; a tag from a traceback path is not a hit.
Also read a few lines by eye: only method, route, status, timing, opaque IDs
and fixed strings (`report filed id=<uuid> reason=harassment`, `presence
offline=1 ...`, `purged entries=... messages=... reports=...`). For the
companions, check the terminals T2 and T3 by eye or search their scrollback
for `CANARY`: there must be none.

**M5-C19 — Report.**

```
mkdir -p reports
uv run pytest --junitxml=reports/m5-junit.xml
```

Fill in `reports/m5-report.json` from the template below, using `pass`,
`fail` or `blocked` per step. Note platforms still pending (native Windows,
macOS, Linux without WSL, and two separate machines until M6). `reports/` is
never committed. Stop the forwarder (`pkill -f 'socat TCP-LISTEN:8001'`) and
tear the stack down when finished: `docker compose down -v` (deletes the dev
database).

```json
{
  "date": "YYYY-MM-DD",
  "claude_code_version": "output of `claude --version`",
  "platform": "WSL2 Linux, kernel version, WSLg yes/no, Docker version",
  "steps": {
    "M5-C01": {"result": "pass", "note": ""},
    "M5-C02": {"result": "pass", "note": ""},
    "M5-C03": {"result": "pass", "note": ""},
    "M5-C04": {"result": "pass", "note": ""},
    "M5-C05": {"result": "pass", "note": ""},
    "M5-C06": {"result": "pass", "note": ""},
    "M5-C07": {"result": "pass", "note": ""},
    "M5-C08": {"result": "pass", "note": ""},
    "M5-C09": {"result": "pass", "note": ""},
    "M5-C10": {"result": "pass", "note": ""},
    "M5-C11": {"result": "pass", "note": ""},
    "M5-C12": {"result": "pass", "note": ""},
    "M5-C13": {"result": "pass", "note": ""},
    "M5-C14": {"result": "pass", "note": ""},
    "M5-C15": {"result": "pass", "note": ""},
    "M5-C16": {"result": "pass", "note": ""},
    "M5-C17": {"result": "pass", "note": ""},
    "M5-C18": {"result": "pass", "note": ""},
    "M5-C19": {"result": "pass", "note": ""}
  },
  "observations": {
    "migration_version": "0003",
    "chat_window_opened_without_focus": true,
    "unread_count_shown": "Open chat (3)",
    "notification_text": "New message from Bea (tray) | window flash only (no tray)",
    "html_line_rendered_literally": true,
    "missed_messages_after_20s_outage": 3,
    "a_line_after_60s_outage": "The connection was lost.",
    "b_line_after_outage": "The chat ended. | The connection was lost.",
    "extensions_accepted": 2,
    "declined_extension_ended_at_original_deadline": true,
    "block_hint_shown_to_blocked": false,
    "report_evidence_messages": 1,
    "late_report_accepted": true,
    "report_disable_revoked_devices": true,
    "messages_after_retention": 0,
    "report_messages_after_retention": 2,
    "breakroom_status_queue_while_chatting": "chatting",
    "postgres_suite_passed": true,
    "privacy_grep_hits": {"server_logs": 0, "canary": 0}
  },
  "pending_platforms": ["native Windows", "macOS", "Linux without WSL", "two separate machines (M6)"]
}
```

M5 gate (§14): a two-user end-to-end chat survives a transient disconnect;
expiry and the moderation controls (End, Block, Report) are tested. Passing
means the reconnect, expiry, extension and report steps pass and `tests/server`,
`tests/privacy`, `tests/e2e` are green on Postgres. The steps that count are
M5-C07 and M5-C08 (reconnect), M5-C09 and M5-C10 (expiry and extension),
M5-C12 to M5-C15 (Block and Report), and M5-C18 (privacy).
