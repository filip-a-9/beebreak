# M6 manual validation checklist

Spec refs: ARCHITECTURE §4 (privacy), §14 (M6 gate), §16 (open items); ADRs
0009 (Caddy), 0010 (CI), 0011 (install route), 0012 (auth row retention),
0013 (appeals and data requests), 0014 (device name), 0016 (export and
delete), 0017 (privacy notice); `docs/DATA_HANDLING.md`,
`docs/PRIVACY_NOTICE.md`, `docs/RUNBOOK.md`; the M6 specs `M6-01` to `M6-09`
(done and deleted; this checklist is `M6-10`). Gate: reproducible
installations and vetted disclosure and retention before the closed beta
(M7). M6 makes the server deploy-ready, not deployed: nothing here needs a
public host.

Platform under test: WSL2 Linux, Docker, Claude Code, companion windows via
WSLg, plus one freshly imported WSL distro for the install steps. Run every
step from the repository root unless it says otherwise. Record results in the
report template at the end. Report blocked platforms as pending, not as
passed. Step IDs are stable; do not renumber.

Written against the M6 specs and the M4 and M5 checklists, whose stack,
terminals and two accounts this one reuses. Failing steps go back to the
owning spec (the owner is named in each step). Steps whose result depends on
a value Filip has not given yet (`<contact address>`, `<operator name>`,
`<owner>/<repo>`, the abuse-report owner) are recorded as `blocked`, never
`fail`. Items that need a public host, TLS or a second machine are not steps
here: see "Moved to the rehearsal deploy" near the end.

## How the steps work

**Terminals.**

- T1: the development Docker stack (`docker compose ...`,
  `breakroom-admin` commands, git and `gh`), in the main distro.
- T2: companion A in the main distro (default state and runtime directories),
  account `FilipM6`.
- T3: companion B in the main distro (separate directories, see M6-C11),
  account `BeaM6`.
- T4: the fresh distro `breakroom-clean` (M6-C07 to M6-C10): its shell, the
  companion and one Claude Code session.
- PS: a Windows PowerShell window. Commands there are **run by Filip** (this
  checklist never asks an agent to run them).

**Two companions on one machine.** As in M4: companion B gets its own
`XDG_RUNTIME_DIR` and `XDG_STATE_HOME`; never set them in T2. In the main
distro run the companion from the venv script, not `uv run`:
`.venv/bin/breakroom-companion`. In the fresh distro it is the installed
command `breakroom-companion` (M6-C08).

**Window texts** (unchanged from M3 to M5): account row `Not signed in`,
`Signing in…`, `Connecting…`, `Connected as <nickname>`, `Offline – retrying`,
`Finish setup`; a note `Stored unencrypted` shows when the refresh token is
kept in the 0600 file instead of the keyring. Buttons `Sign in`, `Settings`,
`Sign out`; `Find a break partner`; `Open chat`. Sign-in dialog (title `Sign
in to Breakroom`): page 1 `Email` and `Invite code (first sign-in only)`; page
2 `We emailed you an 8-character code. Type it here.`, `Code`, the line `Name
for this device. Letters, digits, space, . _ and -, up to 40 characters.` over
an edit box that starts with `WSL device`, and a button `Show what is sent`;
page 3 the nickname; page 4 the three consent boxes and `Show what is shared`;
then `You are all set.`. The preview window is titled `What this app shares`.

**Admin commands** (inside the server container of the development stack):

```
docker compose exec server breakroom-admin invite create EMAIL
docker compose exec server breakroom-admin devices
docker compose exec server breakroom-admin account export EMAIL
docker compose exec server breakroom-admin account delete EMAIL --yes
docker compose exec server breakroom-admin matches
docker compose exec server breakroom-admin report list
```

`invite create` prints the invite code once (`XXXX-XXXX`). `devices` prints one
line per **active** (not revoked) device: `nickname<TAB>device name<TAB>
connected=yes|no<TAB>last seen`; a revoked device disappears from it, so
revocation is shown with the first Postgres query below. `account export EMAIL` prints
the account's data as JSON on stdout (it contains user content; `--out PATH`
would write inside the container, so redirect on the host instead).
`account delete` refuses without `--yes` (exit 2); a refusal for a busy or
disabled account prints one fixed line to stderr and exits 1; on success it
prints one line of `table=count` pairs and nothing else. Sign-in codes arrive
in Mailpit at `http://127.0.0.1:8025` (open it in the Windows browser; M3
already relies on this).

**Postgres queries** (the second is `TB` in M6-C14):

```
docker compose exec -T postgres psql -U breakroom breakroom -c "SELECT u.nickname, d.name, d.revoked_at IS NOT NULL AS revoked FROM devices d JOIN users u ON u.id = d.user_id ORDER BY u.nickname, d.created_at"
docker compose exec -T postgres psql -U breakroom breakroom -c "SELECT table_name FROM information_schema.tables WHERE table_schema = 'public' ORDER BY 1"
```

**Test identities** (new for this run, never the M3 to M5 accounts):
`filip-m6@example.com` (A, nickname `FilipM6`), `bea-m6@example.com` (B,
nickname `BeaM6`), `clean@example.com` (fresh distro, nickname `Clean`),
`drill@example.com` (restore drill, nickname `Drill`). Create an invite for
each with `invite create`. The old accounts must not be reused: their device
rows were written before M6-06 and may hold a hostname, which would break
M6-C11, M6-C12 and M6-C14. Message texts below are synthetic.

**Spending.** The Claude Code sessions in M6-C09 are interactive sessions on
the claude.ai plan, not `claude -p` and not API billing. Keep them short.

## Setup

**M6-C01 — Install and automated checks.**

```
uv sync && uv run ruff format --check . && uv run ruff check . && uv run mypy packages apps tests tools && uv run pytest
```

Then the Postgres suite, as in M5-C01 (own database, needs `.env`):

```
docker compose up -d postgres
(set -a; . ./.env; set +a; docker compose exec postgres createdb -U breakroom breakroom_test
BREAKROOM_TEST_DATABASE_URL="postgresql+psycopg://breakroom:${POSTGRES_PASSWORD}@127.0.0.1:5432/breakroom_test" uv run pytest tests/server tests/privacy tests/e2e)
```

