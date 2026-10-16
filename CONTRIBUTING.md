# Contributing

Breakroom is pre-beta. Read [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) first;
decisions marked CONFIRMED are settled, PROPOSED ones are open to discussion.
Report security problems privately, see [SECURITY.md](SECURITY.md).

## Setup

Python 3.12+ and [uv](https://docs.astral.sh/uv/). The repo is a uv workspace
(`packages/*`, `apps/*`).

```bash
uv sync
```

This creates `.venv` at the repo root. Add dependencies with `uv add` or
`uv add --dev`. Pin every direct dependency with `==`, leave transitive ones to
`uv.lock`, and add a comment where a pin is not obvious.

## Checks

Run before proposing a change:

```bash
uv run ruff format --check . && uv run ruff check . && uv run mypy packages apps tests tools && uv run pytest
```

- ruff does both lint and format, line length 88, configured in `pyproject.toml`.
- Postgres race and migration tests skip unless `BREAKROOM_TEST_DATABASE_URL`
  points at a database.
- Unix socket paths must stay under 108 bytes: in tests create socket
  directories with `tempfile.mkdtemp(dir="/tmp")`.

## Tests

Write the failing test first, then the code. When a test genuinely has to come
after the code, break the production code on purpose and confirm that this
test is what turns red.

Not accepted:

- Assertions on a mock instead of the real component.
- An expected value computed by the code under test.
- Assertions pinned to an exact human-readable message, which fail on every
  redesign and miss real bugs.

## Code rules

- Configuration (timeouts, thresholds, retention) lives in one typed config
  module reading environment variables, never as a literal in logic.
- No secrets in code or committed files.
- Never log or store hook payload content or user content (message bodies,
  report notes, emails, nicknames, device names). Log counts and ids only.
- Debug and experimental tooling goes under `tools/`, not into what gets
  deployed.

## Commits and pull requests

A subject line, plus a short body only when a decision needs explaining. Say
what changed in the project and why. One commit per finished piece of work.

## Licence

Contributions are under the Apache-2.0 licence of the project. There is no CLA
and no DCO sign-off requirement.
