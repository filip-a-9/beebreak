# Breakroom — M1–M2 Threat Model

**Scope:** the M1–M2 local slice (spec §17; M2 adds timing and decision logic) on WSL2 Linux. No network, no
backend. Remote surfaces (auth, queue, chat) get their own section in M3+.

## Assets

| Asset | Where it lives | Why it matters |
|---|---|---|
| Hook payloads | stdin of the hook process | Contain prompts, commands, file paths, tool output, session IDs |
| Normalized events | IPC socket, companion memory | Low sensitivity, but timestamps reveal work patterns |
| Session-key salt | `$XDG_RUNTIME_DIR/breakroom/salt` | Lets anyone holding it link keys back to Claude session IDs |
| Invitation consent | Companion state | An accept is the gate to future remote presence (M3+) |
| Persisted consent state | `~/.local/state/breakroom/companion-state.json` (0600, in a 0700 dir) | Holds "suggestions off" and the cooldown deadline; tampering changes when invitations appear |
| Claude Code availability | The developer's session | Breakroom must never slow or stop the agent |

## Trust boundaries

```text
Claude Code ──stdin JSON──▶ hook process ──Unix socket──▶ companion ──▶ window
Claude Code ◀──stdio MCP──▶ MCP server  ──Unix socket──▶ companion
```

1. **Hook payload → reducer.** Untrusted and sensitive. Prompt text can carry
   injected instructions; fields can be oversized or malformed.
2. **Reducer → socket.** Only `NormalizedEvent` crosses (`extra="forbid"`).
3. **Socket → companion.** Any same-user process can connect.
4. **MCP server → Claude's context.** Anything returned enters the model context.

## Threats and controls

| # | Threat | Control | Verified by |
|---|---|---|---|
| T1 | Raw prompt/command/path leaks into an event, log, or stdout | Reducer reads exactly four payload keys (`hook_event_name`, `session_id`, `tool_name`, `notification_type`); `Notification.message` is never read; output is a validated `NormalizedEvent`; nothing from the payload is logged | Privacy-negative fixtures (M1-02, M2-01) |
| T2 | Hook blocks or crashes Claude Code | Hook always exits 0, bounded connect/send timeout, no process spawning, companion absence is a no-op | Fail-open tests (M1-03), latency measurement (M1-06) |
| T3 | Another user's process reads or injects events | Socket in a `0700` directory, socket file `0600` | Permission test (M1-03) |
| T4 | Windows-side process reaches the IPC | Unix socket, not loopback TCP: WSL2 forwards `127.0.0.1` listeners to Windows | Design decision (ARCHITECTURE §2) |
| T5 | Same-user malicious process forges events or status queries | **Accepted for M1.** Same-user code can already read hook payloads directly; forged events can at most cause an invitation | — |
| T6 | Oversized or malformed IPC frame exhausts the companion | Frame cap (16 KiB), per-connection read timeout, strict model validation, invalid frames dropped | IPC tests (M1-03) |
| T7 | Stale socket file from a crashed companion | Server removes a socket file it cannot connect to; client treats refusal as "not running" | IPC tests (M1-03) |
| T8 | Session ID correlation | Session ID hashed with a per-boot random salt; raw ID never leaves the hook process | Reducer tests (M1-02) |
| T9 | MCP tool leaks state into Claude's context | `breakroom_status` returns a fixed small shape: companion and invitation state, aggregate activity state and a remaining-time estimate bucket (fixed enums; no counts, keys or timestamps beyond `last_event_at`) | MCP tests (M1-01) |
| T10 | Agent enrolls the user without consent | No MCP tool changes consent or accepts invitations; accept is a GUI click only | Tool surface review |
| T11 | Untrusted text rendered as rich content in Qt | The window shows no event-derived text, only fixed strings | Window review (M1-04) |
| T12 | Tampered or corrupt persisted consent state | State file is 0600 in a 0700 dir; same-user tampering falls under T5; a corrupt file turns suggestions off, a missing file gives defaults | State store tests (M2-04) |

## Out of scope for M1–M2

Root or same-user attackers (T5), network attackers (no network), Claude Code
itself being compromised, and supply-chain integrity of pinned dependencies
beyond `uv.lock` hashes.