Expect: both runs green. `tests/privacy/test_privacy_corpus.py` and
`tests/privacy/test_outbound_leaks.py` must be among the collected files of
both runs. If `createdb` says the database exists, carry on. (The full local
run is the slow one; CI runs the same suite split into groups, M6-C03.)

Result (pass / fail / blocked):

**M6-C02 — Development stack up, migration at head.**

```
mkdir -p reports
cp -n .env.example .env     # first time only; replace the two secrets as in M3-C02
docker compose up -d --build
docker compose ps
curl -s http://127.0.0.1:8000/v1/health
docker compose exec -T postgres psql -U breakroom breakroom -c "SELECT version_num FROM alembic_version"
docker compose exec -T postgres psql -U breakroom breakroom -c "SELECT table_name FROM information_schema.tables WHERE table_schema = 'public' ORDER BY 1"
```

Expect: `postgres`, `mailpit` and `server` running; health prints
`{"status":"ok"}`; the migration query prints `0004`; the table query prints
15 names: `alembic_version` plus the 14 tables of DATA_HANDLING (`auth_challenges`,
`beta_invites`, `blocks`, `deleted_account_tombstones`, `devices`, `match_participants`, `matches`,
`messages`, `queue_entries`, `refresh_tokens`, `report_messages`, `reports`,
`user_preferences`, `users`). A different version or table list is a `fail`
against the spec that changed the schema. An `.env` made in M3 lacks the
production keys that `.env.example` has now (`BREAKROOM_PUBLIC_HOST`,
`BREAKROOM_SMTP_*`, `BREAKROOM_MAIL_FROM`): add them from `.env.example`
before M6-C13, which needs them.

Result (pass / fail / blocked):

## CI (M6-01, ci-speed)

These three steps need the GitHub repository (private) and `gh` logged in.
Pushing and opening a pull request are Filip's to approve. The workflow runs
on `pull_request`; its jobs are `groups`, `lint`, `collection`,
`tests (unit)`, `tests (server)`, `tests (rest)` and the single gate job
`checks`. Precondition: `ci-speed` is merged into `main` (until then the
`groups`, `collection` and `tests (...)` jobs exist only on its branch) and
`git status` prints nothing. Do this on a throwaway branch cut from the
current `main`; never push `main`. `gh pr checks --watch` can answer `no
checks reported` in the first seconds after a push, so each watch below goes
through a function that waits until jobs are listed. Define it once in T1:

```
wait_checks() { until gh pr checks 2>/dev/null | grep -q .; do sleep 5; done; gh pr checks --watch; }
```

**M6-C03 — A failing test turns `checks` red; reverting it turns it green.**

```
git switch main && git pull --ff-only
git switch -c m6-c03-ci-check
printf 'def test_deliberate_failure() -> None:\n    raise AssertionError("deliberate CI check")\n' > tests/unit/test_m6_deliberate_failure.py
git add tests/unit/test_m6_deliberate_failure.py
git commit -m "Add a deliberately failing test for the CI check"
git push -u origin m6-c03-ci-check
gh pr create --draft --title "M6-C03 CI check, do not merge" --body "Deliberate failing test. Close this, do not merge."
wait_checks
```

Expect: the run ends with `tests (unit)` failed and `checks` failed;
`groups`, `lint` and `collection` pass (the new file is inside `tests/unit`,
which a group lists). `gh pr checks --watch` exits non-zero. The `results:`
line of `checks` is only in the job log: `gh run view --log-failed` (pick the
run of this branch from `gh run list --branch m6-c03-ci-check`) shows
`results: ...` with a non-success entry. Then revert it and watch again:

```
git revert --no-edit HEAD
git push
wait_checks
```

Expect: all jobs pass and `checks` is green (exit 0). Keep the branch and
pull request for the next step.

Result (pass / fail / blocked):

**M6-C04 — A test in a directory no group lists turns `collection` red.**
On the same branch:

```
mkdir -p tests/m6_ungrouped
: > tests/m6_ungrouped/__init__.py
printf 'def test_ungrouped() -> None:\n    assert sum([1, 1]) == 2\n' > tests/m6_ungrouped/test_ungrouped.py
git add tests/m6_ungrouped
git commit -m "Add a test outside every CI group"
git push
wait_checks
```

Expect: `collection` fails with the error `Test groups do not cover every
test exactly once (< missing, > repeated)` (the current text in
`.github/workflows/ci.yml`; read it there if the workflow changed) and a
`diff` whose `<` side names
`tests/m6_ungrouped/test_ungrouped.py::test_ungrouped` (collected by plain
pytest, in no group; the job strips parameter ids from test names). Read the
job log with `gh run view --log-failed`; `checks` fails; the
three `tests (...)` jobs and `lint` pass (the new test is simply never run,
which is why `collection` exists). Then clean up:

```
git revert --no-edit HEAD
git push
wait_checks
gh pr close --delete-branch
git switch main
git branch -D m6-c03-ci-check 2>/dev/null || true
```

Expect: `checks` green again after the revert; the pull request is closed,
nothing was merged, and `git status` is clean.

Result (pass / fail / blocked):

**M6-C05 — Merges are gated by convention only.** Branch protection is not
available on a private repository on GitHub Free (M6-01).

```
gh api repos/{owner}/{repo}/branches/main/protection; echo "exit $?"
```

Expect: an error, not a protection rule: HTTP 403 (feature needs GitHub Pro
or a public repository) or HTTP 404 (branch not protected). Either confirms
there is no technical gate, so **a merge to `main` must wait for a green
`checks` by hand**. Record the status code. A 404, or a rule being returned, means the premise
of this item has changed (the plan or the repository visibility changed): do
not mark `pass`; record it in `deviations` and tell Filip, because the
project state assumes no protection is possible.

Result (pass / fail / blocked):

## Install and uninstall (M6-02)

The repository is private, so the install does **not** use the README's
`git+https://github.com/<owner>/<repo>` line (that path is untested until the
repository is public, and `<owner>/<repo>` is a placeholder). It uses the
release wheels from `scripts/build_release.sh`, named as explicit paths, which
is the README's Install section as written today. Follow the README and
nothing else: **every extra command you need beyond the README is a finding**
(write it in the step note). The server for this part is the development
stack of M6-C02 running in the main distro; all WSL2 distros share one
network, so the fresh distro is expected to reach it at `http://127.0.0.1:8000`
(if it does not, record that, it is a finding for the README or this
checklist, not a pass).

