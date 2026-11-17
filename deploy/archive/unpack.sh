#!/usr/bin/env bash
# Decrypt and check a panel archive (System -> Panel archive) into a directory:
#   LAMNEZIA_ARCHIVE_PASSWORD=... unpack.sh ARCHIVE TARGET_DIR [CODE_DIR]
# install.sh --restore runs it. The archive is a header line and then `openssl enc` output
# around a tar.gz: manifest.json, env, migrations.txt, database.dump, media/, SHA256SUMS.
# With CODE_DIR, an archive made by a newer panel than the code there is refused.
set -euo pipefail

HEADER="LAMNEZIA-PANEL-ARCHIVE 1"

die() { printf 'Error: %s\n' "$*" >&2; exit 1; }

[ $# -ge 2 ] || die "usage: unpack.sh ARCHIVE TARGET_DIR [CODE_DIR]"
archive="$1"
target="$2"
code="${3:-}"
password="${LAMNEZIA_ARCHIVE_PASSWORD:-}"

[ -f "$archive" ] || die "no archive file $archive"
[ -n "$password" ] || die "the archive password is empty"
[ "$(head -c ${#HEADER} "$archive")" = "$HEADER" ] || die "$archive is not a panel archive"
mkdir -p "$target"

# The password goes through a file descriptor: neither the command line nor the environment
# of openssl shows it, and a non-ASCII password reaches it byte for byte.
tail -c +$((${#HEADER} + 2)) "$archive" |
    openssl enc -d -aes-256-cbc -pbkdf2 -iter 600000 -md sha256 -pass fd:3 3<<<"$password" 2>/dev/null |
    tar -xz --no-same-owner -C "$target" 2>/dev/null ||
    die "the archive cannot be read with this password, or the file is damaged"

(cd "$target" && sha256sum --quiet -c SHA256SUMS) || die "the archive is damaged: checksums differ"
grep -q '"format": 1' "$target/manifest.json" || die "unknown archive format; update the panel code"

if [ -n "$code" ]; then
    while IFS= read -r migration; do
        [ -f "$code/$migration" ] ||
            die "the archive comes from a newer panel ($migration); install that version or newer"
    done <"$target/migrations.txt"
fi
