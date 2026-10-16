# ADR 0009: Caddy terminates TLS

Status: Accepted (CONFIRMED 2026-10-16, M6; built in M6-03, not yet in this
tree). Source: M6 README.

## Context
Until M6 the companion talks plain HTTP/WS and only to loopback. Any other host
needs `https://`.

## Decision
Caddy terminates TLS in a production compose file. It is verified locally and
from a second machine on the LAN, which also runs the M4/M5 two-machine
checklist items.

## Alternatives
None recorded in the M6 decision table.

## Consequences
The server sits behind a proxy, so forwarded headers need a trust rule (ADR
0015). Hosting is not chosen in M6.

## How it is tested
Manual two-machine check in the M6 validation checklist.

## Rollback
Not recorded.
