# ADR 0019: Rename Breakroom to Beebreak

Status: Accepted (the maintainer, 2026-10-18).
Source: PR #15.

## Context
"BREAKROOM CHAT" is a pending US trademark application for downloadable
workforce-communication software (serial 99757490, classes 9 and 42), and an
App Store app ships under the same name. Nothing is deployed and there are no
users, so a rename now costs only the code change.

## Decision
The product is Beebreak: packages `beebreak-*` / `beebreak_*`, the
environment prefix `BEEBREAK_`, the plugin, marketplace and MCP tool names,
the keyring service, runtime, state and config paths, Docker and Compose names.
No compatibility shims.

## Alternatives
Brewlet: free on PyPI and in TMview, but Microsoft publishes a developer tool
named Brewlet and a Homebrew menu-bar app has the name; "brew" reads as
Homebrew in developer tooling. Names with Bramble (the protocol under the
Briar messenger), Burble (near the pending mark BLURBLE for social apps) and
Bumble (a famous chat-app mark) were rejected for the same kind of overlap.

## Consequences
Beebreak: free on PyPI and GitHub, no product found by web search (nearest
mark BEEBLE, app software), no rows in a TMview "contains" search of the EU
(EM) register (2026-10-18). A registry search is not a legal
clearance; a lawyer review would cover it and is not planned (ADR 0021). Keeps the B-shaped logo.
Privacy corpus v1 keeps its old marker as a frozen fixture; v2 uses
`beebreak-canary`.

## How it is tested
The full suite and CI; a fresh install from release wheels (checklist C07 to
C10).

## Rollback
Another rename of the same shape.
