#!/usr/bin/env bash
# Lamnezia installer for Debian/Ubuntu. Run as root:
#   curl -fsSL https://raw.githubusercontent.com/AGLamake/Lamnezia/releases/install.sh | bash -s -- --domain panel.example.com
# Without a domain the panel opens at https://<public IPv4 of the server>/ (bundled Caddy only).
# Running it again updates an existing installation and keeps its .env.
# A panel archive (System -> Panel archive) is restored on a new server with --restore FILE.
# Without a terminal (cloud-init) it asks nothing: the options come from LAMNEZIA_* variables,
# see deploy/cloud-init/user-data.yaml.
set -euo pipefail

REPO="${LAMNEZIA_REPO:-https://github.com/AGLamake/Lamnezia.git}"
# The releases branch holds only releases, so its tip is the latest one.
REF="${LAMNEZIA_REF:-releases}"
DIR="${LAMNEZIA_DIR:-/opt/lamnezia}"
DOMAIN="${LAMNEZIA_DOMAIN:-}"
EMAIL="${LAMNEZIA_EMAIL:-}"
PROXY="${LAMNEZIA_PROXY:-auto}"
IMAGE="${LAMNEZIA_IMAGE:-}"
ADMIN="${LAMNEZIA_ADMIN_USERNAME:-admin}"
# docker compose prefers the shell environment to .env: these must not reach it.
unset LAMNEZIA_DOMAIN LAMNEZIA_EMAIL LAMNEZIA_PROXY LAMNEZIA_IMAGE
IMAGE_REPO="ghcr.io/aglamake/lamnezia"
RESTORE=""
RESTORE_DIR=""
MOVING=0
UNATTENDED=0
NEW_ENV=0
ADMIN_PASSWORD=""
SUMMARY_FILE="install-summary.txt"

usage() {
    cat <<EOF
Usage: install.sh [--domain NAME] [--email ADDRESS] [--proxy auto|nginx|caddy] [--image TAG] [--dir PATH]
                  [--restore FILE [--moving]]

  --domain  panel domain; its DNS A record must point to this server. Without it the panel
            opens by the server's public IPv4 address with a short-lived Let's Encrypt
            certificate, which needs the bundled Caddy (--proxy caddy, ports 80/443 free)
  --email   address for Let's Encrypt expiry notices (optional)
  --proxy   nginx: use the nginx already on this server, HTTPS via certbot
            caddy: bundled Caddy container on ports 80/443
            auto (default): nginx if it is installed, otherwise caddy
  --image   take the ready image from GHCR instead of building it: a version (1.2.0, 1.2),
            latest or a full image reference; kept in .env for later runs
  --dir     installation directory (default /opt/lamnezia)
  --restore install from a panel archive: its database, secrets and files; the password is
            asked or taken from LAMNEZIA_ARCHIVE_PASSWORD; --domain overrides the archived one,
            and an archived IP address is replaced with the address of this server
  --moving  set by the panel move: the domain still points to the old machine, so a failed
            certificate request is retried every 10 minutes instead of stopping the install

Without a terminal, or with LAMNEZIA_UNATTENDED=1, nothing is asked and no domain means the
server's IP address. The options may then come from LAMNEZIA_DOMAIN, LAMNEZIA_EMAIL,
LAMNEZIA_PROXY, LAMNEZIA_IMAGE, LAMNEZIA_DIR and LAMNEZIA_REF; a new installation also gets
the administrator LAMNEZIA_ADMIN_USERNAME (admin) with a generated password. The result is written to install-summary.txt (mode 0600) in --dir.
EOF
}

log() { printf '\033[1;34m==>\033[0m %s\n' "$*"; }
die() { printf '\033[1;31mError:\033[0m %s\n' "$*" >&2; exit 1; }

