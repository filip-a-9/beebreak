# ADR 0008: In-process matcher, uniqueness in the database

Status: Accepted (CONFIRMED 2026-10-13, M4). Source: ARCHITECTURE §6.3, §9.3,
§16 item 11; THREAT_MODEL T26, T33.

## Context
Two users must never be matched twice and one user never in two matches, even
across restarts and races.

## Decision
A single in-process matcher runs on every join and on a 1 s sweep. Uniqueness
(one queued entry per user, one active participant row per user) is enforced by
partial unique indexes on PostgreSQL and SQLite. The matcher lock is only a
performance aid. Only users with a live WebSocket are paired. A search lasts
90 s (`queue_ttl_s`). Blocks are stored by account ID; unblocking is by admin
only.

## Alternatives
Redis or pub/sub for several instances; deferred until more than one instance
is needed (§2).

## Consequences
Single server instance for now. A second process loses at the index and the
pair is skipped.

## How it is tested
`test_matchmaking_races.py`, `test_models_matchmaking.py` and
`tests/e2e/test_two_companions.py`. Postgres race tests run when
`BREAKROOM_TEST_DATABASE_URL` is set.

## Rollback
Not recorded.
