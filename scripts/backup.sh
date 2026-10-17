#!/usr/bin/env bash
# Back up the Breakroom database: `pg_dump --format=custom` from the `postgres`
# compose service into a dated file (mode 0600). Prints size and SHA-256.
#
#   scripts/backup.sh [--out-dir DIR] [--age-recipient KEY | --gpg-recipient ID]
#
#   --out-dir DIR         where the dump goes (default: ./backups, made 0700)
#   --age-recipient KEY   encrypt with `age -r KEY`  -> *.dump.age
#   --gpg-recipient ID    encrypt with `gpg -r ID`   -> *.dump.gpg
#
# The dump is piped straight into the encryptor, so a plaintext copy never
# touches the disk when you ask for encryption. A backup holds user data
# (emails, nicknames, messages not yet purged): keep it encrypted and off the
# server it protects.
#
# Environment (COMPOSE_PROJECT_NAME is required, the rest optional):
#   BREAKROOM_COMPOSE_FILE  compose file (default: compose.prod.yaml)
#   BREAKROOM_DB_USER       database user (default: breakroom)
#   BREAKROOM_DB_NAME       database name (default: breakroom)
#   COMPOSE_PROJECT_NAME    REQUIRED: the compose project (non-empty), as docker
#                           compose reads it; the script refuses to run without

set -euo pipefail
umask 077

compose_file="${BREAKROOM_COMPOSE_FILE:-compose.prod.yaml}"
db_user="${BREAKROOM_DB_USER:-breakroom}"
db_name="${BREAKROOM_DB_NAME:-breakroom}"
out_dir="backups"
age_recipient=""
gpg_recipient=""

usage() {
    sed -n '2,/^$/p' "$0" | sed 's/^# \{0,1\}//' >&2
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --out-dir) out_dir="${2:?--out-dir needs a directory}"; shift 2 ;;
        --age-recipient) age_recipient="${2:?--age-recipient needs a key}"; shift 2 ;;
        --gpg-recipient) gpg_recipient="${2:?--gpg-recipient needs an id}"; shift 2 ;;
        -h | --help) usage; exit 0 ;;
        *) echo "unknown argument: $1" >&2; usage; exit 2 ;;
    esac
done

# The dev stack shares the directory-derived project name; naming the project
# on purpose keeps a backup or --force restore away from the wrong database.
if [[ -z "${COMPOSE_PROJECT_NAME:-}" ]]; then
    echo "COMPOSE_PROJECT_NAME is not set; name the compose project first." >&2
    exit 2
fi

if [[ -n "$age_recipient" && -n "$gpg_recipient" ]]; then
    echo "choose one of --age-recipient and --gpg-recipient" >&2
    exit 2
fi

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$repo_root"
compose=(docker compose -f "$compose_file")

suffix=""
encrypt=(cat)
if [[ -n "$age_recipient" ]]; then
    command -v age >/dev/null || { echo "age is not installed" >&2; exit 1; }
    suffix=".age"
    encrypt=(age -r "$age_recipient")
elif [[ -n "$gpg_recipient" ]]; then
    command -v gpg >/dev/null || { echo "gpg is not installed" >&2; exit 1; }
    suffix=".gpg"
    encrypt=(gpg --batch --yes --encrypt --recipient "$gpg_recipient" --output -)
fi

mkdir -p "$out_dir"
chmod 700 "$out_dir"
stamp="$(date -u +%Y%m%dT%H%M%SZ)"
final="$out_dir/breakroom-$stamp.dump$suffix"
partial="$final.partial"
trap 'rm -f "$partial"' EXIT

if [[ -e "$final" ]]; then
    echo "refusing to overwrite $final" >&2
    exit 1
fi

"${compose[@]}" exec -T postgres \
    pg_dump --format=custom --username "$db_user" --dbname "$db_name" \
    | "${encrypt[@]}" >"$partial"

if [[ ! -s "$partial" ]]; then
    echo "backup is empty; nothing written" >&2
    exit 1
fi

chmod 600 "$partial"
mv "$partial" "$final"

# An unencrypted dump can be checked by reading its table of contents.
if [[ -z "$suffix" ]]; then
    "${compose[@]}" exec -T postgres pg_restore --list <"$final" >/dev/null
fi

size="$(stat -c %s "$final")"
sum="$(sha256sum "$final" | cut -d' ' -f1)"
echo "file:   $final"
echo "size:   $size bytes"
echo "sha256: $sum"