parse_args() {
    while [ $# -gt 0 ]; do
        case "$1" in
            --domain) DOMAIN="${2:?}"; shift 2 ;;
            --email) EMAIL="${2:?}"; shift 2 ;;
            --proxy) PROXY="${2:?}"; shift 2 ;;
            --image) IMAGE="${2:?}"; shift 2 ;;
            --dir) DIR="${2:?}"; shift 2 ;;
            --restore) RESTORE="$(realpath "${2:?}")"; shift 2 ;;
            --moving) MOVING=1; shift ;;
            -h|--help) usage; exit 0 ;;
            *) usage; die "unknown option: $1" ;;
        esac
    done
    case "$PROXY" in auto|nginx|caddy) ;; *) die "--proxy must be auto, nginx or caddy" ;; esac
    [ -z "$RESTORE" ] || [ -f "$RESTORE" ] || die "no archive file $RESTORE"
    [ "$MOVING" = 0 ] || [ -n "$RESTORE" ] || die "--moving needs --restore"
}

# Prompts read from the terminal, so they work under "curl ... | bash" too.
ask() {
    local prompt="$1" answer
    [ "$UNATTENDED" = 0 ] || die "$prompt: pass it as an option or a LAMNEZIA_* variable"
    read -r -p "$prompt: " answer </dev/tty
    printf '%s' "$answer"
}

check_system() {
    [ "$(id -u)" -eq 0 ] || die "run as root (sudo bash install.sh ...)"
    command -v apt-get >/dev/null || die "only Debian and Ubuntu are supported"
}

# cloud-init runs scripts without a terminal and often without HOME; /dev/tty may exist there
# but cannot be opened.
detect_unattended() {
    if [ "${LAMNEZIA_UNATTENDED:-0}" = 1 ] || ! { : </dev/tty; } 2>/dev/null; then
        UNATTENDED=1
        export DEBIAN_FRONTEND=noninteractive HOME="${HOME:-/root}"
        log "No terminal: installing without questions"
    fi
}

