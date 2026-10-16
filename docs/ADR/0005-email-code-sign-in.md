# ADR 0005: Self-hosted email-code sign-in

Status: Accepted (CONFIRMED 2026-10-13, M3). Email change stays open.
Source: ARCHITECTURE §2, §16 item 6; THREAT_MODEL T13 to T22.

## Context
The beta is invite-only and needs access control without a mandatory GitHub
identity.

## Decision
An admin CLI creates an invite bound to an email. The user gets an 8-character
one-time code by email and types it into the companion. Access tokens are
opaque (15 min); refresh tokens rotate (30 days). Both are stored hashed on the
server. Reuse of a spent refresh token revokes the device.

## Alternatives
An external identity provider; email links; URL-scheme handoff from a desktop
app. The architecture chose no identity vendor and no links or URL-scheme
handoff.

## Consequences
Revocation is a row update. Anyone who can read the mailbox can sign in as that
user (out of scope in THREAT_MODEL). Account deletion and export are covered by
ADR 0016.

## How it is tested
Auth, token flow, realtime and rate-limit tests (M3); checklist M3-C07, C09.

## Rollback
Not recorded.
