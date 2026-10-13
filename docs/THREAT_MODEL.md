# Breakroom — M1–M3 Threat Model

**Scope:** the M1–M2 local slice (spec §17; M2 adds timing and decision logic)
on WSL2 Linux, plus the M3 identity and connectivity slice: a local server
(Postgres, Mailpit, FastAPI in Docker, loopback only), email one-time-code
sign-in, and a companion that holds a WebSocket connection to it. M3 sends
connect and heartbeat only; no task metadata leaves the machine (README M3,
rule 13). Queue and chat get their own section in M4+.

## Assets

| Asset | Where it lives | Why it matters |
|---|---|---|
| Hook payloads | stdin of the hook process | Contain prompts, commands, file paths, tool output, session IDs |
| Normalized events | IPC socket, companion memory | Low sensitivity, but timestamps reveal work patterns |
| Session-key salt | `$XDG_RUNTIME_DIR/breakroom/salt` | Lets anyone holding it link keys back to Claude session IDs |
| Invitation consent | Companion state | An accept is the gate to future remote presence (M3+) |
| Persisted consent state | `~/.local/state/breakroom/companion-state.json` (0600, in a 0700 dir) | Holds "suggestions off" and the cooldown deadline; tampering changes when invitations appear |
| Claude Code availability | The developer's session | Breakroom must never slow or stop the agent |
| Account email | Server database (canonical form), invite and challenge rows, Mailpit in dev | Identifies a person; never shown to other users, never logged |
| Sign-in code and invite code | Emailed once; the server stores only an HMAC | Possession plus the email signs a device in |
| Access token (`bra_`, 15 min) | Companion memory only; server stores SHA-256 | Bearer credential for HTTP and WebSocket |
| Refresh token (`brr_`, 30 days, rotated) | Keyring, or `credentials.json` (0600, 0700 dir, unencrypted); server stores SHA-256 | Long-lived login for one device |
| Nickname | Server database; shown by the companion as plain text | The only server-provided string the companion displays; impersonation target |
| Consent record (version, time, three flags) | Server database | Gates which presence fields may ever be sent (M4+) |

## Trust boundaries

```text
Claude Code ──stdin JSON──▶ hook process ──Unix socket──▶ companion ──▶ window
Claude Code ◀──stdio MCP──▶ MCP server  ──Unix socket──▶ companion
companion ──HTTP + WebSocket (loopback in M3; TLS in M6)──▶ server ──▶ Postgres / SMTP (Mailpit)
```

1. **Hook payload → reducer.** Untrusted and sensitive. Prompt text can carry
   injected instructions; fields can be oversized or malformed.
2. **Reducer → socket.** Only `NormalizedEvent` crosses (`extra="forbid"`).
3. **Socket → companion.** Any same-user process can connect.
4. **MCP server → Claude's context.** Anything returned enters the model context.
5. **Companion → server.** The only network boundary. In M3 it is plain HTTP/WS
   and only to `127.0.0.1`, `::1` or `localhost`; any other host needs `https://`
   (TLS termination is M6 work). The server trusts nothing the client says about
   identity (README rule 12). Nothing derived from hook payloads crosses it.
6. **Server → mail relay.** SMTP to Mailpit in M3; real SMTP is a config change.

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
| T11 | Untrusted text rendered as rich content in Qt | The window shows no event-derived text, only fixed strings. Amended in M3: the nickname is the one server-provided string; it is validated to `[A-Za-z0-9_-]{3,24}` on both sides and shown as plain text (`Qt.PlainText`), never as rich text | Window review (M1-04), nickname and window tests (M3-05b, M3-07) |
| T12 | Tampered or corrupt persisted consent state | State file is 0600 in a 0700 dir; same-user tampering falls under T5; a corrupt file turns suggestions off, a missing file gives defaults | State store tests (M2-04) |
| T13 | Refresh token stolen from disk | Keyring (Secret Service) when a real backend exists; otherwise a 0600 file in the 0700 state dir with a visible "stored unencrypted" notice. Access token is memory only; keyring entry is keyed by origin and state dir. A same-user attacker is T5 | Credential tests (M3-04), checklist M3-C07 |
| T14 | Token or code in a URL or a log | Bearer token only in the `Authorization` header or the first WebSocket frame, never a query string (ignored if sent); log lines carry fixed strings and opaque row IDs, no email, code, token, nickname or IP | Log capture tests (M3-02, M3-03), token-not-in-exceptions test (M3-05a), `test_outbound_leaks.py` |
| T15 | Account enumeration | `POST /v1/auth/start` always answers 202 with a fresh `challenge_id`; limits apply to known and unknown emails alike; errors never echo input | Uniform start tests (M3-02) |
| T16 | Code brute force | 8-char code, 10 min lifetime, 5 attempts then locked even for the right code, one live code per email, per-email / per-IP / per-device rate limits (in memory, lost on restart: accepted for M3) | Auth and rate-limit tests (M3-02, M3-07), checklist M3-C09 |
| T17 | Refresh token replay | Rotation on every refresh; presenting a spent refresh token revokes the device and closes its live socket; sign-out revokes server-side | Token flow tests (M3-02), realtime tests (M3-03) |
| T18 | Unauthorized WebSocket join, or an unauthorized frame on a live socket | First frame must be `auth` within the timeout; unknown, expired, revoked or other-device tokens close 4401; every frame is checked again against the device; frame size, binary frames and flood are capped | Realtime tests (M3-03) |
| T19 | Cross-site WebSocket from a browser page | Any `Origin` header is refused (4401); the companion sends none; no cookies exist | `test_origin_header_closes_4401` (M3-03) |
| T20 | Invite reuse or race | Invite is bound to one email, single use, expires in 7 days; redemption is atomic, so the loser of a race is rejected | `test_invite_redeemed_concurrently_loses` (M3-02), checklist M3-C09 |
| T21 | Nickname impersonation | After NFKC the nickname must be ASCII `[A-Za-z0-9_-]{3,24}`, which makes cross-script confusables impossible; uniqueness is on a skeleton (casefold, `0→o`, `1→l`, `i→l`, `5→s`, `_`/`-` removed); reserved words blocked | Nickname tests (M3-07), checklist M3-C09 |
| T22 | Redirect or endpoint swap | One configured origin (`BREAKROOM_SERVER_URL`), validated by the endpoint policy; redirects are never followed; no proxies or cookies from the environment; no network while signed out except a sign-in the user started | Endpoint and API client tests (M3-04, M3-05a) |
| T23 | Task metadata leaks to the server in M3 | Nothing derived from hook events is sent: no code path serializes `OutboundPresence` (rule 13), enforced statically; end to end, protected strings pushed through the real IPC path while connected never appear in any HTTP request, WebSocket frame, raw byte, server log or database row | `tests/privacy/test_no_presence_sent.py`, `tests/privacy/test_outbound_leaks.py`, checklist M3-C12 |

## Out of scope for M3

Root or same-user attackers (T5, and an unencrypted credential file in the
fallback case), a network attacker beyond loopback (M3 is local Docker only;
TLS and public hosting are M6), email provider compromise (anyone who reads the
mailbox can sign in as that user), Claude Code itself being compromised, and
supply-chain integrity of pinned dependencies beyond `uv.lock` hashes.
Also known and unhandled: no account deletion or export yet, no email change,
and WSL2 forwards the server's `127.0.0.1` ports to Windows (a valid token is
still required).
