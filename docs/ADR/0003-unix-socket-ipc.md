# ADR 0003: Unix domain socket for local IPC

Status: Accepted for WSL2 (CONFIRMED 2026-10-12, M1). Other platforms revisit
this. Source: ARCHITECTURE §2, §16 item 3; THREAT_MODEL T3, T4, T5.

## Context
Hooks and the MCP server talk to the companion on the same machine. Loopback
HTTP was the first proposal.

## Decision
A Unix domain socket in a `0700` runtime directory (socket file `0600`)
replaces loopback HTTP. One JSON line per connection.

## Alternatives
Authenticated loopback HTTP on a random port with a bearer token. Rejected
because WSL2 forwards `127.0.0.1` listeners to Windows. Named pipes were noted
as harder cross-platform.

## Consequences
Other users' processes cannot connect. A same-user process still can; this is
accepted (T5). Socket paths must stay under 108 bytes.

## How it is tested
Permission and fail-open IPC tests (M1-03); frame cap and timeout tests (T6).

## Rollback
Not recorded. Another transport would be a new ADR under §16 item 3.
