#!/usr/bin/env bash
# Decrypt and check a panel archive (System -> Panel archive) into a directory:
#   LAMNEZIA_ARCHIVE_PASSWORD=... unpack.sh ARCHIVE TARGET_DIR [CODE_DIR]
# install.sh --restore runs it, before the panel exists: only bash, coreutils and openssl.
# The archive is a header line and `openssl enc` output around a tar.gz: manifest.json, env,
# migrations.txt, database.dump, media/, SHA256SUMS. Format 2 adds a password check after the
# header and an HMAC-SHA256 at the end (apps/backups/services/archive_cipher.py), so a wrong
# password and a damaged file give different errors; format 1 has neither.
# With CODE_DIR, an archive made by a newer panel than the code there is refused.
set -euo pipefail

HEADER_V1="LAMNEZIA-PANEL-ARCHIVE 1"
HEADER_V2="LAMNEZIA-PANEL-ARCHIVE 2"
HEADER_SIZE=$((${#HEADER_V2} + 1))
DIGEST_SIZE=32
OPENSSL_ARGS=(enc -aes-256-cbc -pbkdf2 -iter 600000 -md sha256)

die() { printf 'Error: %s\n' "$*" >&2; exit 1; }

# Bytes of a hex string, through builtins only: keys never reach a command line.
hex_bytes() {
    local hex=$1 out="" i
    for ((i = 0; i < ${#hex}; i += 2)); do out+="\\x${hex:i:2}"; done
    printf '%b' "$out"
}

# The 64-byte HMAC block of a key shorter than it, XOR-ed with the byte $2.
key_pad() {
    local key=$1 pad=$2 out="" byte i
    for ((i = 0; i < 64; i++)); do
        byte=0
        ((i < ${#key} / 2)) && byte=$((16#${key:i * 2:2}))
        printf -v byte '\\x%02x' $((byte ^ pad))
        out+=$byte
    done
    printf '%b' "$out"
}

sha256_hex() { local digest _; read -r digest _ < <(sha256sum); printf '%s' "$digest"; }

# HMAC-SHA256 with the hex key $1 of what comes on stdin, in hex.
hmac_sha256() {
    local inner
    inner=$({ key_pad "$1" $((0x36)); cat; } | sha256_hex)
    { key_pad "$1" $((0x5c)); hex_bytes "$inner"; } | sha256_hex
}

file_hex() { od -An -v -tx1 -j "$2" -N "$3" "$1" | tr -d ' \n'; }

# The password goes through a file descriptor: neither the command line nor the environment
# of openssl shows it, and a non-ASCII password reaches it byte for byte.
decrypt() {
    openssl "${OPENSSL_ARGS[@]}" -d -pass fd:3 3<<<"$password" 2>/dev/null |
        tar -xz --no-same-owner -C "$target" 2>/dev/null
}

unpack_v1() {
    tail -c +$((HEADER_SIZE + 1)) "$archive" | decrypt ||
        die "the archive cannot be read with this password, or the file is damaged"
}

unpack_v2() {
    local size body_at body_size salt material key iv mac_key
    size=$(wc -c <"$archive")
    body_at=$((HEADER_SIZE + DIGEST_SIZE))
    body_size=$((size - body_at - DIGEST_SIZE))
    ((body_size > 16)) && [ "$(file_hex "$archive" "$body_at" 8)" = "53616c7465645f5f" ] ||
        die "the archive is damaged: it is cut short"
    salt=$(file_hex "$archive" $((body_at + 8)) 8)
    material=$(openssl "${OPENSSL_ARGS[@]}" -P -S "$salt" -pass fd:3 3<<<"$password") ||
        die "openssl cannot derive the archive key"
    [[ $material =~ key\ *=\ *([0-9A-Fa-f]{64}) ]] && key=${BASH_REMATCH[1]} &&
        [[ $material =~ iv\ *=\ *([0-9A-Fa-f]{32}) ]] && iv=${BASH_REMATCH[1]} ||
        die "openssl cannot derive the archive key"
    [ "$({ hex_bytes "$key$iv"; printf check; } | sha256_hex)" = \
        "$(file_hex "$archive" "$HEADER_SIZE" "$DIGEST_SIZE")" ] ||
        die "wrong archive password"
    mac_key=$({ hex_bytes "$key$iv"; printf mac; } | sha256_hex)
    [ "$(head -c $((size - DIGEST_SIZE)) "$archive" | hmac_sha256 "$mac_key")" = \
        "$(file_hex "$archive" $((size - DIGEST_SIZE)) "$DIGEST_SIZE")" ] ||
        die "the archive is damaged: the file was changed or not copied whole"
    tail -c +$((body_at + 1)) "$archive" | head -c "$body_size" | decrypt ||
        die "the archive is damaged: it does not unpack"
}

[ $# -ge 2 ] || die "usage: unpack.sh ARCHIVE TARGET_DIR [CODE_DIR]"
archive="$1"
target="$2"
code="${3:-}"
password="${LAMNEZIA_ARCHIVE_PASSWORD:-}"

[ -f "$archive" ] || die "no archive file $archive"
[ -n "$password" ] || die "the archive password is empty"
mkdir -p "$target"

case "$(head -c ${#HEADER_V2} "$archive")" in
"$HEADER_V2") unpack_v2 ;;
"$HEADER_V1") unpack_v1 ;;
*) die "$archive is not a panel archive" ;;
esac

(cd "$target" && sha256sum --quiet -c SHA256SUMS) || die "the archive is damaged: checksums differ"
grep -q '"format": 1' "$target/manifest.json" || die "unknown archive format; update the panel code"

if [ -n "$code" ]; then
    while IFS= read -r migration; do
        [ -f "$code/$migration" ] ||
            die "the archive comes from a newer panel ($migration); install that version or newer"
    done <"$target/migrations.txt"
fi
