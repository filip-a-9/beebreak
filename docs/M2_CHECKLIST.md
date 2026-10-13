# M2 manual validation checklist

Spec refs: ARCHITECTURE §7 (decision), §14 (M2 gate). Gate: short, long,
waiting and stop traces are classified sensibly, and nothing ever says that
`Stop` means the task is finished.

Platform under test: WSL2 Linux, Claude Code, companion window via WSLg.
Run every step from the repository root, in **interactive** Claude Code
sessions: headless `claude -p` delivers no `Stop` and cannot show permission
prompts. Record results in the report template at the end. Report blocked
platforms as pending, not as passed. Step IDs are stable; do not renumber.

Written against the M2 specs (`M2-04` integration, `M2-05` replay). Where a
step says "replay", it uses `tools/replay_trace.py TRACE.jsonl
[--respond none|accept|decline]`.

## How the steps work

**Terminals.** T1: the companion. T2: the Claude session under test ("A").
T3: a long-lived probe Claude session ("P") used only to ask for
`breakroom_status`, because a session that is waiting on a prompt cannot be
asked anything. Start both Claude sessions with
`claude --plugin-dir integrations/claude-code`.

**Pre-allowed tool.** Steps that need a command to run without a prompt start
the session as `claude --plugin-dir integrations/claude-code --allowedTools
"Bash(sleep:*)"`. Steps that need a prompt use plain `claude --plugin-dir
integrations/claude-code` in default permission mode.

**Reading `breakroom_status` from P.** Ask P: "Call the breakroom_status tool
and show the raw result." Fields: `companion`, `invitation`, `last_event_at`,
`activity`, `estimate`. P's own tool call is in flight when the reply is
computed, so P itself counts as `active`. Therefore `waiting_input` in another
session is visible (it takes precedence), but `idle` is **not** observable
this way; steps that need `idle` use replay instead.

**Two passes per trace step.** The recorder (`tools/record_trace.py`) and the
companion bind the same socket, so they cannot run together.

- Pass 1, live: companion running, watch the window and ask P.
- Pass 2, record: stop the companion (Ctrl+C in T1), then **Record procedure
  R**, and repeat the same scenario in A (P is not needed).

**Record procedure R** (name = the step ID, e.g. `m2-C04`):

```
mkdir -p reports
uv run python tools/record_trace.py reports/NAME.jsonl     # T1; leave running
# ... repeat the scenario in A, then Ctrl+C the recorder ...
uv run python tools/replay_trace.py reports/NAME.jsonl --respond none
```

The replay prints a timeline (times relative to the first event) and a JSON
summary (`invitations`, `first_invitation_s`, seconds per activity state).
Run the replay in the shell that has the C02 exports: the tool reads the same
`BREAKROOM_*` variables as the companion. If `first_invitation_s` looks like
the 60 s default where 20 is expected, the exports are missing from that shell.

**Every prompt you send** contains the marker `ZEBRA-7731`, and every Bash
command you ask for contains it too, so step M2-C16 can grep for it.

**Between steps.** After any invitation (accepted, declined, timed out) the
cooldown (45 s here) runs. A "no invitation" result inside that window proves
nothing: wait 45 s after the last invitation before each step. Start each step
with the window idle.

**Traces are private until reviewed.** Traces hold normalized events only, but
nothing is committed before M2-C16 has passed on it. `reports/` is never
committed, and neither is any unreviewed trace.

## Setup

**M2-C01 — Install and automated checks.**
`uv sync && uv run ruff format --check . && uv run ruff check . && uv run mypy packages apps tests tools && uv run pytest`
Expect: all green.

**M2-C02 — Demo thresholds.** In T1 (and only there), before starting the
companion:

```
export BREAKROOM_ACTIVITY_THRESHOLD_S=20
export BREAKROOM_MIN_TOOL_EVENTS=2
export BREAKROOM_COOLDOWN_S=45
export BREAKROOM_SUGGESTION_TIMEOUT_S=30
export BREAKROOM_STALE_AFTER_S=30
export BREAKROOM_WAITING_MAX_S=90
```

