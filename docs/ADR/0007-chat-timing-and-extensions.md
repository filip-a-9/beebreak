# ADR 0007: Chat timing, extensions and offline hold

Status: Accepted (CONFIRMED 2026-10-14, M5). Source: ARCHITECTURE §6.4, §6.5,
§16 item 8; THREAT_MODEL T40, T41.

## Context
The chat is a break, not a meeting. A verified task finish is not available, so
a grace period cannot start from one.

## Decision
A chat lasts 15 min (`match_ttl_s`). The grace period is the last 2 min
(`chat_grace_s`). An extension needs both people, adds 10 min
(`chat_extension_s`), at most 2 (`chat_max_extensions`). A person with no
connected device gets 60 s (`chat_disconnect_s`) before the chat ends with
reason `disconnected`. The deadline keeps running while offline. The chat shows
nothing about either person's agent.

## Alternatives
Starting the grace period from a confirmed task finish (§6.4 proposal); not
possible because nothing counts as a confirmed finish (ADR 0004).

## Consequences
A ghost chat ends by itself after at most 35 min (T41). Extensions are
adjudicated by conditional updates, so races extend once or not at all.

## How it is tested
`test_chat_api.py`, `test_matchmaking_chat.py`, `test_chat_flow.py` and the
two-companion e2e tests; checklist M5-C07 to C10.

## Rollback
All values are server configuration (`BEEBREAK_CHAT_*`, `BEEBREAK_MATCH_TTL_S`).
