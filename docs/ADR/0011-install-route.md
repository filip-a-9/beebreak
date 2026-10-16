# ADR 0011: Plugin marketplace and `uv tool install` for install

Status: Accepted (CONFIRMED 2026-10-16, M6; built in M6-02, not yet in this
tree). Source: M6 README; ARCHITECTURE §2, §3.1.

## Context
The plugin and the companion are separate pieces (product decision 11). The
current plugin only works from a repo checkout.

## Decision
The plugin comes from a marketplace in this repo. The companion installs with
`uv tool install` from a git tag. `install` and `uninstall` commands also
remove the socket, state, credentials and autostart.

## Alternatives
Signed per-OS companion builds are noted as later work (§2); not part of M6.

## Consequences
Uninstall is complete by design, which the data documents rely on.

## How it is tested
Specified in M6-02 and the M6 validation checklist.

## Rollback
Not recorded.