**M6-C06 — Build the release bundle (main distro, T1).**

```
scripts/build_release.sh
sha256sum dist/breakroom-release.tar.gz
tar tzf dist/breakroom-release.tar.gz | sort
```

Expect: the script prints `built:` and lists `SHA256SUMS`,
`breakroom-release.tar.gz`, `breakroom_agent_bridge-*.whl`,
`breakroom_companion-*.whl`, `breakroom_shared-*.whl` and `marketplace`.
The tarball lists the three wheels, `SHA256SUMS` and `marketplace/` (with
`.claude-plugin/marketplace.json` and `integrations/claude-code/`). Write the
SHA-256 down for M6-C08. `dist/` is git-ignored.

Result (pass / fail / blocked):

**M6-C07 — Fresh distro (PS, run by Filip).** Import a distro that has never
been used, so nothing of Breakroom, uv or Claude Code exists in it. **Never
export a distro you work in** (it holds your files and credentials). Needs
WSL 2.4.4 or newer for `--name` (`wsl --version`; write the version down).

```
wsl --install Ubuntu-24.04 --name breakroom-clean --no-launch
```

On an older WSL, download the official Ubuntu WSL root filesystem tarball and
import that instead (`wsl --import breakroom-clean C:\wsl\breakroom-clean
<rootfs tarball> --version 2`); do not use `wsl --export` for this. Then set
up the user:

```
wsl -d breakroom-clean -u root -- bash -c "useradd -m -s /bin/bash tester && echo 'tester ALL=(ALL) NOPASSWD:ALL' > /etc/sudoers.d/tester && printf '[user]\ndefault=tester\n' >> /etc/wsl.conf"
wsl --terminate breakroom-clean
wsl -d breakroom-clean
```

(If the distro opens as root, the `useradd` command has not taken effect:
check `/etc/wsl.conf`.) The last command opens T4 as `tester`. Hand the
tarball over through the Windows temp folder and leave it there: M6-C08
unpacks from that path, so no tarball lands in the fresh home directory (the
README assumes it in the current directory; that is a test-setup deviation,
not a finding):

```
# in the main distro
mkdir -p /mnt/c/Temp && cp dist/breakroom-release.tar.gz /mnt/c/Temp/
# in T4
sha256sum /mnt/c/Temp/breakroom-release.tar.gz
```

Expect: the SHA-256 in T4 equals the one from M6-C06 (this is the README's
"compare with the release page" step; there is no release page yet). `echo
$WSL_DISTRO_NAME` prints `breakroom-clean`, and `ls ~/.local ~/.config`
printing `No such file or directory` is the expected pass on a new user
(otherwise it shows no `breakroom`, `uv` or `claude`). The fresh distro does not have uv and
Claude Code: install them per their own official instructions (the README
lists them as prerequisites and does not say how). Write down every package
you had to add (for example `curl`, `ca-certificates`, Qt libraries); each is
a finding for the README. The companion needs Qt system libraries on a
fresh distro. Record the finding first (the README names none), then continue
by installing the set CI uses plus the xcb cursor library: `sudo apt-get
install -y --no-install-recommends libegl1 libgl1 libxkbcommon0 libfontconfig1
libdbus-1-3 libglib2.0-0t64 libxcb-cursor0`. Check `~/.local/bin` is on `PATH`
(`uv tool update-shell` adds it, as the README says).

Result (pass / fail / blocked):

**M6-C08 — Install from the wheels (T4).** Exactly the README commands, with
the explicit wheel paths:

```
mkdir -p ~/breakroom-release && tar xzf /mnt/c/Temp/breakroom-release.tar.gz -C ~/breakroom-release
(cd ~/breakroom-release && sha256sum -c SHA256SUMS)
R=~/breakroom-release
uv tool install $R/breakroom_companion-*.whl \
  --with $R/breakroom_shared-*.whl \
  --with $R/breakroom_agent_bridge-*.whl \
  --with-executables-from breakroom-agent-bridge
command -v breakroom-companion breakroom-hook breakroom-mcp
breakroom-companion install
claude plugin marketplace add ~/breakroom-release/marketplace
claude plugin install breakroom@breakroom
```

At the `Server URL` prompt press Enter (the default is
`http://127.0.0.1:8000`). Expect: `sha256sum -c` prints `OK` for the three
wheels; `command -v` prints three paths under `~/.local/bin`;
`breakroom-companion install` prints `saved server URL`, then `Next, add the
Claude Code plugin:` with the two `claude plugin ...` commands; the two
`claude plugin` commands succeed. `ls -l ~/.local/state/breakroom/install.json`
shows mode `-rw-------`.

Autostart, a separate measurement (the README says WSL2 may not process
autostart entries at all):

```
breakroom-companion install --server-url http://127.0.0.1:8000 --autostart
cat ~/.config/autostart/breakroom.desktop
```

Expect `added autostart entry` among the output lines and a desktop entry
with `Name=Breakroom` and an `Exec=` line that holds the full path of
`breakroom-companion`. Then in PS `wsl --terminate breakroom-clean`, reopen the
distro and run `pgrep -af breakroom-companion`. Record whether the companion
started by itself (`autostart_started_companion` in the report). The step
passes whether it did or not; it fails only if the entry is wrong or the
install errors. If the companion did not start, the README's warning is
accurate. If it did start, the README's "may not start" line is outdated:
note it. Leave the autostart entry in place for M6-C10 to remove.

Result (pass / fail / blocked):

**M6-C09 — Hooks fire, the MCP tool answers, the companion signs in.** In
the main distro (T1) make an invite:

```
docker compose exec server breakroom-admin invite create clean@example.com
```

In T4 start the companion (`breakroom-companion`). If it does not start with
an error (an `ImportError` naming `libEGL` or `libGL`, or `Could not load
the Qt platform plugin "xcb"`), write down the missing system library and
install it: that is a finding (the README names no system libraries; the list
is in M6-C07). Expect the window
`Not signed in`. Click **Sign in**; enter `clean@example.com` and the invite
code; take the code from Mailpit (`http://127.0.0.1:8025`); keep the device
name `WSL device`; nickname `Clean`; save the privacy page with all three
boxes off. Expect `Connected as Clean`. Then, in the main distro:

