# ADR 0016: Account export and delete semantics

Status: Accepted (CONFIRMED 2026-10-16, M6; built in M6-04). Source: M6
README, M6-04.

## Context
Users can ask for their data or its erasure. Reports about a user contain data
about the reporter, which is third-party data.

## Decision
`beebreak-admin account export <email>` writes the user's own rows as JSON.
Reports filed against the user appear only as dates and outcome, with no
reporter identity, note or evidence. The export never includes code, token or
access hashes, or a peer's message body. `account delete <email> --yes` is a
hard delete, refused while the user is queued, in a live match, or on either
side of an open report (resolve first), and refused for a disabled account
unless `--disabled-ok` is given. Devices are revoked first, then busy is
checked again before anything is deleted, so a match cannot start in between. Where the other participant remains, the
match is kept and only this user's participant row and messages go.

## Alternatives
An in-app UI (see ADR 0013). Deleting open reports the user filed together
with the account (rejected in review: it destroys evidence about a third
party). Anonymising them instead needs a schema change and is not done.

## Consequences
Deletion can be delayed by an open report. A deleted disabled account leaves
no record of the ban, which is why `--disabled-ok` is an explicit operator
choice. The privacy notice says both.

## How it is tested
`tests/server/test_account_data.py` and additions to `test_admin.py`.

## Rollback
Not recorded; a hard delete cannot be undone.
