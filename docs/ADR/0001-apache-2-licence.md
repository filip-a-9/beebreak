# ADR 0001: Apache-2.0 licence

Status: Accepted (CONFIRMED 2026-10-12). Source: ARCHITECTURE §1, §2, §16 item 9.

## Context
The project prefers public source code with a restricted hosted beta. Open
source and access to the hosted network are separate decisions.

## Decision
The source is licensed under Apache-2.0 (`LICENSE`).

## Alternatives
MIT and AGPL were the other candidates named in §16 item 9.

## Consequences
Permissive distribution with explicit patent terms. Dependency compliance
review still applies (for example PySide6 is LGPL). The project takes no CLA
(see `CONTRIBUTING.md`), so contributions stay under the same licence.

## How it is tested
Not tested by code. `LICENSE` is present and each package manifest declares
`Apache-2.0`.

## Rollback
Not recorded. A change of licence would need a new ADR.
