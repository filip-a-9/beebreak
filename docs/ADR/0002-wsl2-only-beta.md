# ADR 0002: WSL2 only for the beta

Status: Accepted (M1 CONFIRMED 2026-10-12; beta scope CONFIRMED 2026-10-16).
Source: ARCHITECTURE §2, §16 item 2; M6 README.

## Context
Hooks, the MCP server and the companion must run on the same machine and agree
on local IPC. Native Windows, WSL, remote/SSH sessions and macOS differ in where
hooks execute.

## Decision
Claude Code, hooks, MCP server, companion (GUI via WSLg) and local IPC all run
inside WSL2 Linux. The beta supports WSL2 only. Native Linux and Windows are
out. The focus-on-match behaviour (F6) needs no native check.

## Alternatives
The original proposal tested Windows first, then Windows and Linux for a closed
beta, with macOS later (ARCHITECTURE §2, "Original platform proposal").

## Consequences
Packaging and install work targets one environment. Other platforms stay open
for later and need their own decision.

## How it is tested
The M1 to M5 checklists and the real `claude -p` runs in `tools/e2e_claude.py`
run inside WSL2.

## Rollback
Reopen §16 item 2 and add the platform with its own checks.
