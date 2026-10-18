#!/usr/bin/env bash
# Restore a backup made by scripts/backup.sh into the `postgres` compose service.
#
#   scripts/restore.sh BACKUP_FILE [--force] [--age-identity FILE]
#
#   --force               restore over a database that already has tables
#                         (its public schema is dropped first)
#   --age-identity FILE   private key for a *.age backup (a *.gpg backup uses
#                         your gpg keyring)
#
# Steps: start postgres, refuse if the database is not empty (unless --force),
# stop the server, restore, run `beebreak-server migrate`, then run ONE purge
# pass. Nothing is started for you at the end; run `docker compose up -d`.
#
# Retention: a backup brings back rows that were purged after it was taken
# (ended matches, message bodies, resolved reports, old sign-in rows). The purge
# pass applies the current retention settings to the restored data. Anyone who
# asked for deletion in the meantime still needs `beebreak-admin account
# delete` again if their account came back (see docs/RUNBOOK.md).
#
# Environment: COMPOSE_PROJECT_NAME is REQUIRED (non-empty; the script refuses to
# run without it, so --force cannot reach the dev stack's database). Optional:
# BEEBREAK_COMPOSE_FILE, BEEBREAK_DB_USER, BEEBREAK_DB_NAME, as for backup.sh.

set -Eeuo pipefail
umask 077

compose_file="${BEEBREAK_COMPOSE_FILE:-compose.prod.yaml}"
db_user="${BEEBREAK_DB_USER:-beebreak}"
db_name="${BEEBREAK_DB_NAME:-beebreak}"
backup=""
force=0
age_identity=""

usage() {
    sed -n '2,/^$/p' "$0" | sed 's/^# \{0,1\}//' >&2
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --force) force=1; shift ;;
        --age-identity) age_identity="${2:?--age-identity needs a file}"; shift 2 ;;
        -h | --help) usage; exit 0 ;;
        -*) echo "unknown argument: $1" >&2; usage; exit 2 ;;
        *)
            if [[ -n "$backup" ]]; then
                echo "only one backup file may be given" >&2
                exit 2
            fi
            backup="$1"
            shift
            ;;
    esac
done

# The dev stack shares the directory-derived project name; naming the project
# on purpose keeps a backup or --force restore away from the wrong database.
if [[ -z "${COMPOSE_PROJECT_NAME:-}" ]]; then
    echo "COMPOSE_PROJECT_NAME is not set; name the compose project first." >&2
    exit 2
fi

if [[ -z "$backup" ]]; then
    usage
    exit 2
fi
if [[ ! -f "$backup" ]]; then
    echo "no such backup file: $backup" >&2
    exit 2
fi

decrypt=(cat)
case "$backup" in
    *.age)
        [[ -n "$age_identity" ]] || { echo "an .age backup needs --age-identity" >&2; exit 2; }
        command -v age >/dev/null || { echo "age is not installed" >&2; exit 1; }
        decrypt=(age --decrypt -i "$age_identity")
        ;;
    *.gpg)
        command -v gpg >/dev/null || { echo "gpg is not installed" >&2; exit 1; }
        decrypt=(gpg --batch --quiet --decrypt)
        ;;
esac

# Resolve the backup path before moving to the repo root.
backup="$(cd "$(dirname "$backup")" && pwd)/$(basename "$backup")"
repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$repo_root"
compose=(docker compose -f "$compose_file")
psql=("${compose[@]}" exec -T postgres psql --username "$db_user" --dbname "$db_name" --no-psqlrc --quiet --tuples-only --no-align)

echo "starting postgres"
"${compose[@]}" up -d --wait postgres

tables="$("${psql[@]}" --command "select count(*) from information_schema.tables where table_schema = 'public'")"
if [[ "$tables" != "0" ]]; then
    if [[ "$force" != "1" ]]; then
        echo "database '$db_name' is not empty ($tables tables); nothing was changed." >&2
        echo "Restore into a fresh volume, or pass --force to replace its contents." >&2
        exit 1
    fi
    echo "--force: dropping the existing contents of '$db_name'"
fi

echo "stopping server and caddy"
"${compose[@]}" stop caddy server

if [[ "$tables" != "0" ]]; then
    "${psql[@]}" --command "set client_min_messages = warning; drop schema public cascade; create schema public"
fi

restore_failed() {
    {
        echo
        echo "RESTORE FAILED (the error is above)."
        echo "State: the restore runs in one transaction, so '$db_name' holds nothing"
        echo "from this backup: it is empty if it was fresh or --force dropped it, and"
        echo "unchanged otherwise. server and caddy are stopped."
        echo "Fix the cause, then run the same command again (with --force if the"
        echo "database was emptied by this run)."
    } >&2
}
trap restore_failed ERR

echo "restoring $backup"
"${decrypt[@]}" <"$backup" \
    | "${compose[@]}" exec -T postgres \
        pg_restore --username "$db_user" --dbname "$db_name" \
        --no-owner --exit-on-error --single-transaction

trap - ERR

echo "running migrations"
"${compose[@]}" run --rm --no-deps migrate

echo "running one purge pass"
# --pull never: use the image already built; do not rebuild it here.
"${compose[@]}" run --rm --no-deps --pull never server beebreak-server purge

echo
echo "Restored. One purge pass has run: rows past their retention period that the"
echo "backup brought back are gone again (the counts, if any, are printed above)."
echo "Start the stack with: docker compose -f $compose_file up -d"
