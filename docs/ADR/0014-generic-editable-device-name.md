# ADR 0014: Generic, editable device name

Status: Accepted (CONFIRMED 2026-10-16, M6; built in M6-06). Source: M6
README, M6-06.

## Context
A device name identifies a sign-in in the `devices` table. The machine hostname
can identify a person.

## Decision
The default name is `WSL device`. The user can edit it before signing in and
can open the privacy preview from the code page before it is sent ("Sent
when you sign in"). Once signed in, the preview says the name was chosen at
sign-in, because the companion does not store it. The hostname is never sent. Renaming after sign-in is out of scope because the server has no
rename route.

## Alternatives
Using the machine hostname as the name. Not chosen, because it can identify a
person.

## Consequences
Device names carry no hostname. Two devices may share the default name.

## How it is tested
`tests/unit/test_device_name.py`: an injected hostname canary must appear in no
captured request.

## Rollback
Not recorded.
