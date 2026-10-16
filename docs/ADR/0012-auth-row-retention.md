# ADR 0012: Retention periods for sign-in rows

Status: Accepted (CONFIRMED 2026-10-16, M6; built in M6-04). Source: M6
README, M6-04.

## Context
Sign-in challenges, refresh tokens and invites hold an email address or a
credential hash. Each needs a stated retention period.

## Decision
`auth_challenges` are deleted 24 h after use or expiry
(`BREAKROOM_CHALLENGE_RETENTION_S`). `refresh_tokens` are deleted 7 days
after they expire, used or not (`BREAKROOM_REFRESH_TOKEN_RETENTION_S`). Expired, redeemed
or revoked `beta_invites` are deleted after 30 days
(`BREAKROOM_INVITE_RETENTION_S`). An unexpired token is never deleted.
The purge runs in the matchmaker's existing purge pass.

## Alternatives
Deleting used tokens 7 days after use (the first M6 proposal). Rejected in
review: reuse detection needs the used row, so a replay between day 7 and the
token's expiry would no longer revoke the device.

## Consequences
A spent token's row lives until 7 days after its expiry. An expired token is
refused before reuse detection runs, so detection covers the token's whole
lifetime, at the cost of keeping small rows for up to the token lifetime
plus 7 days.

## How it is tested
Boundary tests at minus and plus one second for each rule
(`tests/server/test_retention_auth.py`).

## Rollback
All three are environment-configurable and validated positive.