```
docker compose exec server breakroom-admin devices
```

Expect one line starting `Clean`, then `WSL device`, then `connected=yes`.

Now the Claude Code part, in a new terminal in T4, from a scratch directory
outside `~/breakroom-release`:

```
mkdir -p ~/scratch && cd ~/scratch
claude
```

(no `--plugin-dir`: the plugin comes from the marketplace install). Send:
`Call the breakroom_status tool and show the raw result`. Expect the keys
`companion`, `invitation`, `last_event_at`, `activity`, `estimate` and
`queue`; `companion` is `running` (not `not_running`), `last_event_at` is a
timestamp, not `null` (the hooks of this very session reached the companion),
and `queue` is `none`. `/mcp` lists the server as connected (it shows as `plugin:breakroom:breakroom`). To show the
hooks again after the tool call, ask for the same tool a second time and
expect a later `last_event_at`. Exit Claude Code. Leave the companion running
for M6-C10.

Result (pass / fail / blocked):

**M6-C10 — Uninstall leaves nothing, and the device is revoked.** In T4,
before uninstalling, note what exists:

```
ls -A ~/.local/state/breakroom "${XDG_RUNTIME_DIR:-/tmp/breakroom-$(id -u)}/breakroom" ~/.config/autostart
```

Expect (companion still running): `companion-state.json`, `install.json` and,
if the window showed `Stored unencrypted`, `credentials.json` in the state
directory; `companion.sock` and `salt` in the socket directory;
`breakroom.desktop` in the autostart directory. Then the README's Uninstall
block, in order:

```
breakroom-companion uninstall
claude plugin uninstall breakroom@breakroom
claude plugin marketplace remove breakroom
uv tool uninstall breakroom-companion
rm -rf ~/breakroom-release
```

Expect from `breakroom-companion uninstall`, one fixed line each and exit 0:
`stopped the running companion`, `revoked this device on the server`,
`cleared stored login`, `removed state directory (state, credentials, install
record)`, `removed socket directory (socket, salt)`, `removed autostart
entry`. The companion window closes. Run `breakroom-companion uninstall` a
second time before `uv tool uninstall` (so run the first line of the block
above, then this, then the other four):

```
breakroom-companion uninstall; echo "exit $?"
```

Expect exit 0, no traceback and no `NOT revoked` line (`no stored login;
nothing to revoke on the server` is right). It prints the `removed ...` lines
again, because it recreates the two directories before removing them; that is
not a leftover. Then verify nothing is left:

```
find ~ /tmp -maxdepth 5 -iname '*breakroom*' -not -path "$HOME/.cache/uv/*" -not -path "$HOME/.claude/*" 2>/dev/null
ls -d ~/.claude/plugins/cache/*breakroom* ~/.claude/plugins/marketplaces/*breakroom* 2>&1
ls -d "${XDG_RUNTIME_DIR:-/tmp/breakroom-$(id -u)}/breakroom" ~/.local/state/breakroom ~/.config/autostart/breakroom.desktop 2>&1
command -v breakroom-companion breakroom-hook breakroom-mcp
```

Expect: `find` prints nothing (the release tarball is outside the home
directory, in `/mnt/c/Temp`, on purpose); the `ls` lines all say `No such file
or directory`. Anything under `~/.claude/` is Claude Code's own cache: if
`claude plugin uninstall` left a `breakroom` folder there, record it as a
Claude Code leftover in the note, not as a Breakroom `fail`; `command -v` prints nothing. Keyring: if the window showed no
`Stored unencrypted` note the token was in a keyring, so also run
`secret-tool search service breakroom` if that tool exists (it should print
nothing); otherwise record "file store only, no keyring". Then in the main
distro:

```
docker compose exec server breakroom-admin devices
docker compose exec -T postgres psql -U breakroom breakroom -c "SELECT u.nickname, d.name, d.revoked_at IS NOT NULL AS revoked FROM devices d JOIN users u ON u.id = d.user_id WHERE u.nickname = 'Clean'"
```