Defaults: 60 / 2 / 900 / 60 / 180 / 300. Not changed here (defaults apply):
`BREAKROOM_OPEN_TOOL_MAX_S` (1800), `BREAKROOM_FORGET_AFTER_S` (7200),
`BREAKROOM_TASK_EVIDENCE_WAIT_S` (300). Values
must be positive integers; anything else makes the companion refuse to start.
**Restart the companion after changing any of these**: they are read once at
startup. Export them in the replay shell too (see above).
Expect: nothing visible yet.

**M2-C03 — Fresh state directory and companion start.**

```
ls -ld ~/.local/state/breakroom 2>&1
rm -rf ~/.local/state/breakroom        # Breakroom's own directory only
.venv/bin/breakroom-companion
```

(Run the venv script directly, not `uv run`.) In the window, uncheck then
re-check "Suggest breaks" once; this forces the first save. Then in another
terminal: `ls -la ~/.local/state/breakroom`
Expect: a `Breakroom` window with the idle text; the directory shows
`drwx------` and contains `companion-state.json` with `-rw-------`. Start T3
and T2 sessions as described above (T2 is restarted per step where the
allow-list differs). Ask P for `breakroom_status`: `companion` is `running`,
`invitation` is `idle`.

## Waiting for the user

**M2-C04 — Permission wait (trace).** In T2 (default permission mode, no
allow-list) send: `ZEBRA-7731: run the Bash command "sleep 5 && echo
ZEBRA-7731"`. When the permission prompt appears, **leave it open for 40 s**
(past the 20 s threshold, before the 90 s wait limit). During that time ask P
for `breakroom_status`.
Expect: no invitation in the window; P shows `activity: waiting_input` and
`invitation: idle`. Then approve the prompt; the command runs and Claude
answers. Record pass: R as `m2-C04`, same scenario.
Replay expect: 0 invitations; `waiting_input` seconds greater than 0.

**M2-C05 — Denied prompt (trace).** Same prompt as M2-C04 (default mode).
When the prompt appears, answer **No** (deny). Let Claude respond until it
stops. Then ask P for `breakroom_status` after 10 s more.
Expect: no invitation; P does not show `waiting_input` once Claude has
responded after the denial. Record pass: R as `m2-C05`, same scenario; open
the trace and write down, in order, the event types after the
`waiting_input` line (this answers whether any hook fires on denial).
Replay expect: 0 invitations; the replay timeline leaves `waiting_input`
(into `idle` or `unknown`), it does not end the trace still waiting.

**M2-C06 — Approved long tool, not pre-allowed (trace).** Default mode. Send:
`ZEBRA-7731: run the Bash command "sleep 60 && echo ZEBRA-7731"`. Approve when
asked, then watch the window for the full 60 s.
Expect: **no invitation** while the command runs (documented limitation: no
hook fires on approval, so the session stays `waiting_input` until the tool
completes, M2 README). After the command finishes, the window stays idle.
Record pass: R as `m2-C06`, same scenario.
Replay expect: 0 invitations.

**M2-C07 — Same long tool, pre-allowed (trace).** Restart T2 with
`--allowedTools "Bash(sleep:*)"`. Send the same prompt as M2-C06 (no prompt
appears).
Expect: an invitation appears in the window about 20 s into the sleep (during
the silence, not at completion). Click Decline. Record pass: R as `m2-C07`,
same scenario, no clicking.
Replay expect: 1 invitation with `--respond none`, near `first_invitation_s`
20 (see the note on replay thresholds).

**M2-C08 — Cancelled tool, Esc (trace).** T2 with the pre-allow flag. Send
`ZEBRA-7731: run the Bash command "sleep 90"`. After about 5 s press **Esc**.
Then watch the window for 40 s without typing.
This step is a **measurement**, not a pass/fail on invitations. Docs say a
cancelled tool fires no completion hook and `Stop` does not run on a user
interrupt, so Breakroom may still see an open tool. The M2 README accepts an
invitation here as a documented false positive. Write down:

