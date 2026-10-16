# ADR 0015: Forwarded headers only from configured proxies

Status: Accepted (CONFIRMED 2026-10-16, M6; built in M6-03, not yet in this
tree). Source: M6 README.

## Context
Behind Caddy, the client address comes from forwarded headers. Per-IP rate
limits depend on it.

## Decision
Forwarded headers are honoured only from the Caddy container's network, set by
`BREAKROOM_TRUSTED_PROXIES` (a CIDR list). Empty means trust none, which is the
default.

## Alternatives
None recorded.

## Consequences
A deployment without the setting ignores forwarded headers.

## How it is tested
Specified in M6-03.

## Rollback
Set the variable empty.