Expect: `devices` no longer lists `Clean`; the query shows one row, `Clean`,
`WSL device`, `revoked` `t`. (The spec words this as "shows revoked in
`breakroom-admin devices`"; that command lists only active devices, so the
query is the proof: see the report's deviations.) Last, in PS remove the
distro: `wsl --unregister breakroom-clean`.

Result (pass / fail / blocked):

## Device name (M6-06)

**M6-C11 — The server never sees the hostname; the edited name is stored.**
Needs the stack of M6-C02, two new accounts (create the invites for
`filip-m6@example.com` and `bea-m6@example.com` with `invite create`; see
"Test identities") and two companions in the main distro. Note the start time
in T1: `T0=$(date -u +%Y-%m-%dT%H:%M:%SZ)`. T2 (A, default directories):
`.venv/bin/breakroom-companion`. T3 (B):

```
export B_ROOT="${B_ROOT:-$(mktemp -d)}"
mkdir -p -m 700 "$B_ROOT/run" "$B_ROOT/state"
XDG_RUNTIME_DIR="$B_ROOT/run" XDG_STATE_HOME="$B_ROOT/state" .venv/bin/breakroom-companion
```

(Always start B with this command.) B starts `Not signed in`; if A's default
directories still hold an old login, click **Sign out** in A first (the device
name is chosen only at sign-in). In A: sign in as `filip-m6@example.com`; on
the code page **replace** the device name with `FilipM6 test laptop` and click **Show what is sent**. Expect the
preview `What this app shares` to list under `Sent when you sign in`
`Email`, `Nickname`, and `Device name: FilipM6 test laptop`; type another name
and watch the line follow. Finish the sign-in; after it the preview line reads
`Device name: the name you chose at sign-in`. In B: sign in as
`bea-m6@example.com` and leave the name `WSL device` (open **Show what is sent**:
`Device name: WSL device`). Check the validation once in B's dialog before you finish it: type a valid
code first and see **Next** enabled, then change the device name to `bad/name`
and see **Next** disabled, then restore `WSL device`. Save each privacy
page with all boxes off; wait for `Connected as FilipM6` and `Connected as BeaM6`.

```
docker compose exec server breakroom-admin devices
docker compose exec -T postgres psql -U breakroom breakroom -c "SELECT u.nickname, d.name, d.revoked_at IS NOT NULL AS revoked FROM devices d JOIN users u ON u.id = d.user_id ORDER BY u.nickname, d.created_at"
echo "hostname hits in this run's device rows: $(docker compose exec -T postgres psql -U breakroom breakroom -At -c "SELECT d.name FROM devices d JOIN users u ON u.id = d.user_id WHERE u.email_canonical LIKE '%-m6@example.com'" | grep -ci "$(hostname)")"
echo "hostname hits in the server log since the start: $(docker compose logs --since "$T0" server 2>&1 | grep -ci "$(hostname)")"
```

Expect: `devices`
shows `FilipM6	FilipM6 test laptop	connected=yes` and `BeaM6	WSL device
connected=yes`; the query shows the same names with `revoked` `f`; both hostname
counts print `0` (they cover only rows and log lines created in this run, which
is why old accounts are not reused). If your hostname is a common word that legitimately occurs
in the dump, use a longer unique part of it and say so in the note. A count
above `0` is a `fail` against M6-06 (read the matching line first).

Result (pass / fail / blocked):

## Data request (M6-04)

**M6-C12 — Export, then delete a test account; the peer's history remains.**
With A (`FilipM6`) and B (`BeaM6`) connected (M6-C11), click **Find a break
partner** in both so they match. Send `export-test A1` from A and
`export-test B1` from B. While the chat is live:

```
mkdir -p reports
(umask 077; docker compose exec -T server breakroom-admin account export bea-m6@example.com > reports/m6-bea-export.json)
python3 -c "import json; d=json.load(open('reports/m6-bea-export.json')); print(sorted(d)); print([m['body'] for m in d['messages']]); print(d['user']['nickname'], d['devices'][0]['name'])"
ls -l reports/m6-bea-export.json
docker compose exec server breakroom-admin account delete bea-m6@example.com; echo "exit $?"
docker compose exec server breakroom-admin account delete bea-m6@example.com --yes; echo "exit $?"
```

Expect: the key list is `blocks, challenges, devices, export_version,
invites, matches, messages, preferences, queue_entries, reports_against,
reports_filed, user`; the bodies list holds only Bea's own message(s)
(`export-test B1`, never `export-test A1`); the last print line is `BeaM6 WSL
device`; the file mode is `-rw-------`. No `hash`, `hmac` or token appears:
`grep -ciE 'hash|hmac|token' reports/m6-bea-export.json` prints `0`. The
first `delete` exits 2 with `refusing to delete without --yes`; the second
exits 1 with one line saying the account is queued, in a live match or has an
open report (the chat is live) and the account is untouched.

Now End the chat in A (**End**, then **Yes**) and click `Close`/OK in both
windows. Take Filip's "before" export only now: ending a chat changes the
match's `ended_at` and `end_reason`, so an export taken during the live chat
would never compare equal. Then delete:

```
(umask 077; docker compose exec -T server breakroom-admin account export filip-m6@example.com > reports/m6-filip-before.json)
docker compose exec server breakroom-admin account delete bea-m6@example.com --yes; echo "exit $?"
docker compose exec server breakroom-admin account export bea-m6@example.com; echo "exit $?"
(umask 077; docker compose exec -T server breakroom-admin account export filip-m6@example.com > reports/m6-filip-after.json)
python3 -c "import json; a=json.load(open('reports/m6-filip-before.json')); b=json.load(open('reports/m6-filip-after.json')); print(a['messages']==b['messages'], a['matches']==b['matches'], len(b['matches']), [m['body'] for m in b['messages']])"
docker compose exec -T postgres psql -U breakroom breakroom -c "SELECT count(*) FROM matches"
docker compose exec server breakroom-admin devices
```

Expect: `delete` exits 0 and prints one line of `table=count` pairs (names
`report_messages reports messages queue_entries match_participants matches
blocks refresh_tokens devices user_preferences auth_challenges beta_invites
users`) with `users=1` and `matches=0` (the match has another participant);
the export of BeaM6 now exits 1 with `no such account`; Filip's export compares
`True True`, shows one match and the body `['export-test A1']`; the `matches`
count is at least `1`; `devices` no longer lists BeaM6. B's companion drops to
`Offline – retrying` or `Not signed in`. `reports/` is never committed; delete
the export files when done (M6-C17). Bea's account no longer exists after this
step; M6-C14 needs only A.

Result (pass / fail / blocked):

## Restore drill (M6-08)

**M6-C13 — Backup, destroy, restore, sign in again.** Per `docs/RUNBOOK.md`
("Restore drill"), on a local copy of the production stack. It does not
exercise Caddy's real certificate or a real mail provider (those move to the
rehearsal deploy). This needs **its own Compose project name**: the
production file and the development file share the directory name, so
without `COMPOSE_PROJECT_NAME` the drill would share (and `down -v` would
destroy) the development database volume. `backup.sh` and `restore.sh` exit 2
without `COMPOSE_PROJECT_NAME`, but still fall back to `compose.prod.yaml`
when `BREAKROOM_COMPOSE_FILE` is missing, and `restore.sh --force` runs `drop
schema public cascade`. So nothing here relies on exported variables. Two shell
functions pass both values on every call, and `drill` refuses to run unless the
merged file's project name is `breakroom-m6drill` (a python one-liner reads it,
because `jq` is not installed on this machine). Define them in the shell you
use for this step:

