# ADR 0013: Appeals and data requests by email

Status: Accepted (CONFIRMED 2026-10-16, M6). Amended by
[ADR 0021](0021-no-hosted-instance.md): the contact is the operator's, not the
maintainer's. Source: M6 README, M6-09; ARCHITECTURE §10, §11.

## Context
A server needs an appeal path for disabled accounts and a way to answer access
and erasure requests. Moderation is the admin CLI only.

## Decision
Each operator publishes one email contact, the `<contact address>` field in
the privacy notice, that handles appeals, export and deletion on that server.
The default response time is 14 days; an operator who changes it changes the
notice too. Operators use the admin CLI (`report`, and `account export` /
`account delete`). There is no in-app UI.

## Alternatives
An in-app appeal or data-request UI; not built.

## Consequences
Requests depend on a human reading mail. The address is a field the operator
fills in before opening the server to others (RUNBOOK, "Before you open your
server to others"). The maintainer has no address for this: the project runs no
server.

## How it is tested
Not by code. The M6 validation checklist covers the documents and the CLI.

## Rollback
Not recorded.
