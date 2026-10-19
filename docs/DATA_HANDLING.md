# Data handling

What Beebreak stores, where, who can read it, how long it is kept and how it
is deleted. Server retention values are the defaults in `ServerConfig`
(`apps/server/src/beebreak_server/config.py`); each can be changed with the
environment variable named in the table.

The Beebreak project runs no server and collects no data. "The operator" below
is whoever runs the server you use.

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

Paths follow `beebreak_shared.paths`. `$XDG_RUNTIME_DIR` falls back to
`/tmp/beebreak-<uid>` and `$XDG_STATE_HOME` to `~/.local/state`.

| What | Where stored | Who can read | Retention | Deletion |
|---|---|---|---|---|
| IPC socket `companion.sock` | `$XDG_RUNTIME_DIR/beebreak/` (dir `0700`, socket `0600`) | Your user account only | Exists while the companion runs; a stale file is removed on the next start | Stop the companion; remove the file |
| Session-key salt `salt` | `$XDG_RUNTIME_DIR/beebreak/` (`0600`) | Your user account only | Kept in the runtime directory until it is removed | Remove the file |
| State file `companion-state.json` (suggestions on/off, cooldown deadline, chosen stack tags) | `$XDG_STATE_HOME/beebreak/` (dir `0700`, file `0600`) | Your user account only | Until you remove it | Remove the file |
| Refresh token (30 days, rotated) | OS keyring when a real backend exists; otherwise `credentials.json` in the state directory (`0600`, **unencrypted**, and the app tells you) | Your user account (keyring: also anything that can unlock it) | Until sign-out or removal | Sign out in the app; or remove the file or keyring entry |
| Access token (15 min) | Companion memory only | The companion process | Process lifetime | Quit the companion |
| Chat messages and peer nickname | Companion memory and window only | You | Until the window closes | Close the window |

`beebreak-companion install` writes `install.json` in the state directory
(`0600`; the server URL chosen at install, no secrets).
`beebreak-companion uninstall` deletes the state directory, socket directory
(socket, salt), keyring entry, credentials file and any
`~/.config/autostart/beebreak.desktop` an older release wrote, and
revokes the device on the server unless `--keep-account` is given or the
server is unreachable (it says which).

## On the server

All tables are in `apps/server/src/beebreak_server/models.py`. Unless a row
says otherwise, only the operator (anyone with database or admin CLI access)
can read a table; no API route returns an account ID or an email to another
user.

| What | Where stored | Who can read | Retention | Deletion |
|---|---|---|---|---|
| Account: canonical email (email accounts) or numeric GitHub user id (GitHub accounts; never both), nickname, nickname skeleton, status, created time | `users` | Operator. Other users see only the nickname (while matched) | Until the account is deleted | `beebreak-admin account delete` |
| Consent: version, time, three flags (category, stack tags, time bucket) | `user_preferences` | Operator | Until the account is deleted | `account delete` |
| Devices: name, hashed access token, token expiry, revoked, created, last seen and connected times | `devices` | Operator | Until the account is deleted. Name is a generic default (`WSL device`) the user can edit before sign-in; the machine hostname is never sent | `account delete`; sign-out revokes the device but keeps the row |
| Refresh tokens: SHA-256 hash, device, expiry, used time | `refresh_tokens` | Operator | Deleted 7 days after the token expires, used or not (`BEEBREAK_REFRESH_TOKEN_RETENTION_S`); an unexpired token is kept | Automatic purge |
| Sign-in challenges: email, HMAC of the emailed code, attempts, expiry, consumed time; for GitHub sign-in no identity at all, only the GitHub device code (a secret valid for about 15 minutes, cleared when the challenge is used or refused; a never-polled one stays until the purge), the poll interval and next poll time. The GitHub access token is never stored | `auth_challenges` | Operator | Deleted 24 h after use or expiry (`BEEBREAK_CHALLENGE_RETENTION_S`). A code is valid for 10 minutes (`BEEBREAK_CODE_TTL_S`) | Automatic purge |
| Beta invites: email and HMAC of the invite code, or a GitHub numeric id (no code, no username), expiry, redeemed and revoked times, who redeemed | `beta_invites` | Operator | An invite is valid for 7 days (`BEEBREAK_INVITE_TTL_S`); expired, redeemed or revoked rows are deleted after 30 days (`BEEBREAK_INVITE_RETENTION_S`) | Automatic purge; `invite revoke` ends validity |
| Account tombstone: keyed hash (HMAC under a subkey of the server secret) of the email, or of `github:<id>`, of an account that was disabled for abuse and then deleted, created time. No email, no user id | `deleted_account_tombstones` | Operator (the hash cannot be reversed to an email without the secret and a guess) | 1 year (`BEEBREAK_TOMBSTONE_RETENTION_S`). Purpose: abuse prevention, so a banned person cannot return as a new user unnoticed. Lawful basis: legitimate interest, PROPOSED; each operator checks it | Automatic purge; `invite create --override-tombstone` removes it when the operator decides to invite that address |
| Queue entries: presence id, task category, stack tags, time bucket (only while queued), status, times, device, match | `queue_entries` | Operator | The four presence columns are cleared the moment the entry ends. The row is deleted 30 days after it ends (`BEEBREAK_RETENTION_S`). A search lasts 90 s (`BEEBREAK_QUEUE_TTL_S`) | Automatic purge; `account delete` |
| Matches (also the chat): times, end reason, shared tags and category, extension count and requester, last message number | `matches` | Operator. Both participants see the shared tags and category | Deleted 30 days after the chat ends (`BEEBREAK_RETENTION_S`) | Automatic purge |
| Match participants: who took part, active flag, offline-since time, count of messages sent | `match_participants` | Operator | Deleted with the match, 30 days after it ends | Automatic purge; `account delete` |
| Messages: author, per-chat sequence number, client message id, body, time | `messages` | The two participants (through the app, until deleted) and the operator | Bodies deleted 24 h after the chat ends (`BEEBREAK_MESSAGE_RETENTION_S`) | Automatic purge; `account delete` |
| Blocks: blocker, blocked, time | `blocks` | Operator (a block is never shown to the blocked person) | Until the account is deleted | `account delete`; the admin CLI can remove a block |
| Reports: reporter, reported, match id, reason, optional note (500 chars), status, times | `reports` | Operator. The reported person is not told | Open reports are kept until resolved; resolved reports are deleted 90 days after resolution (`BEEBREAK_REPORT_RETENTION_S`) | Automatic purge. `account delete` is refused while the user is on either side of an open report |
| Report evidence: copies of the messages the reporter ticked (author role, body, time) | `report_messages` | Operator | Same as the report | Deleted with the report |
| Backups: database dumps (every table above) | `backups/` on the host, mode 0600, optionally age or gpg encrypted | Operator | Set by the operator (`<backup retention>` in the privacy notice); the runbook's 14-day cron prune is an example | Delete the file. An older backup still holds accounts deleted since it was taken |