```
mkdir -p reports
cat > reports/compose.m6-drill.yaml <<'EOF'
services:
  mailpit:
    image: axllent/mailpit@sha256:e22dce5b36f93c77082e204a3942fb6b283b7896e057458400a4c88344c3df68
    ports:
      - "127.0.0.1:18025:8025"
    networks: [edge]
  server:
    ports:
      - "127.0.0.1:18000:8000"
    environment:
      BREAKROOM_SMTP_HOST: mailpit
      BREAKROOM_SMTP_PORT: "1025"
      BREAKROOM_SMTP_TLS: none
      BREAKROOM_SMTP_USERNAME: ""
      BREAKROOM_SMTP_PASSWORD: ""
  caddy:
    environment:
      BREAKROOM_PUBLIC_HOST: localhost
EOF
(umask 077; COMPOSE_PROJECT_NAME=breakroom-m6drill docker compose -f compose.prod.yaml -f reports/compose.m6-drill.yaml config > reports/compose.m6-drill-merged.yaml)
dcd() { COMPOSE_PROJECT_NAME=breakroom-m6drill docker compose -f reports/compose.m6-drill-merged.yaml "$@"; }
drill() {  # run a script against the drill project only
  local name
  name=$(COMPOSE_PROJECT_NAME=breakroom-m6drill docker compose -f reports/compose.m6-drill-merged.yaml config --format json | python3 -c "import json,sys; print(json.load(sys.stdin)['name'])")
  [ "$name" = breakroom-m6drill ] || { echo "REFUSING: project is '$name', not breakroom-m6drill" >&2; return 1; }
  BREAKROOM_COMPOSE_FILE=reports/compose.m6-drill-merged.yaml COMPOSE_PROJECT_NAME=breakroom-m6drill "$@"
}
drill true && echo "guard ok"
```

Expect `guard ok`. If it prints `REFUSING`, stop: the merged file has no
`name`, or compose prints it differently. Record that as a finding and do not
run the scripts without the guard.

(The scripts take one compose file, which is why the two files are merged
first. The mail settings blank the SMTP credentials because the stand-in
Mailpit has no TLS; the production file still needs `.env` to define every
variable, which a copy of `.env.example` with the two secrets replaced does.
The merged file contains your secrets: it lives in `reports/`, which is
never committed, and was written with mode 600. Caddy publishes ports 80 and
443 on all interfaces (`0.0.0.0`) during the drill, not only on loopback; if
they are taken, stop what uses them.) Then:

1. Start and sign in once. A third companion with its own directories:

   ```
   dcd up -d --build --wait
   dcd ps -a
   dcd exec server breakroom-admin invite create drill@example.com
   export D_ROOT="${D_ROOT:-$(mktemp -d)}"
   mkdir -p -m 700 "$D_ROOT/run" "$D_ROOT/state"
   BREAKROOM_SERVER_URL=http://127.0.0.1:18000 XDG_RUNTIME_DIR="$D_ROOT/run" XDG_STATE_HOME="$D_ROOT/state" .venv/bin/breakroom-companion
   ```

   Sign in as `drill@example.com` with the invite code and the code from
   Mailpit at `http://127.0.0.1:18025`; nickname `Drill`; privacy page all
   off; wait for `Connected as Drill`; then stop the companion with Ctrl+C.
   Expect `dcd ps -a`: `postgres` and `server` healthy, `caddy` up, `migrate`
   exited 0 (`ps` without `-a` hides exited containers); `dcd exec server breakroom-admin devices` lists `Drill`.

2. Back up, keeping the dump out of the repository tree's tracked paths
   (`backups/` is not in `.gitignore`, so use `--out-dir`):

   ```
   drill scripts/backup.sh --out-dir reports/m6-backups
   stat -c '%a %n' reports/m6-backups/*
   ```

   Expect `file:`, `size:` and `sha256:` lines, and mode `600`.
3. Destroy: `dcd down -v`. Expect the containers and the `postgres-data` and
   `caddy-data` volumes of the `breakroom-m6drill` project gone;
   `docker volume ls | grep breakroom-m6drill` prints nothing and
   `curl -s http://127.0.0.1:18000/v1/health` fails to connect. The
   development stack (M6-C02) is untouched: `curl -s
   http://127.0.0.1:8000/v1/health` still answers.
4. Restore into the fresh volume:

   ```
   B=$(ls -t reports/m6-backups/breakroom-*.dump | head -1)
   drill scripts/restore.sh "$B"
   ```

   Expect, in order: `starting postgres`, `stopping server and caddy`,
   `restoring <path>`, `running migrations`, `running one purge pass`, and a
   closing line starting `Restored.`. Exit 0. Running the same command again
   (`drill scripts/restore.sh "$B"`) without `--force` must refuse: `database 'breakroom' is not empty (...
   tables); nothing was changed.` and exit 1.
5. `dcd up -d` and `curl -s http://127.0.0.1:18000/v1/health` print
   `{"status":"ok"}`.
6. Start the companion again with the same command as in step 1 (same
   `$D_ROOT`). Expect `Connected as Drill` **without** any sign-in (the
   restored refresh token still works). Then click **Sign out** and **Sign
   in** again with `drill@example.com` and a fresh code from Mailpit (no
   invite code): expect `Connected as Drill`.

Optional, only if `age` is installed (the RUNBOOK drill never ran this path):
`age-keygen -o reports/m6-age.key`, then `drill scripts/backup.sh --out-dir
reports/m6-backups --age-recipient <public key printed by age-keygen>`, and
`drill scripts/restore.sh "$(ls -t reports/m6-backups/*.dump.age | head -1)"
--age-identity reports/m6-age.key --force`, then `dcd up -d` and the health check. Record
whether it worked or that `age` was not available.

Clean up: `dcd down -v`; then remove the companion's login with D's own
environment, because a keyring entry (if one was used) is keyed by the server
address and the state directory: `BREAKROOM_SERVER_URL=http://127.0.0.1:18000
XDG_RUNTIME_DIR="$D_ROOT/run" XDG_STATE_HOME="$D_ROOT/state"
.venv/bin/breakroom-companion uninstall --keep-account`; then `rm -rf
reports/m6-backups reports/compose.m6-drill*.yaml reports/m6-age.key
"$D_ROOT"` and `unset -f dcd drill`. Any failure of the scripts goes back to M6-08; a failure that
only comes from merging the compose files is a finding for the RUNBOOK (it
shows that `restore.sh` and `backup.sh` cannot take an override file).

Result (pass / fail / blocked):

## Disclosure review (M6-09)

**M6-C14 — The notice, DATA_HANDLING and the preview match the code.** Read
`docs/PRIVACY_NOTICE.md`, `docs/DATA_HANDLING.md` and the preview windows side
by side. Every item that any of the three names must match what the code does.
Checks (use the stack of M6-C11 with A signed in; B is not needed, and Bea's
account is gone after M6-C12):

