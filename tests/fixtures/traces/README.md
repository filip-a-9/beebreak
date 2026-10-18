# Event traces

Traces are JSON Lines files of `NormalizedEvent`, recorded from a real Claude
Code session, used later to replay and tune the trigger.

## Recording

1. Make sure the companion is not running (the recorder refuses to start if it
   is, because both bind the same socket).
2. In one WSL terminal: `uv run python tools/record_trace.py my-trace.jsonl`
3. In another terminal, use Claude Code as usual with the Beebreak hooks
   installed. Each hook event is appended as one line.
4. Stop the recorder with Ctrl-C. It removes its socket on exit.

## Before committing

Traces contain only normalized events (ids, ephemeral session key, type,
timestamp, coarse tool class), but open the file and check it anyway. Do not
commit a trace you have not read. Name files by scenario, e.g.
`long-refactor.jsonl`.

## Replaying a trace

```
uv run python tools/replay_trace.py my-trace.jsonl [--respond none|accept|decline]
```

The tool feeds the events to the companion's `Controller` with a virtual clock
(no waiting) and prints each change of invitation, activity and estimate with
times relative to the first event, then one JSON summary line: `invitations`,
`first_invitation_s`, seconds per activity state (`activity_s`) and
`invalid_lines`. Invalid lines are counted and skipped, never printed. The
`BEEBREAK_*` trigger variables apply to replays exactly as to the companion.
`--respond` answers each suggestion at the next virtual second.

## Synthetic and recorded traces

`synthetic.py` builds the 17 scenarios of the M2 regression set
(`tests/regression/test_replay.py`) with explicit timestamps. Real recordings
go in `tests/fixtures/traces/recorded/` only after you have read them (see
above); the regression set does not depend on them.

## Recorded traces

- `esc-cancel.jsonl`: the M2 recording of Esc on a tool (M2-C08).
- `esc-cancel-running-tool.jsonl`: recorded 2026-10-17 with Claude Code
  2.1.302 on Haiku. A foreground Bash `ping`, Esc pressed 5 s into it. Claude
  Code showed "Interrupted" but sent no event after the Esc (no tool
  completion, no `turn_stopped`, no idle event) until the session closed about
  95 s later. Replayed in
  `tests/regression/test_recorded_esc_cancel_running_tool.py`: a lone running
  tool never invites, and the session is unknown by 600 s.