Not stored in the database: the in-memory rate-limit counters (lost on server
restart). Server logs hold fixed strings, counts and opaque row ids, never an
email, code, token, nickname, message body, report note or device name, and
no IP address.

IP addresses. The server keeps a client's IP address (or, behind the trusted
proxy, the forwarded address; ADR 0015) in memory as the key of its rate
limiters for sign-in, GitHub polling and WebSocket connects (`_client_key` in
`apps/server/src/beebreak_server/app.py`). A key is dropped at the next call of the same limiter after its hits are
older than `BEEBREAK_RATE_WINDOW_S` (default 15 minutes; pruning runs on
traffic, not on a timer, so on a quiet server a key can stay longer), and all
of them are lost when the server restarts. Nothing writes them to the database or to the
server's own logs. Purpose: abuse prevention and security (limiting guessing of
sign-in codes and flooding). Lawful basis: legitimate interest, as for the
abuse handling in the privacy notice.

The Caddy proxy (`compose.prod.yaml`) has its own log. Its error log may
include the client IP address, the host and the request URI, for example on a
502 while the server restarts. Docker's json-file driver keeps it, capped at 5
files of 10 MB per service (`max-size: 10m`, `max-file: "5"`), so retention is
by size, not by time. Check on your host what the Caddy log
really contains, and whether it should be cut down (see the
[untested list](RUNBOOK.md#untested-on-a-real-host)).

GitHub sign-in (OAuth device flow, driven by the server): the server sends GitHub its client id (no scope, no secret), receives a device code and a user code, and later one access token. It uses the token for one `GET /user`, keeps only the numeric `id`, and drops the token; it is never written to the database or to a log, and never reaches the companion. The companion sees only the user code and the page to open. A GitHub username is used once at invite time to find the id and is not stored. Servers with an empty SMTP host send no email at all.

Sign-in emails are sent through the configured SMTP host (Mailpit in local
development), over STARTTLS or implicit TLS in production. SMTP credentials
come only from the environment and are never logged. The operator names the provider in the privacy notice.

## Your data requests

Email the operator (`<contact address>` in the privacy notice). The operator exports or deletes an account with the
admin CLI and answers within the response time in that server's privacy
notice (14 days by default, ADR 0013).

- Export (`beebreak-admin account export <email>` or `--github-id <id>`): your own rows as JSON, including
  your own message bodies while they exist and your own messages copied into
  reports you filed. Reports filed against you appear
  only as a date, status and resolution date, with no reporter, note or
  evidence. Hashes and other people's messages are never included.
- Delete (`beebreak-admin account delete`): a hard delete of every row that
  refers to you, and of the invites and challenges for your email. It is
  refused while you are queued, in a live chat, or on either side of an open
  report. A disabled account is deleted only after the operator reviews it;
  deleting it leaves a keyed hash of its email for a year (see the
  tombstone row in the table above). Deleting an account also deletes the
  reports filed against it and their evidence, resolved ones included, so the
  record of a resolved report about you goes with you (PROPOSED; each
  operator checks it). Your devices are
  signed out first. If the other person in a chat remains, their side of the chat is
  kept.

For the full notice see [PRIVACY_NOTICE.md](PRIVACY_NOTICE.md).