1. **Preview against the notice.** In A open `Settings`, privacy page, **Show
   what is shared**; and (in a sign-in dialog) **Show what is sent**. The
   preview must show an `Example` presence JSON that holds only the fields you
   ticked, `Sent when you sign in` with `Email`, `Nickname` and `Device name:
   ...`, and `Never shared` with these 11 items: Your prompts; Source code;
   Command arguments; Tool results; Plans; Stack traces; Secrets and
   environment values; File and folder paths; Workspace and repository names;
   Detailed file names; Claude session identifiers. The notice says the
   device name is `WSL device` by default and is editable before sign-in, and
   the hostname is never sent: matches M6-C11.
2. **Tables.** The 14 tables of the "On the server" table equal the table
   query (`TB`, second query in "Postgres queries"; 15 rows with
   `alembic_version`).
3. **Retention values.** DATA_HANDLING gives these defaults; check the code:

   ```
   grep -nE '^    (code_ttl_s|access_ttl_s|refresh_ttl_s|invite_ttl_s|queue_ttl_s|retention_s|message_retention_s|report_retention_s|challenge_retention_s|refresh_token_retention_s|invite_retention_s|tombstone_retention_s):' apps/server/src/breakroom_server/config.py
   ```

   Expect the defaults `86_400` (challenges, messages), `604_800` (refresh
   tokens after expiry, invite validity), `2_592_000` (invites after end,
   queue entries and matches, and `refresh_ttl_s`, the 30-day refresh token),
   `7_776_000` (resolved reports), `31_536_000` (tombstones of deleted
   disabled accounts), `600` (code validity), `900`
   (`access_ttl_s`, the 15 min access token), `90` (search). That is 24 h, 7 d,
   30 d, 90 d, 1 year, 10 min, 15 min and 90 s as the documents say.
4. **Local files.** With A running as in M6-C11:

   ```
   stat -c '%a %n' ~/.local/state/breakroom ~/.local/state/breakroom/* "${XDG_RUNTIME_DIR:-/tmp/breakroom-$(id -u)}/breakroom" "${XDG_RUNTIME_DIR:-/tmp/breakroom-$(id -u)}/breakroom"/*
   ```

   Expect the directories `700`, the files and `companion.sock` `600`, and the
   names `companion-state.json` and (file store only) `credentials.json` in
   the state directory, `companion.sock` in the socket directory, and `salt`
   there too once a hook has run (the hook creates it, so a Claude Code
   session must have been active).
   Nothing else appears (`install.json` only after a `breakroom-companion
   install`).
