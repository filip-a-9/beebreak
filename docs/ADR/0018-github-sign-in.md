# ADR 0018: GitHub sign-in next to email codes

Status: Accepted (the maintainer, 2026-10-17). Extends ADR 0005.
Source: `.claude/specs/github-signin.md`; THREAT_MODEL T61 to T66.

## Context
The product ships as a Claude Code plugin from a public repository, so nearly
every user has a GitHub account. Email codes need a mail provider, a sending
domain and SPF/DKIM, and store an address.

## Decision
OAuth device flow driven by the server (client id only, no scope, no secret).
The companion shows the user code and a button that opens GitHub; the GitHub
access token never reaches it. The server uses the token for one `GET /user`,
keeps only the numeric id, and discards the token. An account is either email or
GitHub (no linking). Which methods exist is configuration: empty SMTP host
turns email off, empty client id turns GitHub off, at least one must be set.
Invites are by username (`invite create --github`); the id is resolved once and
only the id is stored. Tombstones key on `HMAC(subkey, "github:" + id)`.

## Alternatives
A web-callback OAuth flow (needs a client secret and a redirect URL, and puts
a token in a browser handoff); a token handed to the companion (rejected: the
token would need storing); linking email and GitHub accounts (deferred).

## Consequences
GitHub learns that a person signed in to this app. Whoever controls the GitHub
account can sign in. Migration 0005 cannot be downgraded while GitHub accounts
exist.

## How it is tested
A fake GitHub on a loopback port (real HTTP) in `tests/server/github_fake.py`;
`test_github_*.py` in `tests/server`, `tests/privacy/test_github_privacy.py`,
`tests/unit/test_onboarding_github.py`.

## Rollback
Empty `BEEBREAK_GITHUB_CLIENT_ID` turns the method off; existing GitHub
accounts then cannot sign in.
