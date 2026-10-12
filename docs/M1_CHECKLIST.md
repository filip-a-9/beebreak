# M1 manual validation checklist

Spec refs: ARCHITECTURE §13 (engineering targets), §14 (M1 gate), §17 (M1 exit).
Platform under test: WSL2 Linux, Claude Code, companion window via WSLg.
Run every step from the repository root. Record results in the report
template at the end. Report blocked platforms as pending, not as passed.

Step IDs are stable; do not renumber.

This checklist now runs on M2 behavior: `BREAKROOM_QUIET_GAP_S` is gone, and
a session stays active through silence until `BREAKROOM_STALE_AFTER_S`
(default 180 s). Other M2 variables (`BREAKROOM_STALE_AFTER_S`,
`BREAKROOM_WAITING_MAX_S`, `BREAKROOM_OPEN_TOOL_MAX_S`,
`BREAKROOM_FORGET_AFTER_S`) keep their defaults here; see
`docs/M2_CHECKLIST.md`. Restart the companion after changing any variable.
Suggestion and cooldown state now persist in
`~/.local/state/breakroom/companion-state.json`; remove it for a clean run.

## Setup

**M1-C01 — Install.** `uv sync`
Expect: exits 0, `.venv/` exists.

**M1-C02 — Automated checks.**
`uv run ruff format --check . && uv run ruff check . && uv run mypy packages apps tests tools && uv run pytest`
Expect: all green.

**M1-C03 — Demo thresholds.** In the terminal that will start the companion
(and only there), shorten the trigger so a demo takes seconds, not minutes:

```
export BREAKROOM_ACTIVITY_THRESHOLD_S=15
export BREAKROOM_MIN_TOOL_EVENTS=2
export BREAKROOM_COOLDOWN_S=90
export BREAKROOM_SUGGESTION_TIMEOUT_S=60
```

Defaults are 60 / 2 / 900 / 60. Values must be positive. Unset them (or
use a new shell) for step M1-C13.
Expect: nothing visible yet.

## MCP status

**M1-C04 — Plugin loads, companion down.** With no companion running:
`claude --plugin-dir integrations/claude-code`, then `/mcp`.
Expect: server `breakroom` is listed and connected.

**M1-C05 — Status with companion down.** Ask Claude: "Call the
breakroom_status tool and show the raw result."
Expect: `companion` is `not_running`, `invitation` and `last_event_at` are
`null`, and nothing else is in the result.

**M1-C06 — Companion starts.** In a second terminal (with the M1-C03 exports):
`.venv/bin/breakroom-companion`
(Run the venv script directly, not `uv run`: `uv run` keeps a parent process,
and the freeze steps below must target the companion itself.)
Expect: a small always-on-top window titled `Breakroom` appears under WSLg,
showing the idle text, with the "Suggest breaks" checkbox checked and no
Accept/Decline buttons.

**M1-C07 — Status with companion up.** In the Claude session, call
`breakroom_status` again.
Expect: `companion` is `running`, `invitation` is `idle`.

**M1-C08 — Second companion refused.** In a third terminal:
`.venv/bin/breakroom-companion; echo "exit=$?"`
Expect: one short line on stderr, `exit=2`, the first window unaffected.

## Events reach the window

**M1-C09 — One invitation after sustained work.** In the Claude session,
give a task with many tool calls over more than 15 s, for example: "Read each
file under packages/ one at a time and summarise each in one sentence."
Expect: status moves from idle to a suggested break exactly once while Claude
works; the window raises itself and shows Accept and Decline. Claude's output
shows no errors.

**M1-C10 — Accept.** Click Accept.
Expect: window returns to idle, buttons disappear.

**M1-C11 — No second invitation inside the cooldown.** Within 90 s of M1-C10
repeat a long task as in M1-C09.
Expect: no invitation appears while the cooldown runs.

**M1-C12 — Decline, and invitation after cooldown.** Wait out the cooldown,
repeat the long task, and this time click Decline.
Expect: an invitation appears again, Decline returns the window to idle, and
a further long task inside the new cooldown shows nothing.

**M1-C13 — Short task shows nothing.** With the companion restarted under
default thresholds (no exports), ask a trivial question such as "What is 2+2?"
Expect: no invitation.

**M1-C14 — Disable.** Uncheck "Suggest breaks" in the window, then run a long
task (companion with the M1-C03 exports).
Expect: window shows the disabled text, no invitation, and `breakroom_status`
reports `invitation` as `disabled`. Re-check the box: window returns to idle.

## Fail open

**M1-C15 — Companion stopped.** Close the companion (Ctrl+C in its terminal;
it quits within about a second and removes its socket file).
In Claude, run a task with several tool calls.
Expect: Claude behaves as normal, with no visible delay, hook error, or
stderr/stdout noise. `breakroom_status` reports `not_running`; no leftover
socket file prevents the next start (restart the companion: it starts).

**M1-C16 — Companion frozen.** Start the companion, then freeze it:
`PID=$(pgrep -f '[.]venv/bin/breakroom-companion'); echo $PID; kill -STOP $PID`
(exactly one PID must print; the brackets stop the pattern matching the shell
running it). Run a task with several tool calls in Claude, and call
`breakroom_status`.
Expect: Claude keeps working without visible delay or errors; the status call
returns `not_running` within about a second rather than hanging. Afterwards
`kill -CONT $PID` (or kill it) to clean up.