5. **Chat footer.** The sentence in DATA_HANDLING ("Not end-to-end encrypted.
   Messages are deleted 24 h after the chat ends.") is the one in the chat
   window: `grep -rn "Not end-to-end encrypted" apps/companion/src docs/DATA_HANDLING.md`
   prints both, with the same words.
6. **Logs.** DATA_HANDLING says server logs hold fixed strings, counts and
   opaque ids, never an email, code, token, nickname, message body, report
   note, device name or IP address. That covers every log line, not only the
   access lines:

   ```
   docker compose logs server 2>&1 | grep -cE '([0-9]{1,3}\.){3}[0-9]{1,3}|filip-m6@|bea-m6@|bra_|brr_|FilipM6|BeaM6|test laptop|export-test|\b[0-9A-Z]{4}-[0-9A-Z]{4}\b'
   ```

   Expect `0`. Read the matching lines before judging, as in M5-C18: a bind
   address such as `0.0.0.0` in the server start line is not a hit; a client
   address, a person's data or a code is.
7. **Export wording.** The export and delete behaviour the documents describe
   is what M6-C12 observed (own rows only, no hashes, peer's history kept).

Any mismatch is a `fail`; name the sentence and the code that disagree. Ignore
the placeholders here: M6-C15 covers them.

Result (pass / fail / blocked):

**M6-C15 — Placeholders still open (expected: blocked).** Placeholders stay
unfilled for now (decided 2026-10-17). This step lists them and records them
as `blocked`, not `fail`.

```
grep -rnE '<contact address>|<operator name>|<owner>/<repo>|abuse report owner' README.md SECURITY.md CONTRIBUTING.md docs/RUNBOOK.md docs/DATA_HANDLING.md docs/PRIVACY_NOTICE.md
```

Expect hits for `<contact address>` (SECURITY.md, DATA_HANDLING.md,
PRIVACY_NOTICE.md, RUNBOOK.md), `<operator name>` (PRIVACY_NOTICE.md),
`<owner>/<repo>` (README.md, the git+https install line and marketplace
line) and the abuse-report owner line (RUNBOOK.md). The step is `blocked`
while any remains; write the count per placeholder in the note. If a hit
appears in a file not listed, or a new kind of placeholder, record it. When
Filip fills them, the step becomes `pass` if the grep prints nothing and the
filled text is the same everywhere. A placeholder that is not documented as
open is a `fail`. Two decisions the documents still leave open are not found
by the grep; list them in the note too: the backup lifetime and the off-host
copy (RUNBOOK "Backup schedule" and "Retention reminder"; the DATA_HANDLING
backups row says "Not decided yet"), and the abuse-report owner and response
time (RUNBOOK "Abuse reports").

Result (pass / fail / blocked):

## Usability (run after the design pass)

**M6-C16 — Usability flows (ARCHITECTURE §14). Run after the design pass
(README "Next" step 6), not with the rest of this list.** Mark it `blocked`
(pending) in the report until then. One person who has never seen
Breakroom goes through, using only the documents (README, the in-app texts):
install, onboarding, a first invite, a match, a chat, an extension, report
and block, and uninstall. Use the fresh distro of M6-C07 (re-import it, the
last run removed it) and the development stack, with a second tester or Filip
playing the other side. The observer says nothing and does not touch the
keyboard. Record each point where the person stalled, hesitated, read
something twice, guessed or asked: the screen or document, what they
expected, what they did, how long they were stuck. Each point is a finding.
M6 has no separate usability spec: if the run finds more than copy fixes, a
spec follows.

Result (pass / fail / blocked):

## Moved to the rehearsal deploy

These are **not** steps in this checklist (decided 2026-10-17: no LAN
two-machine run; see README "Next" step 8). They are run on the rehearsal host
with the real domain, then the host is destroyed, and again on the real
deployment:

- TLS: a public certificate for `BREAKROOM_PUBLIC_HOST` from Caddy; a client on
  another network signs in, matches and chats through it; the WebSocket
  (`/v1/realtime`) works through Caddy (M6-03).
- Proxy limits: a spoofed `X-Forwarded-For` sent to the public address does not
  change the limiter key, while the real peer address does (M6-03,
  `BREAKROOM_TRUSTED_PROXIES`).
- Mail: a real provider sends the sign-in code (SPF and DKIM set before any
  real send); `BREAKROOM_SMTP_TLS` starttls or implicit with credentials.
- The restore drill repeated on the host (RUNBOOK: the local drill ran without
  Caddy or real SMTP), including the `age` path and an off-host copy of the
  backup.
- The two-machine steps of the M4 and M5 checklists, which ran on one machine
  against a local proxy. Neither checklist marks single steps as pending; their
  reports list the platform "two separate machines (M6)". The steps that stand
  in for a second machine and should be reused, by ID: **M4-C03** (two
  companions signed in), **M4-C04** (gate: match exactly once), **M4-C09**
  (server restart while both search), **M4-C10** (block), **M5-C03** (the
  `socat` proxy at port 8001 stands in for a separate network), **M5-C04**
  (chat both ways), **M5-C07** (B offline about 20 s: replace the forwarder
  kill with a real network cut on the second machine), **M5-C08** (B offline
  over 60 s), **M5-C09** and **M5-C10** (extension and expiry) and **M5-C11**
  to **M5-C15** (End, Block, Report, late report, resolve). M5-C07 and M5-C08
  are the ones the single-machine setup only imitates.

## Report

**M6-C17 — Report.**

```
mkdir -p reports
uv run pytest --junitxml=reports/m6-junit.xml
```

(Skip the run if M6-C01 just did it; keep that output instead.) Fill in
`reports/m6-report.json` from the template below, using `pass`, `fail` or
`blocked` per step. Note platforms still pending (native Windows, macOS,
Linux without WSL, and the two-machine and TLS items under "Moved to the
rehearsal deploy"). `reports/` is never committed. Clean up what this run
left; nothing is deleted automatically, each item is yours to confirm:

- `reports/m6-*.json` exports and `reports/m6-junit.xml` once the report is
  written; `dist/` if the bundle is no longer needed.
- `/mnt/c/Temp/breakroom-release.tar.gz` and any `C:\wsl\` files you created;
  the test distro (`wsl --unregister breakroom-clean` in PS, run by Filip;
  `wsl --list` must no longer show it).
- Companion B's directories: `XDG_RUNTIME_DIR="$B_ROOT/run"
  XDG_STATE_HOME="$B_ROOT/state" .venv/bin/breakroom-companion uninstall
  --keep-account`, then `rm -rf "$B_ROOT"`.
- The `breakroom-server:prod` image of the drill: `docker image rm
  breakroom-server:prod`.
- Opt-in, only if you want an empty development database: `docker compose
  down -v` deletes it and every account in it. Skip it otherwise.

```json
{
  "date": "YYYY-MM-DD",
  "claude_code_version": "output of `claude --version`",
  "platform": "WSL2 Linux, kernel version, WSLg yes/no, Docker version, fresh distro name and Ubuntu release",
  "steps": {
    "M6-C01": {"result": "", "note": ""},
    "M6-C02": {"result": "", "note": ""},
    "M6-C03": {"result": "", "note": ""},
    "M6-C04": {"result": "", "note": ""},
    "M6-C05": {"result": "", "note": ""},
    "M6-C06": {"result": "", "note": ""},
    "M6-C07": {"result": "", "note": ""},
    "M6-C08": {"result": "", "note": ""},
    "M6-C09": {"result": "", "note": ""},
    "M6-C10": {"result": "", "note": ""},
    "M6-C11": {"result": "", "note": ""},
    "M6-C12": {"result": "", "note": ""},
    "M6-C13": {"result": "", "note": ""},
    "M6-C14": {"result": "", "note": ""},
    "M6-C15": {"result": "", "note": "expected blocked: list the open placeholders"},
    "M6-C16": {"result": "", "note": "expected blocked until the design pass has run"},
    "M6-C17": {"result": "", "note": ""}
  },
  "observations": {
    "migration_version": null,
    "ci_failing_test_checks_result": null,
    "ci_revert_checks_result": null,
    "ci_ungrouped_test_failed_job": null,
    "branch_protection_status_code": null,
    "release_tarball_sha256_matches": null,
    "wsl_version": null,
    "extra_install_steps_beyond_readme": [],
    "autostart_started_companion": null,
    "hook_last_event_at_set": null,
    "mcp_companion_state": null,
    "uninstall_leftovers": [],
    "claude_code_leftovers": [],
    "device_revoked_after_uninstall": null,
    "device_name_edited": null,
    "hostname_hits": {"database": null, "server_log": null},
    "export_has_hashes": null,
    "delete_refused_while_live_chat": null,
    "peer_history_kept_after_delete": null,
    "restore_drill": {"refused_without_force": null, "refresh_after_restore": null, "age_path_tested": null},
    "placeholders_open": {"contact address": null, "operator name": null, "owner/repo": null, "abuse report owner": null},
    "usability_stall_points": []
  },
  "deviations": [],
  "pending_platforms": ["native Windows", "macOS", "Linux without WSL", "public TLS and a second machine (rehearsal deploy)"]
}
```

M6 gate (§14): reproducible installations and vetted disclosure and retention
before the closed beta. Passing means M6-C01, M6-C02, M6-C03, M6-C04, M6-C08,
M6-C09, M6-C10, M6-C11, M6-C12, M6-C13 and M6-C14 pass; M6-C15 is `blocked`
only on the named placeholders; M6-C05 records the convention; M6-C16 is run
after the design pass. The steps that count most are the clean install and
uninstall (M6-C08 to M6-C10), the data request (M6-C12), the restore drill
(M6-C13) and the disclosure review (M6-C14).