- whether an invitation appeared while nothing was running (expected by the
  README: possible; expected by the ticket: none; record which happened),
- whether a `Stop` line (type `turn_stopped`) is in the trace after the Esc.

Record pass: R as `m2-C08`, same scenario. Replay shows the same invitation
(or none) as the live run.
Expect: step passes if both observations are recorded and the live and replay
outcomes agree. Mark `fail` only if the companion misbehaves (crash, stuck
window, invitation that cannot be answered).

## Failure, stop

**M2-C09 — Tool failure (trace).** T2 with the pre-allow flag. Send
`ZEBRA-7731: run the Bash command "ls /nonexistent-ZEBRA-7731"` and let
Claude answer. In the window nothing is expected. After the answer, wait 40 s
(longer than `BREAKROOM_STALE_AFTER_S`), then ask P for `breakroom_status`.
Expect: no invitation. Record pass: R as `m2-C09`, same scenario.
Replay expect: 0 invitations; the aggregate activity at the end of the replay
is `idle` or `unknown`, never `active` (the `active` seconds in the summary
are small, about the length of the turn).

**M2-C10 — Stop never says finished.** After any turn ends (M2-C09 or
M2-C07), look at the window text, the T1 terminal output, and P's raw
`breakroom_status` result.
Expect, by eye: none of them contains the words "finished", "done",
"complete", or "completed" in any form. The window shows only the idle text
or an invitation; `invitation` is one of the known state words. In the
M2-C09 replay timeline, no transition label claims completion either.

## Several sessions

