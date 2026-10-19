# ADR 0009: Caddy terminates TLS

Status: Accepted (CONFIRMED 2026-10-16, M6; built in M6-03, in the tree as
`Caddyfile` and `compose.prod.yaml`). Source: M6 README.

## Context
Until M6 the companion talks plain HTTP/WS and only to loopback. Any other host
needs `https://`.

## Decision
Caddy terminates TLS in a production compose file. Revised 2026-10-17: there
is no LAN run. TLS is checked by each operator on their own host, together with
the M4/M5 two-machine checklist items.

## Alternatives
None recorded in the M6 decision table.

## Consequences
The server sits behind a proxy, so forwarded headers need a trust rule (ADR
0015). Hosting is the operator's choice.

## How it is tested
Manual check by the operator on the real host (RUNBOOK, "Untested on a real host").

## Rollback
Not recorded.
