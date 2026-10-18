# ADR 0010: GitHub Actions CI for `main`

Status: Accepted (CONFIRMED 2026-10-16, M6; workflow built in M6-01). Source:
M6 README.

## Context
M0 was done except CI. The checks were run by hand.

## Decision
GitHub Actions runs ruff format and check, mypy, and pytest with a Postgres
service (`BEEBREAK_TEST_DATABASE_URL`) and Qt offscreen. It gates merges to
`main` once branch protection requires the `checks` status.

## Alternatives
None recorded.

## Consequences
The Postgres race and migration tests that skip locally run in CI.

## How it is tested
The workflow runs itself on every change.

## Rollback
Not recorded.