**M2-C11 — One waits, one works.** Companion running. Three terminals with
Claude: A (default mode, no allow-list), B (pre-allow flag). First, in A,
send `ZEBRA-7731: run the Bash command "sleep 5 && echo ZEBRA-7731"` and
leave the prompt open. Within 5 s, in B send `ZEBRA-7731: run the Bash
command "sleep 100"`. Watch the window for 40 s.
Expect: **no invitation** while A's prompt is open (B alone would invite at
20 s). Then approve A's prompt, and keep watching.
Expect: an invitation appears within about 10 s of approving (B is still
sleeping; A's command finishes in 5 s). Do this inside A's 90 s wait limit;
if A passed 90 s first, redo the step (A would be `unknown`, which proves
nothing). Decline the invitation. Record pass: skip (the recorder cannot tell
sessions apart in a way that adds evidence); the two-session timeline is
covered by synthetic scenario 9 in the replay regression set.

## Range label

**M2-C12 — Task list, label shape.** T2 with the pre-allow flag. Send:
`ZEBRA-7731: create a task list with 4 tasks and do them one at a time; each
task is the Bash command "sleep 15".` Watch the window for the whole run.
Expect: either **no invitation** or an invitation with one of the labels
`~5–10 min left`, `~10–20 min left`, `20+ min left`, or no label at all.
Never any text containing "under 5" or "<5", and never a label on the idle
window. (Tasks that finish in 15 s each estimate well under 5 minutes, so the
expected live result is no invitation; that is the per-candidate veto working.
The range labels themselves are covered by synthetic scenario 11 and the
window unit test, not by a two-minute live run.) If the task tools do not
exist in your Claude Code version, mark `blocked`.

## Persistence

**M2-C13 — Decline survives a restart.** Stop the companion. Set
`export BREAKROOM_COOLDOWN_S=300` in T1 and start it (restart after changing
env). Run a pre-allowed `sleep 60` as in M2-C07 and click **Decline** when the
invitation appears. Ctrl+C the companion, check
`cat ~/.local/state/breakroom/companion-state.json`, restart it with the same
exports, and run `sleep 60` again for 40 s.
Expect: the file is JSON with `"enabled": true` and a non-null
`"cooldown_until"` in the future; after the restart, **no invitation** during
the second run.

**M2-C14 — Disable survives a restart.** Uncheck "Suggest breaks". Ctrl+C,
restart.
Expect: the window starts with the disabled text and the box unchecked; P
shows `invitation: disabled`; a pre-allowed `sleep 60` brings no invitation.
Re-check the box: window returns to idle, and the state file shows
`"enabled": true`.

**M2-C15 — Corrupt state starts off.** Ctrl+C the companion, then
`echo 'not json' > ~/.local/state/breakroom/companion-state.json` and start
it.
Expect: the window starts disabled (unchecked box), no crash, and no output
containing the file content. Fix up: Ctrl+C, `rm
~/.local/state/breakroom/companion-state.json`, `unset
BREAKROOM_COOLDOWN_S` and re-export `BREAKROOM_COOLDOWN_S=45`, restart.
Expect: starts enabled (missing file means defaults).

## Privacy and report

**M2-C16 — Traces contain no task content.** Stop the companion and
recorder. Run:

```
for f in reports/m2-C*.jsonl; do
  echo "$f: $(wc -l < "$f") lines, hits: $(grep -ciE "ZEBRA-7731|sk-test-DO-NOT-LEAK|needs your permission|permission to use|sleep|nonexistent|$(basename "$PWD")|$HOME" "$f")"
done
```

(To include a secret-looking string, send one extra prompt in a recorded pass,
such as "My key is sk-test-DO-NOT-LEAK-12345" with a Bash command containing
it.) Also open one trace and read a few lines.
Expect: every file has lines (events were recorded) and `hits: 0`, i.e. none
of: the prompt marker, the secret string, the permission notification text,
the command text, the project directory name, your home path. Only normalized
event fields and a hashed session key appear. Any hit is a `fail`: delete that
trace and report it, do not commit it.

**M2-C17 — Report and reviewed traces.** For each trace that passed M2-C16
and that you want to keep, copy it by scenario name:

```
mkdir -p tests/fixtures/traces/recorded
cp reports/m2-C04.jsonl tests/fixtures/traces/recorded/permission-wait.jsonl
```

(and likewise `denied-prompt`, `approved-long-tool`, `preallowed-long-tool`,
`esc-cancel`, `tool-failure`). Copy only after M2-C16; a trace that was not
grepped is not copied. Do not commit `reports/`. Then fill in the report and
run:

```
uv run pytest --junitxml=reports/m2-junit.xml
```

Fill in `reports/m2-report.json` from the template below, using `pass`, `fail`
or `blocked` per step. Also note platforms still pending (native Windows,
macOS, Linux without WSL), per §14.

```json
{
  "date": "YYYY-MM-DD",
  "claude_code_version": "output of `claude --version`",
  "platform": "WSL2 Linux, kernel version, WSLg yes/no",
  "steps": {
    "M2-C01": {"result": "pass", "note": ""},
    "M2-C02": {"result": "pass", "note": ""},
    "M2-C03": {"result": "pass", "note": ""},
    "M2-C04": {"result": "pass", "note": ""},
    "M2-C05": {"result": "pass", "note": ""},
    "M2-C06": {"result": "pass", "note": ""},
    "M2-C07": {"result": "pass", "note": ""},
    "M2-C08": {"result": "pass", "note": ""},
    "M2-C09": {"result": "pass", "note": ""},
    "M2-C10": {"result": "pass", "note": ""},
    "M2-C11": {"result": "pass", "note": ""},
    "M2-C12": {"result": "pass", "note": ""},
    "M2-C13": {"result": "pass", "note": ""},
    "M2-C14": {"result": "pass", "note": ""},
    "M2-C15": {"result": "pass", "note": ""},
    "M2-C16": {"result": "pass", "note": ""},
    "M2-C17": {"result": "pass", "note": ""}
  },
  "observations": {
    "events_after_denial": ["event types in order, from the M2-C05 trace"],
    "esc_invitation_appeared": false,
    "esc_stop_event_seen": false
  },
  "pending_platforms": ["native Windows", "macOS", "Linux without WSL"]
}
```

M2 gate (§14): short, long, waiting and stop traces are classified sensibly
and nothing declares the task finished. Passing means M2-C04, M2-C06,
M2-C07, M2-C09, M2-C10, M2-C11 and M2-C16 pass, M2-C05 and M2-C08 are
recorded, and the persistence steps M2-C13 to M2-C15 pass.
