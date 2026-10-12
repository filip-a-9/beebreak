# Breakroom plugin for Claude Code (development install)

Declares the `breakroom` MCP server (inline in `.claude-plugin/plugin.json`),
which exposes one read-only tool, `breakroom_status`.

## Install (repo checkout)

```bash
uv sync                                   # creates .venv at the repo root
claude --plugin-dir integrations/claude-code
```

Then run `/mcp`: `breakroom` should be listed. Ask Claude to call
`breakroom_status`; with no companion running it reports `not_running`.

## Known limitation

The server command is `${CLAUDE_PLUGIN_ROOT}/../../.venv/bin/python`, so it only
works for a repo checkout loaded with `--plugin-dir integrations/claude-code`
and a synced `.venv`. It will not work for an installed or copied plugin. A
packaged launcher is planned for M6.

Hooks are not declared here; M1-02 adds `hooks/hooks.json`.
