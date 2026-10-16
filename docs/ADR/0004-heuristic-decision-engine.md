# ADR 0004: Heuristic decision engine only

Status: Accepted for M2 (CONFIRMED 2026-10-12). Calibration of the estimate is
still open. Source: ARCHITECTURE §7, §16 items 4 and 5; M2 README.

## Context
The companion decides when to offer a break. A local model is optional and
never required.

## Decision
The heuristic engine is the only engine. Nothing counts as a whole-task finish;
`TaskCompleted` is a hint. With several sessions, invite only if one qualifies
and none waits for input. The invitation shows the remaining-time bucket as a
range, nothing when unknown.

## Alternatives
A local model scoring candidate breaks (§7.2). Candidates and resource budgets
are open (§16 item 5); no model weights are bundled.

## Consequences
No model dependency. A `Stop` event is never read as task completion.

## How it is tested
A synthetic trace regression set gates the build (M2); a dev tool replays real
recordings.

## Rollback
Not recorded. §7.2 defines one validated result type for any engine, so a
second engine could sit behind the same interface.