## Privacy

**M1-C17 — Trace contains no secrets.** Stop the companion. Start the
trace recorder, which stands in for the companion:

```
mkdir -p reports
uv run python tools/record_trace.py reports/m1-trace.jsonl
```

In a Claude session, send a prompt such as: "My key is
sk-test-DO-NOT-LEAK-12345; read README-like files and list them." Let it run
a few tool calls, include a Bash command containing the same string, then
stop the recorder with Ctrl+C.

```
grep -c "sk-test-DO-NOT-LEAK" reports/m1-trace.jsonl
grep -c "$(basename "$PWD")" reports/m1-trace.jsonl
wc -l reports/m1-trace.jsonl
```

Expect: first count `0`, second count `0` (no project path or directory
name), and the file has lines (events were recorded). Also eyeball a few
lines: only normalized event fields, hashed session key, no raw session ID.
Review before committing any trace anywhere.

## Latency

Run each with the same command; `--runs` defaults to 50 and
`--timeout-s` to 5 (also `BREAKROOM_LATENCY_RUNS`,
`BREAKROOM_LATENCY_TIMEOUT_S`). Output is JSON with `runs`, `p50_ms`,
`p95_ms`, `max_ms`, `nonzero_exits`, `stdout_bytes`.

For M1-C18 to M1-C20, start the companion **without** the M1-C03 demo
exports: the tool sends 50 synthetic tool events, which can raise an
invitation under the short demo thresholds (dismiss it if one appears).

**M1-C18 — Companion absent.** Companion not running:
`uv run python tools/measure_hook_latency.py`
Expect: `nonzero_exits` 0 and `stdout_bytes` 0. Record p50/p95/max.

**M1-C19 — Companion present.** Companion running:
`uv run python tools/measure_hook_latency.py`
Expect: `nonzero_exits` 0, `stdout_bytes` 0. Record numbers.

**M1-C20 — Companion frozen.** Freeze it as in M1-C16 (`kill -STOP $PID`), then run the
same command.
Expect: `nonzero_exits` 0, `stdout_bytes` 0, and `max_ms` close to the hook's
send timeout (`BREAKROOM_HOOK_SEND_TIMEOUT_S`, default 0.2 s = 200 ms) plus
interpreter start-up, not seconds. Then `kill -CONT $PID`. No target numbers are
fixed yet (spec §13): these are the first measurements, so record them and
judge afterwards.

## Report

Copy the template to `reports/m1-report.json` and fill it in. Use
`pass`, `fail` or `blocked` for each step. Then produce the automated test
report:

```
uv run pytest --junitxml=reports/m1-junit.xml
```

`reports/` is not committed (generated output; traces may contain session
data). Also note platforms still pending (native Windows, macOS, Linux
without WSL), per §14.

```json
{
  "date": "YYYY-MM-DD",
  "claude_code_version": "output of `claude --version`",
  "platform": "WSL2 Linux, kernel version, WSLg yes/no",
  "steps": {
    "M1-C01": {"result": "pass", "note": ""},
    "M1-C02": {"result": "pass", "note": ""},
    "M1-C03": {"result": "pass", "note": ""},
    "M1-C04": {"result": "pass", "note": ""},
    "M1-C05": {"result": "pass", "note": ""},
    "M1-C06": {"result": "pass", "note": ""},
    "M1-C07": {"result": "pass", "note": ""},
    "M1-C08": {"result": "pass", "note": ""},
    "M1-C09": {"result": "pass", "note": ""},
    "M1-C10": {"result": "pass", "note": ""},
    "M1-C11": {"result": "pass", "note": ""},
    "M1-C12": {"result": "pass", "note": ""},
    "M1-C13": {"result": "pass", "note": ""},
    "M1-C14": {"result": "pass", "note": ""},
    "M1-C15": {"result": "pass", "note": ""},
    "M1-C16": {"result": "pass", "note": ""},
    "M1-C17": {"result": "pass", "note": ""},
    "M1-C18": {"result": "pass", "note": ""},
    "M1-C19": {"result": "pass", "note": ""},
    "M1-C20": {"result": "pass", "note": ""}
  },
  "latency": {
    "absent": {"runs": 0, "p50_ms": 0, "p95_ms": 0, "max_ms": 0, "nonzero_exits": 0, "stdout_bytes": 0},
    "present": {"runs": 0, "p50_ms": 0, "p95_ms": 0, "max_ms": 0, "nonzero_exits": 0, "stdout_bytes": 0},
    "frozen": {"runs": 0, "p50_ms": 0, "p95_ms": 0, "max_ms": 0, "nonzero_exits": 0, "stdout_bytes": 0}
  },
  "pending_platforms": ["native Windows", "macOS", "Linux without WSL"]
}
```

M1 exit (spec §17): a task is observed, one invitation may appear, accept and
decline work, and a missing or crashed companion cannot block Claude or
export raw task content. Passing means M1-C09 to M1-C12, M1-C15, M1-C16 and
M1-C17 all pass.
