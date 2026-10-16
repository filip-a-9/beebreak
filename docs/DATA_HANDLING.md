# Data handling

What Breakroom stores, where, who can read it, how long it is kept and how it
is deleted. Server retention values are the defaults in `ServerConfig`
(`apps/server/src/breakroom_server/config.py`); each can be changed with the
environment variable named in the table.

## The operator can read chats

Chat is not end-to-end encrypted. Messages sit in the server database in clear
text. Whoever runs the server or its database can read them from the moment
they are sent until they are deleted, 24 hours after the chat ends. The same
person can read report notes and the messages a reporter ticked as evidence
until 90 days after the report is resolved (open reports are kept until
resolved). The admin CLI has no command that reads a chat; `report show`
prints only the reporter's note and the ticked messages. The chat window says
this every time: "Not end-to-end encrypted. Messages are deleted 24 h after the
chat ends." (THREAT_MODEL T49).

## What never leaves your machine

Hook payloads (prompts, commands, file paths, tool output, session IDs),
repository and workspace names, and the activity events derived from them stay
local. A search sends only the fields you consented to (task category, stack
tags, time bucket) and only after you press Find or accept an invitation.

## On your machine

Paths follow `breakroom_shared.paths`. `$XDG_RUNTIME_DIR` falls back to
`/tmp/breakroom-<uid>` and `$XDG_STATE_HOME` to `~/.local/state`.

| What | Where stored | Who can read | Retention | Deletion |
|---|---|---|---|---|
| IPC socket `companion.sock` | `$XDG_RUNTIME_DIR/breakroom/` (dir `0700`, socket `0600`) | Your user account only | Exists while the companion runs; a stale file is removed on the next start | Stop the companion; remove the file |
| Session-key salt `salt` | `$XDG_RUNTIME_DIR/breakroom/` (`0600`) | Your user account only | Kept in the runtime directory until it is removed | Remove the file |
| State file `companion-state.json` (suggestions on/off, cooldown deadline, chosen stack tags) | `$XDG_STATE_HOME/breakroom/` (dir `0700`, file `0600`) | Your user account only | Until you remove it | Remove the file |
| Refresh token (30 days, rotated) | OS keyring when a real backend exists; otherwise `credentials.json` in the state directory (`0600`, **unencrypted**, and the app tells you) | Your user account (keyring: also anything that can unlock it) | Until sign-out or removal | Sign out in the app; or remove the file or keyring entry |
| Access token (15 min) | Companion memory only | The companion process | Process lifetime | Quit the companion |
| Chat messages and peer nickname | Companion memory and window only | You | Until the window closes | Close the window |

`breakroom-companion install` writes `install.json` in the state directory
(`0600`; the server URL chosen at install, no secrets) and, with
`--autostart`, `~/.config/autostart/breakroom.desktop`.
`breakroom-companion uninstall` deletes the state directory, socket directory
(socket, salt), keyring entry, credentials file and autostart entry, and
revokes the device on the server unless `--keep-account` is given or the
server is unreachable (it says which).

## On the server

All tables are in `apps/server/src/breakroom_server/models.py`. Unless a row
says otherwise, only the operator (anyone with database or admin CLI access)
can read a table; no API route returns an account ID or an email to another
user.

