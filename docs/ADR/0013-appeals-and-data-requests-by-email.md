# ADR 0013: Appeals and data requests by email

Status: Accepted (CONFIRMED 2026-10-16, M6). Source: M6 README, M6-09;
ARCHITECTURE §10, §11.

## Context
The beta needs an appeal path for disabled accounts and a way to answer access
and erasure requests. Moderation is the admin CLI only.

## Decision
One published email contact, `<contact address>`, handles appeals, export and
deletion. The response time is 14 days. Operators use the admin CLI
(`report`, and `account export` / `account delete`). There is no in-app UI.

## Alternatives
An in-app appeal or data-request UI; not built.

## Consequences
Requests depend on a human reading mail. The address is a placeholder until
the operator supplies it.

## How it is tested
Not by code. The M6 validation checklist covers the documents and the CLI.

## Rollback
Not recorded.
