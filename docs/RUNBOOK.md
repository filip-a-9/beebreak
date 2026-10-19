# Runbook

How to run a Beebreak server from `compose.prod.yaml`. The Beebreak project
runs no server (ADR 0021): you are the operator of yours, and the controller of
the data on it. Every command runs on the host, from the repository root. `dc` below means:

```sh
alias dc='docker compose -f compose.prod.yaml'
```

The stack is Caddy (TLS) -> server -> Postgres. Only Caddy publishes ports.

Read [Before you open your server to others](#before-you-open-your-server-to-others)
and [Untested on a real host](#untested-on-a-real-host) before you let anyone
else sign in.

## Before you open your server to others

Fill in each field below. Each is written in angle brackets. Copy
`docs/PRIVACY_NOTICE.md`, fill in your copy, and run `grep -n '<'
your-copy-of-PRIVACY_NOTICE.md`: it must print nothing. Point the links in the
notice that go to `DATA_HANDLING.md` at that file in the repository at the tag
you run.

| Field | What to put | File |
|---|---|---|
| `<operator name>` | Who is responsible for the data (you or your organisation) | `docs/PRIVACY_NOTICE.md` |
| `<contact address>` | A mailbox you read, for appeals, data requests and abuse reports | `docs/PRIVACY_NOTICE.md` (also named in `docs/DATA_HANDLING.md` and this file) |
| `<response time>` | How fast you answer; this runbook assumes 14 days | `docs/PRIVACY_NOTICE.md` |
| `<email provider>` | The SMTP provider, if email sign-in is on | `docs/PRIVACY_NOTICE.md` |
| `<hosting provider>` | Who hosts the machine and database | `docs/PRIVACY_NOTICE.md` |
| `<backup retention>` | How long you keep backups, and where | `docs/PRIVACY_NOTICE.md` (also the backups row of `docs/DATA_HANDLING.md`) |
| `<transfers>` | Whether a provider is outside your country and on what basis | `docs/PRIVACY_NOTICE.md` |
| `<supervisory authority>` | The data protection authority for your country | `docs/PRIVACY_NOTICE.md` |
| `<changes>` | Where the notice is published and how you announce changes | `docs/PRIVACY_NOTICE.md` |
| `<abuse report owner>` and `<abuse report response time>` | Who runs `report list`, and how fast they act | [Abuse reports](#abuse-reports) in this file |

Then:

- Publish the filled-in privacy notice where your users can read it, and check
  its lawful bases and retention periods against the law that applies to you.
  No lawyer has reviewed the template.
- Register your own GitHub OAuth App if you use GitHub sign-in (see below).
- Go through [Untested on a real host](#untested-on-a-real-host).
- Set up [alerting](#alerting).
- Run the [restore drill](#restore-drill) on your host once.

## Start and stop

```sh
dc up -d --build        # build the server image, start everything
dc ps                   # postgres, server (healthy), caddy; migrate has exited 0
dc stop                 # stop, keep all data
dc start                # start again
dc down                 # remove containers and networks, KEEP volumes
```

Never run `dc down -v` unless you mean to destroy the database and Caddy's
certificates: `-v` deletes the `postgres-data` and `caddy-data` volumes.

`migrate` runs `beebreak-server migrate` once before `server` starts, so a
new release needs only `dc up -d --build`.

Check the server from outside: `curl https://<BEEBREAK_PUBLIC_HOST>/v1/health`
answers `{"status":"ok"}`. The `server` container has the same check as its
Docker healthcheck, and Caddy starts only once it passes.

## Set secrets

`cp .env.example .env && chmod 600 .env`, then replace every placeholder. `.env` is ignored by
git; never commit it. Make each secret with:

```sh
python -c "import secrets; print(secrets.token_urlsafe(48))"
```

- `POSTGRES_PASSWORD`: the server refuses to start while it is still
  `change-me`. Use `token_urlsafe` output (it is placed inside a database URL).
- `BEEBREAK_SERVER_SECRET`: URL-safe base64, at least 32 bytes decoded.
- `BEEBREAK_SMTP_*` and `BEEBREAK_MAIL_FROM`: the mail account that sends
  sign-in codes. `BEEBREAK_SMTP_USERNAME` and `BEEBREAK_SMTP_PASSWORD` must
  not stay at their `change-me-*` examples.
- `BEEBREAK_PUBLIC_HOST`: the DNS name Caddy gets its certificate for.
- Sign-in methods: at least one of email codes (`BEEBREAK_SMTP_HOST` and the
  other SMTP values) and GitHub (`BEEBREAK_GITHUB_CLIENT_ID`, next section).
  An empty `BEEBREAK_SMTP_HOST` turns email codes off; an empty client id turns
  GitHub off; with neither the server refuses to start.

## GitHub sign-in (optional)

Needs no email provider and no client secret.

Each operator registers their own OAuth App; the project has none to share.

1. On GitHub: Settings, Developer settings, OAuth Apps, New OAuth App. Name it,
   set the homepage to your server's page or the project page, and set the authorization callback URL
   to the same page (the field is required; the device flow never uses it).
2. On the new app's page, tick **Enable Device Flow** and save. Without it,
   sign-in fails with `github_unavailable` (GitHub answers `device_flow_disabled`;
   GitHub's "Authorizing OAuth apps" page says the flow must be enabled in the
   app's settings; the exact label is not verified here, check the page).
3. Copy the **Client ID** (not a secret) into `.env` as
   `BEEBREAK_GITHUB_CLIENT_ID`. Do not generate or store a client secret.
4. `dc up -d`. `GET /v1/auth/methods` now reports `"github": true`.

Optional: `BEEBREAK_GITHUB_TIMEOUT_S` (default 10), `BEEBREAK_RATE_GITHUB_POLL_IP`
(default 1200 polls per address per rate window), `BEEBREAK_GITHUB_OAUTH_URL` and
`BEEBREAK_GITHUB_API_URL` (only for tests; defaults are GitHub's).

Invite a person by GitHub username (the numeric id is looked up once and is all
that is stored; there is no code to pass on, they just sign in with GitHub):

```sh
dc exec server beebreak-admin invite create --github octocat
```

Export or delete a GitHub account with `account export --github-id <id>` and
`account delete --github-id <id> --yes` (the id is in the export of an
account, or look it up with the same `GET /users/<name>`). An account is either
email or GitHub; to switch someone, delete and re-invite. After enabling GitHub
for the first time, check on your host that a sign-in with no
scope can call `GET /user` (GitHub's docs do not say it outright); it is on
the [untested list](#untested-on-a-real-host).

Changing `.env` takes effect with `dc up -d` (it recreates what changed).
Changing `POSTGRES_PASSWORD` later does **not** change the password inside an
existing database volume; do that in Postgres first.

## First admin invite

Sign-in is by invitation. Moderation is the admin CLI inside the server
container; there is no web UI.

```sh
dc exec server beebreak-admin invite create you@example.com --send
```

(Needs email sign-in on; on a GitHub-only server use `--github` below.)

The code is printed once on stdout either way (it is never stored in clear);
`--send` also emails it. The invitee enters the email and the code in the
companion. `invite list` shows state, `invite revoke <id>` ends an invite.
`--days N` overrides the 7-day validity.

## Backup schedule

**Both scripts need `COMPOSE_PROJECT_NAME` set to a non-empty value.** Without
it they exit with status 2 before any container is touched: the directory-derived
default is the development stack's project, and `restore.sh --force` drops the
database it finds. Production examples below use `beebreak`; use whatever
name your production stack was started with (`dc ps` shows it as the prefix of the
container names). The drill uses its own name, `beebreak-m6drill`.

`scripts/backup.sh` writes `pg_dump --format=custom` of the `postgres` service
to `backups/beebreak-<UTC time>.dump` (mode 0600) and prints size and SHA-256.
Add `--age-recipient <key>` or `--gpg-recipient <id>` to encrypt (the dump is
piped into the encryptor, so no plaintext copy reaches the disk).

A backup holds user data (emails, nicknames, message bodies not yet purged).
Encrypt it, and copy it off the host: a backup on the same disk does not
survive losing the disk. The script does not copy anywhere.

Cron example (daily at 03:15, encrypted, keeping 14 days; the 14 is an example,
not a decided policy):

```cron
15 3 * * * cd /srv/beebreak && COMPOSE_PROJECT_NAME=beebreak scripts/backup.sh --age-recipient age1... >>/var/log/beebreak-backup.log 2>&1 && find backups -name 'beebreak-*' -mtime +14 -delete
```

Check the log now and then: a backup that silently stops is found on the day
it is needed.

**Retention reminder.** A restored backup brings back rows that have since been
purged (messages after 24 h, ended matches after 30 days, resolved reports
after 90 days, and so on) and accounts that were deleted after the backup was
taken. Keeping backups longer than those periods keeps the data longer.
Decide the backup lifetime on purpose, and after any restore see
[Restore](#restore) below.

## Restore

```sh
export COMPOSE_PROJECT_NAME=beebreak   # required; see "Backup schedule"
scripts/restore.sh backups/beebreak-<time>.dump            # into a fresh database
scripts/restore.sh backups/beebreak-<time>.dump.age --age-identity key.txt
scripts/restore.sh backups/beebreak-<time>.dump --force    # replace a database in use
```

The script first builds the `server` image (`docker compose build server`; the
`migrate` service runs that image and never builds it, so a host that has not
built it yet would fail after the restore; a failed build stops the script
before the database is touched), starts `postgres`, **refuses** when the database already has tables
(unless `--force`, which drops them first), stops `server` and `caddy`,
restores, runs `beebreak-server migrate`, and then runs **one purge pass**
(`beebreak-server purge`, which you can also run by hand:
`dc run --rm --no-deps --pull never server beebreak-server purge`) with the current
retention settings. If the restore itself fails, the script says so and how to
retry: the restore is one transaction, so the database is left empty (after a
`--force` drop) or untouched, and `server` and `caddy` stay stopped. It does not start the stack: run `dc up -d`
afterwards.

Why the purge pass: the restored data is as old as the backup, so it contains
rows the live server had already deleted. The pass removes whatever is past
retention again (it prints counts when it deletes something). It is the same
sweep the server runs every minute; it also ends matches and queue entries that
are past their time.

It cannot know about deletions you made after the backup. If you answered an
erasure request (`account delete`) since then, run the deletion again.

### Restore drill

Done once, and repeated after any change to the scripts, the database layout
or the hosting. Run it under its own project name, never the development or
production one (`down -v` destroys that project's database volume). Every
command below runs with `COMPOSE_PROJECT_NAME=beebreak-m6drill` and
`dc` pointed at the drill's compose file; `docs/M6_CHECKLIST.md` M6-C13 has the
guarded `dcd` and `drill` shell functions that do both:

1. Start the stack and sign in once; note a refresh token.
2. `COMPOSE_PROJECT_NAME=beebreak-m6drill scripts/backup.sh`.
3. `COMPOSE_PROJECT_NAME=beebreak-m6drill dc down -v` (destroys the drill's
   database volume).
4. `COMPOSE_PROJECT_NAME=beebreak-m6drill scripts/restore.sh backups/<file>`.
5. `dc up -d`; `curl .../v1/health` returns 200.
6. Sign in again with the same email, and refresh the token noted in step 1.
   Both must work.

**Drill record: 2026-10-16, done, passed.** Run on a local equivalent of the
production stack, not on a host: `compose.prod.yaml` with Caddy removed, the
server port published on `127.0.0.1:18000`, an image tagged `drill`, and SMTP
pointed at a throwaway local listener that records mail (so the sign-in codes
could be read). Caddy, TLS and a real mail provider were therefore not
exercised. All run through `scripts/backup.sh` and `scripts/restore.sh`:

| Step | Result |
|---|---|
| `up -d --build --wait server` | postgres, migrate (exit 0) and server healthy |
| invite, sign-in with the invite code, note refresh token | 202 start, 200 verify |
| `scripts/backup.sh` | 31359-byte `.dump`, mode 0600, SHA-256 printed |
| `down -v` | database volume removed, `/v1/health` unreachable |
| `scripts/restore.sh <dump>` into the fresh volume | restore, migrate and purge pass completed (nothing past retention to purge) |
| re-run `restore.sh` on the now-filled database, no `--force` | refused with exit 1, nothing changed |
| `restore.sh <dump> --force` | dropped and restored again, exit 0 |
| `up -d`, `GET /v1/health` | 200 `{"status":"ok"}` |
| sign-in with the same email | 202 start, 200 verify |
| refresh the token noted before the backup | 200 (the restored data is the live data) |
| gpg round trip: `backup.sh --gpg-recipient`, `restore.sh *.dump.gpg --force` | restored, `/v1/health` 200 |

The drill ran before the purge pass became the `beebreak-server purge`
command; that command and the failure message are covered by tests
(`test_purge_cli.py`, `test_ops_scripts.py`) but were not part of the
drill, so repeat it on the real host. `age` was not installed on the drill machine, so the `--age-recipient` and
`--age-identity` paths were not run. All drill containers, volumes, the image
and the backups were removed afterwards.

## Rotate `BEEBREAK_SERVER_SECRET`

What the secret is used for:

- It keys an HMAC over emailed sign-in codes and invite codes (the database
  stores only these HMACs).
- It is the key from which the successor token pair of a refresh is derived:
  HMAC (with a subkey of the secret) over the raw old refresh token. The
  database never stores the raw token, so a backup plus the secret still does
  not yield a live token. Tokens issued at sign-in are random and stored only
  as SHA-256 hashes.
- A refresh retried within the grace window (`BEEBREAK_REFRESH_GRACE_S`, 90 s)
  gets the same pair back; that is why the pair has to be derivable.

Changing it therefore:

- invalidates every **unredeemed invite** (the code can no longer be verified;
  create new ones),
- invalidates every **sign-in code already emailed** (valid 10 minutes anyway;
  the user asks for a new one),
- makes a refresh retry that straddles the restart, inside the grace window,
  fail the grace check. That revokes the device, and that user signs in again.
  Rare, and limited to refreshes in flight at the moment of the restart.
- makes every **account tombstone** unmatchable (its key is derived from the
  secret), so a banned person whose account was deleted can be invited again
  without the override. The old rows still purge on schedule.

It does **not** sign anyone out: existing access and refresh tokens are checked
by their stored hashes, so signed-in users stay signed in.

Steps: put the new value in `.env`, `dc up -d server`, then re-issue pending
invites. The restart drops open WebSocket connections; the companions
reconnect by themselves. Old backups are unaffected by the secret (they never
held it).

**If the secret leaked:** rotate it as above. An attacker with the secret and
no database access has no token: tokens are random, and derived ones need the
raw old token as well. Revoking every device is therefore not required for the
tokens. With the secret an attacker could forge the HMAC of an invite or
sign-in code, so also re-issue all outstanding invites and look for sign-ins
you did not expect. To force everyone to sign in again
anyway, disable and re-enable the accounts (`user disable` revokes every
device).

## Read the logs

```sh
dc logs -f server              # follow
dc logs --since 1h server | jq # pretty-print (compose.prod.yaml sets JSON)
```

With `BEEBREAK_LOG_FORMAT=json` (set in `compose.prod.yaml`; the local default
is text) each line is one object with `timestamp`, `level`, `logger` and
`message`, plus a `request_id` (a random id per HTTP request, shared by every line that request writes). An unhandled error logs a fixed message plus `exc_type` (the exception class) and `frames` (`file:line:function`), never the exception text or variables, so a 500 can be traced to a code location without exposing request content. Request lines come from the
`beebreak_server.access` logger: method, route **template** (not the real
path), status, duration, and opaque `user=` and `device=` ids. They carry no
query string, header, body, email, nickname, device name, token or client
address, and neither does any other line: logs hold counts and ids only. To
find out who an id is, look it up in the database.

`compose.prod.yaml` caps every service's logs with Docker's `json-file`
driver (`x-logging`: 10 MB per file, 5 files), so logs cannot fill the disk.
Older lines are discarded; ship them elsewhere if you need a longer history.

## Disable a user

```sh
dc exec server beebreak-admin user disable <nickname>
dc exec server beebreak-admin user enable <nickname>
```

Disabling revokes every device of the account, cancels its queue entry and ends
its match within the sweep interval (about a second). Reports are handled with
`report list`, `report show <id>` (prints user-written notes and messages, with
a warning line) and `report resolve <id> dismiss|disable`.

## Answer an appeal or a data request

Appeals, access (export) and erasure (delete) requests on your server arrive at
your `<contact address>`. This runbook assumes a response time of **14 days**
(ADR 0013); change `<response time>` in the privacy notice if you choose
another. Moderation and data requests are handled only through the admin
CLI.

- **Appeal** of a disabled account: confirm the mail comes from the account's
  email address, read the relevant `report show`, and decide.
  `user enable <nickname>` restores the account.
- **Access request:** write the export to a file only you can read, and send it
  to the account's own email address only.

  ```sh
  (umask 077; dc exec -T server beebreak-admin account export user@example.com > export.json)
  ```

  Use the shell redirect: `--out` would write inside the container. The file
  contains user content; delete it after sending.
- **Erasure request:**
  `dc exec server beebreak-admin account delete user@example.com --yes`
  prints table names and row counts. It is refused while the user is queued, in
  a live match or on either side of an open report (resolve first). A disabled
  account needs `--disabled-ok`, and its deletion leaves one tombstone (a keyed
  hash of the email, kept 1 year, `BEEBREAK_TOMBSTONE_RETENTION_S`; the output
  shows `tombstones=1`). `invite create` for that address is then refused with
  exit 1 unless the operator passes `--override-tombstone`, which creates the
  invite and deletes the tombstone. Backups taken earlier still contain the
  account; see the retention reminder under Backup schedule.

## Abuse reports

Reports are not pushed to anyone. They appear only when someone runs
`report list`. Decide, and write here:

- Abuse report owner: `<abuse report owner>`
- Response time: `<abuse report response time>`

See [Alerting](#alerting) for a way to be told when a report is open.

## Untested on a real host

Everything below was tested locally only: on one machine, with Caddy removed or
pointed at a local listener, and fake mail. Check each item on your own host
before you let other people in, and again after you change the host, the proxy
or the compose files.

- **TLS via Caddy.** A public certificate for `BEEBREAK_PUBLIC_HOST`, ports 80
  and 443 reachable, and the WebSocket (`/v1/realtime`) working through Caddy
  from a client on another network.
- **Proxy limits.** A spoofed `X-Forwarded-For` sent to the public address must
  not change the rate-limit key, while the real peer address does
  (`BEEBREAK_TRUSTED_PROXIES`).
- **Two-machine behaviour.** The M4 and M5 checklists ran both companions on one
  machine against a local proxy. Repeat the match, chat, offline-hold (a real
  network cut on the second machine), extension, block and report steps with
  two machines (`docs/M4_CHECKLIST.md`, `docs/M5_CHECKLIST.md`).
- **Restore drill on a host.** The drill in [Restore drill](#restore-drill) ran
  without Caddy or real mail, and without the `age` path. Run it on the host,
  with an off-host copy of the backup.
- **IPv6 client address.** `compose.prod.yaml` trusts Caddy at one IPv4 address
  on the `edge` network. What the server records as `remote_ip` for a client
  that arrives over IPv6 was not tested. Either enable IPv6 on `edge` and check
  the recorded address is the client's, or publish no AAAA record for the host.
- **The `172.29.7.0/24` network.** `edge` uses this fixed subnet. Check that it
  is free on your provider (`docker network inspect` and your provider's
  private ranges); if not, change it and the `BEEBREAK_TRUSTED_PROXIES` value
  together.
- **Caddy error-log contents.** The error log can include the client address,
  the host and the requested path. Look at what it holds on a real 502 (stop the
  server briefly) and decide whether to cut it down. The privacy notice says it
  may include these.
- **GitHub `GET /user` with no scope.** GitHub's docs do not say outright that a
  token with no scope can call it. Sign in with GitHub once on your server and
  confirm.
- **Transitive versions.** `uv tool install` from git ignores `uv.lock`, so
  transitive dependencies resolve to whatever is newest at install time.
- **Real mail.** A real provider sends the sign-in code, with SPF and DKIM set
  before any real send, and `BEEBREAK_SMTP_TLS` set for starttls or implicit TLS.

## Alerting

Guidance only; the project ships no monitoring. Use what you already run, or
self-hosted tools. Three things are worth knowing about.

- **The server is down.** Probe `https://<BEEBREAK_PUBLIC_HOST>/v1/health` from
  a machine other than the host, every minute or so, and alert on anything but
  HTTP 200. A self-hosted tool such as Uptime Kuma does this on a second
  machine; a cron job on another host with `curl -fsS` and `mail` also works.
  Probing from the host itself misses a dead host.
- **A backup failed.** The backup cron line in [Backup schedule](#backup-schedule)
  appends to a log. Make cron mail you on failure: set `MAILTO=` at the top of
  the crontab and drop the log redirect, so any output from a failed run is
  mailed, or end the line with `|| echo "beebreak backup failed" | mail -s
  "beebreak backup" you@example.com`. Also alert when the newest file in
  `backups/` is more than a day old.
- **A report is open.** `report list` prints open reports and nothing when there
  are none. A cron job can count its lines and mail you only the count, never
  the report text:

  ```sh
  n=$(cd /srv/beebreak && docker compose -f compose.prod.yaml exec -T server beebreak-admin report list | wc -l)
  [ "$n" -gt 0 ] && echo "$n open Beebreak reports" | mail -s "beebreak reports" you@example.com
  ```

  Your abuse report response time starts when this mail arrives.

## Single-worker rule

The server runs as exactly **one worker in one container**; never scale it,
never set `--workers` or `WEB_CONCURRENCY` above 1. `beebreak-server serve`
refuses any other count and exits with status 2.

Why (restated from `compose.prod.yaml` header comment, `docs/ARCHITECTURE.md`
section on the M6 deployment, and the refusal text in
`apps/server/src/beebreak_server/cli.py`): the realtime hub, presence, rate
limiters and the matchmaker lock live in the server process. A second replica
or worker would split connected users between processes, so waiting users
could not be matched with each other, and rate-limit counters would be per
process. `docs/ADR/0008-in-process-matcher.md` adds that the matcher is a
single in-process component and that uniqueness of matches is kept by database
indexes, so a second process "loses at the index and the pair is skipped"; it
defers a multi-instance design (Redis or pub/sub) until more than one instance
is needed.

Practical consequence: a restart or deploy drops every WebSocket; companions
reconnect and receive a `sync` frame.