| What | Where stored | Who can read | Retention | Deletion |
|---|---|---|---|---|
| Account: canonical email, nickname, nickname skeleton, status, created time | `users` | Operator. Other users see only the nickname (while matched) | Until the account is deleted | `breakroom-admin account delete` |
| Consent: version, time, three flags (category, stack tags, time bucket) | `user_preferences` | Operator | Until the account is deleted | `account delete` |
| Devices: name, hashed access token, token expiry, revoked, created, last seen and connected times | `devices` | Operator | Until the account is deleted. Name is a generic default (`WSL device`) the user can edit before sign-in; the machine hostname is never sent | `account delete`; sign-out revokes the device but keeps the row |
| Refresh tokens: SHA-256 hash, device, expiry, used time | `refresh_tokens` | Operator | Deleted 7 days after the token expires, used or not (`BREAKROOM_REFRESH_TOKEN_RETENTION_S`); an unexpired token is kept | Automatic purge |
| Sign-in challenges: email, HMAC of the emailed code, attempts, expiry, consumed time | `auth_challenges` | Operator | Deleted 24 h after use or expiry (`BREAKROOM_CHALLENGE_RETENTION_S`). A code is valid for 10 minutes (`BREAKROOM_CODE_TTL_S`) | Automatic purge |
| Beta invites: email, HMAC of the invite code, expiry, redeemed and revoked times, who redeemed | `beta_invites` | Operator | An invite is valid for 7 days (`BREAKROOM_INVITE_TTL_S`); expired, redeemed or revoked rows are deleted after 30 days (`BREAKROOM_INVITE_RETENTION_S`) | Automatic purge; `invite revoke` ends validity |
| Queue entries: presence id, task category, stack tags, time bucket (only while queued), status, times, device, match | `queue_entries` | Operator | The four presence columns are cleared the moment the entry ends. The row is deleted 30 days after it ends (`BREAKROOM_RETENTION_S`). A search lasts 90 s (`BREAKROOM_QUEUE_TTL_S`) | Automatic purge; `account delete` |
| Matches (also the chat): times, end reason, shared tags and category, extension count and requester, last message number | `matches` | Operator. Both participants see the shared tags and category | Deleted 30 days after the chat ends (`BREAKROOM_RETENTION_S`) | Automatic purge |
| Match participants: who took part, active flag, offline-since time, count of messages sent | `match_participants` | Operator | Deleted with the match, 30 days after it ends | Automatic purge; `account delete` |
| Messages: author, per-chat sequence number, client message id, body, time | `messages` | The two participants (through the app, until deleted) and the operator | Bodies deleted 24 h after the chat ends (`BREAKROOM_MESSAGE_RETENTION_S`) | Automatic purge; `account delete` |
| Blocks: blocker, blocked, time | `blocks` | Operator (a block is never shown to the blocked person) | Until the account is deleted | `account delete`; the admin CLI can remove a block |
| Reports: reporter, reported, match id, reason, optional note (500 chars), status, times | `reports` | Operator. The reported person is not told | Open reports are kept until resolved; resolved reports are deleted 90 days after resolution (`BREAKROOM_REPORT_RETENTION_S`) | Automatic purge. `account delete` is refused while the user is on either side of an open report |
| Report evidence: copies of the messages the reporter ticked (author role, body, time) | `report_messages` | Operator | Same as the report | Deleted with the report |

Not stored in the database: the in-memory rate-limit counters (lost on server
restart). Server logs hold fixed strings, counts and opaque row ids, never an
email, code, token, nickname, message body, report note, device name or IP
address.

Sign-in emails are sent through the configured SMTP host (Mailpit in local
development), over STARTTLS or implicit TLS in production. SMTP credentials
come only from the environment and are never logged. The provider for the hosted beta is to be named before the beta.

## Your data requests

Email `<contact address>`. The operator exports or deletes an account with the
admin CLI and answers within 14 days.

- Export (`breakroom-admin account export`): your own rows as JSON, including
  your own message bodies while they exist and your own messages copied into
  reports you filed. Reports filed against you appear
  only as a date, status and resolution date, with no reporter, note or
  evidence. Hashes and other people's messages are never included.
- Delete (`breakroom-admin account delete`): a hard delete of every row that
  refers to you, and of the invites and challenges for your email. It is
  refused while you are queued, in a live chat, or on either side of an open
  report. A disabled account is deleted only after the operator reviews it,
  because deletion would also erase the record of the ban. Your devices are
  signed out first. If the other person in a chat remains, their side of the chat is
  kept.

For the full notice see [PRIVACY_NOTICE.md](PRIVACY_NOTICE.md).
