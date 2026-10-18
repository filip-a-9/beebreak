# Beebreak plugin for Claude Code

Declares the `beebreak` MCP server (inline in `.claude-plugin/plugin.json`),
which exposes one read-only tool, `beebreak_status`, and the activity hooks in
`hooks/hooks.json`.

Both call the `beebreak-mcp` and `beebreak-hook` commands by name, found on
`PATH` (`~/.local/bin` is added). Install them with the root README's Install
steps (`uv tool install`). A missing `beebreak-hook` makes every hook exit 0
silently; a missing `beebreak-mcp` shows the server as failed in `/mcp`.

## Development (repo checkout)

```bash
uv sync                                   # creates .venv at the repo root
claude --plugin-dir integrations/claude-code
```

The launchers look in `${CLAUDE_PLUGIN_ROOT}/../../.venv/bin` first, so a
checkout uses its own synced `.venv`. Then run `/mcp`: `beebreak` should be
listed. With no companion running, `beebreak_status` reports `not_running`.