install_packages() {
    local missing=()
    for cmd in git curl openssl; do
        command -v "$cmd" >/dev/null || missing+=("$cmd")
    done
    if [ ${#missing[@]} -gt 0 ]; then
        log "Installing ${missing[*]}"
        apt-get update -q
        apt-get install -y -q "${missing[@]}"
    fi
    if ! command -v docker >/dev/null || ! docker compose version >/dev/null 2>&1; then
        log "Installing Docker"
        curl -fsSL https://get.docker.com | sh
    fi
}

fetch_code() {
    if [ -d "$DIR/.git" ]; then
        log "Updating code in $DIR"
        checkout_ref
    elif [ -f "$DIR/compose.yaml" ]; then
        log "Using code already in $DIR"
    else
        log "Downloading Lamnezia to $DIR"
        git clone -q --branch "$REF" "$REPO" "$DIR"
    fi
}

# A branch is kept as a local branch tracking origin, so `git pull` works later;
# a tag stays a fixed version.
checkout_ref() {
    if git -C "$DIR" ls-remote --exit-code --heads origin "$REF" >/dev/null; then
        git -C "$DIR" fetch -q origin "+refs/heads/$REF:refs/remotes/origin/$REF"
        git -C "$DIR" checkout -q -B "$REF" "origin/$REF"
        git -C "$DIR" branch -q --set-upstream-to="origin/$REF" "$REF"
    else
        git -C "$DIR" fetch -q origin "+refs/tags/$REF:refs/tags/$REF"
        git -C "$DIR" checkout -q "$REF"
    fi
}

choose_proxy() {
    if [ "$PROXY" = "auto" ]; then
        if grep -q '^COMPOSE_PROFILES=caddy' "$DIR/.env" 2>/dev/null; then
            PROXY="caddy"
        elif command -v nginx >/dev/null; then
            PROXY="nginx"
        else
            PROXY="caddy"
        fi
    fi
    log "Reverse proxy: $PROXY"
}

# An IPv4 or IPv6 address rather than a domain name.
is_ip() {
    [[ "$1" =~ ^[0-9]+(\.[0-9]+){3}$ || "$1" == *:* ]]
}

# The public IPv4 address of this server as the internet sees it: Let's Encrypt checks it from
# outside, so an interface address behind NAT would not do.
public_ip() {
    local url ip
    for url in https://api.ipify.org https://ifconfig.me/ip; do
        ip="$(curl -4fsS --max-time 10 "$url" 2>/dev/null || true)"
        if [[ "$ip" =~ ^[0-9]+(\.[0-9]+){3}$ ]]; then
            printf '%s' "$ip"
            return
        fi
    done
    die "could not find the public IPv4 address of this server: pass --domain"
}

# No domain means the panel is opened by the server's IP address.
resolve_address() {
    if [ -z "$DOMAIN" ] && [ "$UNATTENDED" = 0 ]; then
        DOMAIN="$(ask "Panel domain (leave empty to open the panel by this server's IP address)")"
    fi
    if [ -z "$DOMAIN" ]; then
        DOMAIN="$(public_ip)"
        log "No domain: the panel opens by the IP address $DOMAIN"
    fi
    check_address
}

# certbot gives no certificate for an IP address; the bundled Caddy gets a short-lived one.
check_address() {
    is_ip "$DOMAIN" || return 0
    [ "$PROXY" = "caddy" ] || die "a panel without a domain needs the bundled Caddy on ports 80 and 443, \
but this server uses nginx: pass --domain, or --proxy caddy if nginx does not hold these ports"
}

# Caddy takes the certificate kind from .env (see Caddyfile). It follows LAMNEZIA_DOMAIN on every
# run, so moving the panel from an IP address to a domain is: edit .env, run install.sh again.
write_tls_mode() {
    local env_file="$DIR/.env"
    sed -i '/^LAMNEZIA_CADDY_TLS=/d' "$env_file"
    if is_ip "$DOMAIN"; then
        printf 'LAMNEZIA_CADDY_TLS=ip\n' >>"$env_file"
    fi
}

# First free localhost port from 8000 up: the host may already run other services.
free_port() {
    local port=8000
    while ss -Htln "sport = :$port" | grep -q .; do port=$((port + 1)); done
    printf '%s' "$port"
}

write_env() {
    local env_file="$DIR/.env"
    if [ -f "$env_file" ]; then
        log "Keeping existing $env_file"
        DOMAIN="$(sed -n 's/^LAMNEZIA_DOMAIN=//p' "$env_file")"
        check_address
        return
    fi
    resolve_address
    log "Creating $env_file with new secrets"
    sed -e "s|^LAMNEZIA_DOMAIN=.*|LAMNEZIA_DOMAIN=$DOMAIN|" \
        -e "s|^LAMNEZIA_SECRET_KEY=.*|LAMNEZIA_SECRET_KEY=$(openssl rand -hex 32)|" \
        -e "s|^LAMNEZIA_DB_PASSWORD=.*|LAMNEZIA_DB_PASSWORD=$(openssl rand -hex 24)|" \
        -e "s|^LAMNEZIA_ENCRYPTION_KEY=.*|LAMNEZIA_ENCRYPTION_KEY=$(openssl rand -base64 32 | tr '+/' '-_')|" \
        -e "s|^LAMNEZIA_HTTP_BIND=.*|LAMNEZIA_HTTP_BIND=127.0.0.1:$(free_port)|" \
        "$DIR/.env.example" >"$env_file"
    # compose reads COMPOSE_PROFILES from .env, so later "docker compose up" keeps Caddy.
    add_proxy_profile "$env_file"
    chmod 600 "$env_file"
    NEW_ENV=1
}

add_proxy_profile() {
    if [ "$PROXY" = "caddy" ]; then
        printf '\n# Start the bundled Caddy reverse proxy.\nCOMPOSE_PROFILES=caddy\n' >>"$1"
    fi
}

archive_password() {
    if [ -n "${LAMNEZIA_ARCHIVE_PASSWORD:-}" ]; then return; fi
    [ "$UNATTENDED" = 0 ] || die "set LAMNEZIA_ARCHIVE_PASSWORD, there is no terminal to ask the password"
    read -r -s -p "Archive password: " LAMNEZIA_ARCHIVE_PASSWORD </dev/tty
    printf '\n'
    export LAMNEZIA_ARCHIVE_PASSWORD
}

# Decrypt the archive and write .env from it: the secrets key must stay the one of the archive.
unpack_archive() {
    local env_file="$DIR/.env" bind
    [ ! -f "$env_file" ] || die "$env_file exists: --restore is for a new installation only"
    archive_password
    # Outside $DIR: the image build must not take the decrypted data into its context.
    RESTORE_DIR="$(mktemp -d -t lamnezia-restore.XXXXXX)"
    trap 'rm -rf "$RESTORE_DIR"' EXIT
    log "Decrypting and checking the archive"
    bash "$DIR/deploy/archive/unpack.sh" "$RESTORE" "$RESTORE_DIR" "$DIR"
    # A panel opened by an IP address moves to the address of this machine.
    if [ -z "$DOMAIN" ]; then
        DOMAIN="$(sed -n 's/^LAMNEZIA_DOMAIN=//p' "$RESTORE_DIR/env")"
        if is_ip "$DOMAIN"; then DOMAIN=""; fi
    fi
    resolve_address
    grep -v -e '^COMPOSE_PROFILES=' -e '^LAMNEZIA_DOMAIN=' "$RESTORE_DIR/env" >"$env_file"
    chmod 600 "$env_file"
    printf 'LAMNEZIA_DOMAIN=%s\n' "$DOMAIN" >>"$env_file"
    bind="$(sed -n 's/^LAMNEZIA_HTTP_BIND=//p' "$env_file")"
    if [ -z "$bind" ] || ss -Htln "sport = :${bind##*:}" | grep -q .; then
        sed -i '/^LAMNEZIA_HTTP_BIND=/d' "$env_file"
        printf 'LAMNEZIA_HTTP_BIND=127.0.0.1:%s\n' "$(free_port)" >>"$env_file"
    fi
    add_proxy_profile "$env_file"
}

# The database must be empty: pg_restore into a used one would mix two panels.
restore_data() {
    local tables
    log "Restoring the database and the uploaded files"
    (cd "$DIR" && docker compose up -d --wait postgres)
    tables="$(cd "$DIR" && docker compose exec -T postgres psql -U lamnezia -d lamnezia -tAc \
        "select count(*) from information_schema.tables where table_schema = 'public'")"
    [ "$tables" = "0" ] ||
        die "the database is not empty; to start over: cd $DIR && docker compose down -v && rm .env"
    (cd "$DIR" && docker compose exec -T postgres pg_restore -U lamnezia -d lamnezia \
        --no-owner --no-privileges --exit-on-error) <"$RESTORE_DIR/database.dump"
    mkdir -p "$RESTORE_DIR/media"
    (cd "$DIR" && docker compose run --rm --no-deps --user root -v "$RESTORE_DIR/media:/restore:ro" \
        web sh -c 'cp -a /restore/. /app/media/ && chown -R lamnezia:lamnezia /app/media')
}

# A bare tag means the published image; the choice is saved in .env, where compose reads it.
save_image() {
    local env_file="$DIR/.env"
    [ -n "$IMAGE" ] || return 0
    case "$IMAGE" in */*) ;; *) IMAGE="$IMAGE_REPO:$IMAGE" ;; esac
    sed -i '/^LAMNEZIA_IMAGE=/d' "$env_file"
    printf 'LAMNEZIA_IMAGE=%s\n' "$IMAGE" >>"$env_file"
}

prepare_images() {
    if grep -q '^LAMNEZIA_IMAGE=' "$DIR/.env"; then
        log "Pulling the ready image"
        (cd "$DIR" && docker compose pull -q)
    else
        log "Building the image (the first build takes a few minutes)"
        (cd "$DIR" && docker compose build -q)
    fi
}

start_stack() {
    log "Starting containers"
    (cd "$DIR" && docker compose up -d --no-build)
    log "Waiting for the panel to become healthy"
    for _ in $(seq 60); do
        if (cd "$DIR" && docker compose ps --format '{{.Service}} {{.Health}}') | grep -q '^web healthy$'; then
            return
        fi
        sleep 3
    done
    die "the panel did not start; see: cd $DIR && docker compose logs web"
}

setup_nginx() {
    local site="/etc/nginx/sites-available/lamnezia"
    local bind port
    bind="$(sed -n 's/^LAMNEZIA_HTTP_BIND=//p' "$DIR/.env")"
    port="${bind##*:}"
    if [ -f "$site" ]; then
        log "Keeping existing nginx site $site"
    else
        log "Adding nginx site for $DOMAIN"
        sed -e "s/panel.example.com/$DOMAIN/g" -e "s/127.0.0.1:8000/127.0.0.1:$port/" \
            "$DIR/deploy/nginx/lamnezia.conf" >"$site"
        ln -sf "$site" /etc/nginx/sites-enabled/lamnezia
        nginx -t || { rm -f /etc/nginx/sites-enabled/lamnezia; die "nginx rejected the site, it was disabled"; }
        systemctl reload nginx
    fi
    if [ ! -d "/etc/letsencrypt/live/$DOMAIN" ]; then
        command -v certbot >/dev/null || apt-get install -y -q certbot python3-certbot-nginx
        log "Requesting a Let's Encrypt certificate for $DOMAIN"
        local account=(--register-unsafely-without-email)
        [ -z "$EMAIL" ] || account=(--email "$EMAIL")
        certbot --nginx -d "$DOMAIN" --non-interactive --agree-tos --redirect "${account[@]}" && return
        [ "$MOVING" = 1 ] || die "certbot failed: check that $DOMAIN points to this server, then run install.sh again"
        retry_certificate "${account[@]}"
    fi
}

# During a panel move the domain points to the old machine until the admin switches DNS:
# a systemd timer asks for the certificate every 10 minutes and turns itself off on success.
retry_certificate() {
    local unit=/etc/systemd/system/lamnezia-certificate
    log "No certificate yet: retrying every 10 minutes until $DOMAIN points to this server"
    cat >"$unit.service" <<UNIT
[Unit]
Description=Lamnezia: Let's Encrypt certificate for $DOMAIN

[Service]
Type=oneshot
ExecStart=/bin/sh -c 'certbot --nginx -d $DOMAIN --non-interactive --agree-tos --redirect $* && systemctl disable --now lamnezia-certificate.timer'
UNIT
    cat >"$unit.timer" <<UNIT
[Unit]
Description=Lamnezia: retry the certificate for $DOMAIN

[Timer]
OnActiveSec=10min
OnBootSec=10min
OnUnitActiveSec=10min

[Install]
WantedBy=timers.target
UNIT
    systemctl daemon-reload
    systemctl enable --now lamnezia-certificate.timer
}

# The panel has no access to Docker: after a move it leaves stop-after-move in its archives
# volume, and this timer stops the stack once a minute after it finds the file.
install_stop_timer() {
    local unit=/etc/systemd/system/lamnezia-stop
    local request=/app/archives/stop-after-move
    cat >"$unit.service" <<UNIT
[Unit]
Description=Lamnezia: stop the panel when it asks to after a move

[Service]
Type=oneshot
ExecStart=/bin/sh -c 'cd $DIR && if docker compose exec -T web sh -c "test -e $request && rm -f $request" 2>/dev/null; then docker compose down; fi'
UNIT
    cat >"$unit.timer" <<UNIT
[Unit]
Description=Lamnezia: check once a minute whether the panel asks to stop

[Timer]
OnBootSec=1min
OnUnitActiveSec=1min

[Install]
WantedBy=timers.target
UNIT
    systemctl daemon-reload
    systemctl enable --now lamnezia-stop.timer
}

# The panel asks for a release update by a file in update/request (it writes there as uid 1000);
# the host writes the status and log to update/status, which the panel only reads. Made before
# the first start: docker would create missing folders owned by root.
prepare_update_dir() {
    mkdir -p "$DIR/update/request" "$DIR/update/status"
    chown root:root "$DIR/update" "$DIR/update/status"
    chmod 755 "$DIR/update" "$DIR/update/status"
    chown 1000:1000 "$DIR/update/request"
    chmod 700 "$DIR/update/request"
}

# The panel has no access to Docker or to the host: lamnezia-update.path starts the update script
# when the panel leaves a request. Only a git checkout can be updated this way.
install_update_unit() {
    local unit=/etc/systemd/system/lamnezia-update
    [ -d "$DIR/.git" ] || return 0
    install -m 755 "$DIR/deploy/update/lamnezia-deploy.sh" /usr/local/sbin/lamnezia-deploy
    cat >"$unit.service" <<UNIT
[Unit]
Description=Lamnezia: update the panel to the release it asks for

[Service]
Type=oneshot
Environment=LAMNEZIA_DIR=$DIR
ExecStart=/usr/local/sbin/lamnezia-deploy release
TimeoutStartSec=40min
UNIT
    cat >"$unit.path" <<UNIT
[Unit]
Description=Lamnezia: wait for an update request from the panel

[Path]
PathExists=$DIR/update/request/update

[Install]
WantedBy=paths.target
UNIT
    # The channel the panel shows; the update script refreshes it on every run.
    git -C "$DIR" symbolic-ref -q --short HEAD >"$DIR/update/status/agent" ||
        echo releases >"$DIR/update/status/agent"
    systemctl daemon-reload
    systemctl enable --now lamnezia-update.path
}

# Unattended, a new panel gets its first administrator at once: nobody is there to run
# createadmin. Prints the password; when the user cannot be created, prints nothing.
create_admin() {
    local password
    password="$(openssl rand -base64 18 | tr -d '/+=')"
    if printf '%s\n' "$password" | (cd "$DIR" && docker compose exec -T web \
        python manage.py createadmin --username "$ADMIN" --password-stdin) >/dev/null; then
        printf '%s' "$password"
    else
        log "Could not create the administrator $ADMIN: create one with createadmin" >&2
    fi
}

# The summary may hold a password, so only root reads it; the log shows only where it is.
# Written for a new installation only: a rerun must not replace the password not yet used.
write_summary() {
    local file="$DIR/$SUMMARY_FILE"
    [ "$NEW_ENV" = 1 ] || [ -n "$RESTORE" ] || return 0
    if [ "$UNATTENDED" = 1 ] && [ "$NEW_ENV" = 1 ]; then ADMIN_PASSWORD="$(create_admin)"; fi
    (
        umask 077
        {
            printf 'Lamnezia was installed %s\n\n' "$(date -u '+%Y-%m-%d %H:%M UTC')"
            printf 'Panel: https://%s/\n' "$DOMAIN"
            if [ -n "$ADMIN_PASSWORD" ]; then
                printf 'Administrator: %s\nPassword: %s\n' "$ADMIN" "$ADMIN_PASSWORD"
                printf 'Sign in, change the password, set up two-factor login, delete this file.\n'
            elif [ -n "$RESTORE" ]; then
                printf 'Restored from %s: sign in with the accounts of the archived panel.\n' "$RESTORE"
            else
                printf 'Create the first administrator:\n'
                printf '  cd %s && docker compose exec web python manage.py createadmin --username admin\n' "$DIR"
            fi
        } >"$file"
    )
    chmod 600 "$file"
    log "Summary saved to $file"
}

main() {
    parse_args "$@"
    detect_unattended
    check_system
    install_packages
    fetch_code
    choose_proxy
    if [ -n "$RESTORE" ]; then unpack_archive; else write_env; fi
    write_tls_mode
    save_image
    prepare_update_dir
    prepare_images
    if [ -n "$RESTORE" ]; then restore_data; fi
    start_stack
    install_stop_timer
    install_update_unit
    if [ "$PROXY" = "nginx" ]; then setup_nginx; fi
    if [ "$MOVING" = 1 ]; then
        if is_ip "$DOMAIN"; then
            log "The copy is running and waits: confirm the move in the old panel, then open https://$DOMAIN/"
        else
            log "The copy is running and waits: confirm the move in the old panel, then point $DOMAIN here"
        fi
        return
    fi
    log "Lamnezia is running at https://$DOMAIN/"
    write_summary
    if [ -n "$ADMIN_PASSWORD" ]; then
        log "The administrator $ADMIN and the password are in the summary"
    elif [ -n "$RESTORE" ]; then
        log "Restored from $RESTORE: sign in with the accounts of the archived panel"
    else
        log "Create the first administrator: cd $DIR && docker compose exec web python manage.py createadmin --username admin"
    fi
}

main "$@"
