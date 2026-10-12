# Event traces

Traces are JSON Lines files of `NormalizedEvent`, recorded from a real Claude
Code session, used later to replay and tune the trigger.

## Recording

1. Make sure the companion is not running (the recorder refuses to start if it
   is, because both bind the same socket).
2. In one WSL terminal: `uv run python tools/record_trace.py my-trace.jsonl`
3. In another terminal, use Claude Code as usual with the Breakroom hooks
   installed. Each hook event is appended as one line.
4. Stop the recorder with Ctrl-C. It removes its socket on exit.

## Before committing

Traces contain only normalized events (ids, ephemeral session key, type,
timestamp, coarse tool class), but open the file and check it anyway. Do not
commit a trace you have not read. Name files by scenario, e.g.
`long-refactor.jsonl`.
