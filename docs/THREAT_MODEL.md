# Breakroom — M1–M4 Threat Model

**Scope:** the M1–M2 local slice (spec §17; M2 adds timing and decision logic)
on WSL2 Linux, plus the M3 identity and connectivity slice: a local server
(Postgres, Mailpit, FastAPI in Docker, loopback only), email one-time-code
sign-in, and a companion that holds a WebSocket connection to it. M4 adds
matchmaking: a user starts a search (the Find button, or accepting an
invitation), the server pairs two connected users, and each sees the other's
nickname plus only what both shared. A search is the one moment task metadata
leaves the machine (M4 rule 13, T23). Chat is M5 and gets its own section then.

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
| Consent record (version, time, three flags) | Server database | Gates which presence fields may be sent with a search (M4) |
| Queue entry metadata (presence id, task category, stack tags, time bucket, status, timestamps) | Server database, `queue_entries`; the four presence columns are filled only while the entry is `queued` | What a user chose to share for one search; linked to an account through the entry row |
| Match records (two participants, shared tags and category, times, end reason) | Server database, `matches` and `match_participants` | Show who was paired with whom and what they had in common; deleted 30 days after ending |
| Blocks (blocker, blocked, time) | Server database, `blocks` | A user's private list of people they refuse; revealing it tells the blocked person |
| Stack tag selection | Companion state file (local) | Sent only with a search and only when stack sharing is on |

## Trust boundaries

```text
Claude Code ──stdin JSON──▶ hook process ──Unix socket──▶ companion ──▶ window
Claude Code ◀──stdio MCP──▶ MCP server  ──Unix socket──▶ companion
companion ──HTTP + WebSocket (loopback until M6 adds TLS)──▶ server ──▶ Postgres / SMTP (Mailpit)
```

1. **Hook payload → reducer.** Untrusted and sensitive. Prompt text can carry
   injected instructions; fields can be oversized or malformed.
2. **Reducer → socket.** Only `NormalizedEvent` crosses (`extra="forbid"`).
3. **Socket → companion.** Any same-user process can connect.
4. **MCP server → Claude's context.** Anything returned enters the model context.
5. **Companion → server.** The only network boundary. Until M6 it is plain HTTP/WS
   and only to `127.0.0.1`, `::1` or `localhost`; any other host needs `https://`
   (TLS termination is M6 work). The server trusts nothing the client says about
   identity (README rule 12). Nothing derived from hook payloads crosses it;
   a search carries only the consented presence fields (T23, T24).
6. **Server → mail relay.** SMTP to Mailpit in M3; real SMTP is a config change.
7. **Peer → peer, through the server.** One user's nickname and shared tags are
   shown in another user's window. Both are untrusted text there (T32); the
   server never sends the other user's account ID or email (T35).

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
| T9 | MCP tool leaks state into Claude's context | `breakroom_status` returns a fixed small shape: companion and invitation state, aggregate activity state, a remaining-time estimate bucket and, since M4, the queue state (`none`, `searching`, `matched`); all fixed enums, no counts, keys or timestamps beyond `last_event_at` (see T29 for peer data) | MCP tests (M1-01, M4-07) |
| T10 | Agent enrolls the user without consent | No MCP tool changes consent or accepts invitations; accept is a GUI click only. Since M4 the only tool that touches the queue can cancel a search (T28) | Tool surface review, `test_exactly_the_two_tools_are_registered_and_take_no_input` |
| T11 | Untrusted text rendered as rich content in Qt | The window shows no event-derived text, only fixed strings. Amended in M3: the nickname is the one server-provided string; it is validated to `[A-Za-z0-9_-]{3,24}` on both sides and shown as plain text (`Qt.PlainText`), never as rich text | Window review (M1-04), nickname and window tests (M3-05b, M3-07) |
| T12 | Tampered or corrupt persisted consent state | State file is 0600 in a 0700 dir; same-user tampering falls under T5; a corrupt file turns suggestions off, a missing file gives defaults | State store tests (M2-04) |
| T13 | Refresh token stolen from disk | Keyring (Secret Service) when a real backend exists; otherwise a 0600 file in the 0700 state dir with a visible "stored unencrypted" notice. Access token is memory only; keyring entry is keyed by origin and state dir. A same-user attacker is T5 | Credential tests (M3-04), checklist M3-C07 |
| T14 | Token or code in a URL or a log | Bearer token only in the `Authorization` header or the first WebSocket frame, never a query string (ignored if sent); log lines carry fixed strings and opaque row IDs, no email, code, token, nickname or IP (the server runs with uvicorn's own request and connection logging off, database parameters hidden, and unexpected errors logged as a fixed string) | Log capture tests (M3-02, M3-03), token-not-in-exceptions test (M3-05a), `test_serve_process.py` (real `serve` stderr), `test_outbound_leaks.py` |
| T15 | Account enumeration | `POST /v1/auth/start` always answers 202 with a fresh `challenge_id`; limits apply to known and unknown emails alike; errors never echo input | Uniform start tests (M3-02) |
| T16 | Code brute force | 8-char code, 10 min lifetime, 5 attempts then locked even for the right code (the cap is enforced inside the `UPDATE`, so parallel guesses cannot pass it), one live code per email, per-email / per-IP / per-device rate limits (in memory, lost on restart: accepted for M3) | Auth and rate-limit tests (M3-02, M3-07), checklist M3-C09 |
| T17 | Refresh token replay | Rotation on every refresh; presenting a spent refresh token revokes the device and closes its live socket; sign-out revokes server-side | Token flow tests (M3-02), realtime tests (M3-03) |
| T18 | Unauthorized WebSocket join, or an unauthorized frame on a live socket | First frame must be `auth` within the timeout; unknown, expired, revoked or other-device tokens close 4401; every frame is checked again against the device; frame size, binary frames and flood are capped | Realtime tests (M3-03) |
| T19 | Cross-site WebSocket from a browser page | Any `Origin` header is refused (4401); the companion sends none; no cookies exist | `test_origin_header_closes_4401` (M3-03) |
| T20 | Invite reuse or race | Invite is bound to one email, single use, expires in 7 days; redemption is atomic, so the loser of a race is rejected | `test_invite_redeemed_concurrently_loses` (M3-02), checklist M3-C09 |
| T21 | Nickname impersonation | After NFKC the nickname must be ASCII `[A-Za-z0-9_-]{3,24}`, which makes cross-script confusables impossible; uniqueness is on a skeleton (casefold, `0→o`, `1→l`, `i→l`, `5→s`, `_`/`-` removed); reserved words blocked | Nickname tests (M3-07), checklist M3-C09 |
| T22 | Redirect or endpoint swap | One configured origin (`BREAKROOM_SERVER_URL`), validated by the endpoint policy; redirects are never followed (HTTP and WebSocket); no proxies or cookies from the environment; no network while signed out except a sign-in the user started | Endpoint and API client tests (M3-04, M3-05a), WebSocket redirect test |
| T23 | Task metadata leaks to the server beyond a user-started search | Task metadata leaves the companion through exactly one path (M4 rule 13): `RemoteWorker.join_queue` sends an `OutboundPresence` that `app.py` built with `build_presence` from the saved consent, only after the user pressed Find or Accept. Enforced statically; end to end, protected strings pushed through the real IPC path never appear in any HTTP request, WebSocket frame, raw byte, server log or database row, including through a full search, match and end | `tests/privacy/test_no_presence_sent.py`, `tests/privacy/test_outbound_leaks.py`, checklist M3-C12, M4-C13 |
| T24 | Presence beyond consent: a field the user did not allow is sent or stored | Client: `build_presence` copies a field only when its consent flag is on, and the consent is read at click time. Server: a join carrying a field whose flag is off gets 400, so a modified client cannot store more than the user allowed | `test_find_sends_unknown_with_only_consented_fields`, `test_find_with_no_consent_sends_presence_alone`, `test_find_uses_the_consent_saved_now`, `test_accept_with_time_not_consented_sends_no_bucket` (`test_app_break_wiring.py`); `test_field_without_consent_is_rejected`, `test_no_consent_and_presence_only_stores_nulls` (`test_queue_api.py`); `tests/privacy/test_presence_consent.py`; checklist M4-C13 |
| T25 | Presence kept after the search ends | The four presence columns are nulled in the same transaction that ends the entry (match, cancel, expiry, disabled user); the row keeps only status and times. Ended entries, ended matches and their participants are deleted after 30 days | `test_cancel_nulls_presence_and_is_idempotent` (`test_queue_api.py`); the match, expiry and disabled-user cases in `test_matchmaking.py`; `test_purge_deletes_only_rows_older_than_retention`, `test_purge_runs_at_most_once_a_minute`; `tests/privacy/test_outbound_leaks.py`; checklist M4-C13 |
| T26 | Duplicate match under races: one user in two matches, or one pair matched twice | The database enforces it: a partial unique index allows one `queued` entry per user and another allows one active participant row per user, on SQLite and Postgres. The in-process matcher lock is only a performance aid; a stale snapshot or a second server process loses at the index and the pair is skipped | `test_concurrent_passes_never_double_match`, `test_stale_snapshot_cannot_match_twice`, `test_overlapping_pairs_from_stale_snapshots`, `test_user_with_active_match_is_rejected_by_the_index` (`test_matchmaking_races.py`); `test_concurrent_joins_of_one_user_make_one_entry` (`test_queue_api.py`); `test_second_queued_entry_for_a_user_is_refused`, `test_second_active_participant_row_for_a_user_is_refused` (`test_models_matchmaking.py`); `tests/e2e/test_two_companions.py`; checklist M4-C04, C09 |
| T27 | Queue flooding by one account | At most one queued entry per user (T26), 20 joins per user per 15 minutes (429 after), entries expire after 90 s, and an idempotent replay returns the existing entry instead of making another | `test_rate_limit_on_the_21st_join`, `test_join_while_queued_or_matched_is_409`, `test_join_replay_returns_same_entry_without_second_kick` (`test_queue_api.py`); `test_entry_expires_exactly_at_expires_at` (`test_matchmaking.py`) |
| T28 | Agent enrolls the user in a search, or ends a human conversation | The MCP surface is exactly two tools. `breakroom_leave_queue` can only cancel a search the user started; it answers `not_searching` while matched and never ends a match. Nothing in the agent-facing code references `join_queue`, `QueueJoinRequest` or `build_presence` (AST check) | `test_bridge_never_references_the_queue_start_path`, `test_exactly_the_two_tools_are_registered_and_take_no_input`, `test_leave_queue_returns_exactly_the_companion_enum` (`test_mcp_server.py`); `test_leave_cancels_a_search`, `test_leave_never_touches_anything_else` (`test_app_break_wiring.py`); checklist M4-C12 |
| T29 | Peer data reaches Claude's context | The status reply and the MCP result carry only the fixed `queue` enum. The peer's nickname, tags and category exist only in the companion window, never in `StatusReply`, the IPC or the MCP result | `test_status_reports_the_fixed_queue_enum` (`test_mcp_server.py`); `test_the_status_reply_carries_only_the_fixed_queue_value` (`test_app_break_wiring.py`); checklist M4-C12 |
| T30 | Block evasion by renaming | A block is stored by account ID (never by nickname) and the matcher refuses the pair in both directions, so a nickname change does not get around it. Unblocking is admin CLI only | `test_block_records_block_and_tells_both_users_ended`, `test_mutual_blocks_are_two_rows` (`test_queue_api.py`); `test_block_prevents_pair_in_either_direction`, `test_blocked_users_each_match_the_third` (`test_matchmaking.py`); `test_block_list_and_remove` (`test_admin.py`); checklist M4-C10 |
| T31 | Block disclosure: the blocked person learns they were blocked | The blocked peer's companion gets `match.ended` with reason `ended`, the same as a normal End; no frame, field or message carries `blocked`, and the block list is visible only through the admin CLI | `test_block_records_block_and_tells_both_users_ended` (`test_queue_api.py`); `test_peer_ending_the_match_is_announced_by_frame` (`test_remote_worker_break.py`); checklist M4-C10 |
| T32 | Peer text injection in the window | The peer nickname is re-validated against the nickname pattern (`?` if it fails) and shown as plain text; tags and category are mapped to fixed display strings from the vocabulary, unknown values are dropped, never rendered as received | `test_a_peer_nickname_failing_the_pattern_shows_a_question_mark`, `test_unknown_tags_and_categories_never_render`, `test_break_labels_are_plain_text` (`test_window.py`) |
| T33 | Stale or ghost matches: a match with someone who is gone | Only users with a live WebSocket are paired; an entry whose user is offline is skipped and expires by its TTL. A match ends by itself after 15 minutes. A disabled account's entry is cancelled and its match ended within the sweep interval. The database, not frames, is the truth, and a reconnecting device gets a `sync` frame | `test_disconnected_user_skipped_then_expires`, `test_reconnecting_before_expiry_matches`, `test_match_ends_at_ttl`, `test_disabled_user_cancelled_and_match_ended` (`test_matchmaking.py`); `test_sync_with_a_queue_entry_shows_searching_with_that_entry`, `test_sync_with_a_match_shows_matched` (`test_remote_worker_break.py`); checklist M4-C08, C09 |
| T34 | Idempotency key used across users to read or take over another's entry | The key is unique per user and every lookup is scoped to the caller; the same key from two users makes two entries, and neither sees the other's | `test_replay_keys_are_scoped_to_the_user` (`test_queue_api.py`); `test_idempotency_key_is_unique_per_user_only` (`test_models_matchmaking.py`) |
| T35 | User ID enumeration, or probing a match one is not in | No client message, frame or field carries an account ID or email (a match shows the nickname only). Ending or blocking a match one is not in, or one that does not exist, answers the same 404, and every route needs authentication | `test_end_by_non_participant_or_unknown_match_is_404`, `test_every_route_requires_authentication` (`test_queue_api.py`); `test_send_to_user_reaches_every_device_and_no_other_user` (`test_realtime_fanout.py`) |

## Out of scope for M1–M4

Root or same-user attackers (T5, and an unencrypted credential file in the
fallback case), a network attacker beyond loopback (M3 and M4 are local Docker only;
TLS and public hosting are M6), email provider compromise (anyone who reads the
mailbox can sign in as that user), Claude Code itself being compromised, and
supply-chain integrity of pinned dependencies beyond `uv.lock` hashes.
Also known and unhandled: no reporting and no chat yet (M5), the match record
keeps the shared tags and category for 30 days after it ends, no account
deletion or export yet, no email change,
and WSL2 forwards the server's `127.0.0.1` ports to Windows (a valid token is
still required).
