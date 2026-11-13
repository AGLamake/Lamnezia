#!/usr/bin/env bash
# Lamnezia installer for Debian/Ubuntu. Run as root:
#   curl -fsSL https://raw.githubusercontent.com/AGLamake/Lamnezia/releases/install.sh | bash -s -- --domain panel.example.com
# Running it again updates an existing installation and keeps its .env.
set -euo pipefail

REPO="${LAMNEZIA_REPO:-https://github.com/AGLamake/Lamnezia.git}"
# The releases branch holds only releases, so its tip is the latest one.
REF="${LAMNEZIA_REF:-releases}"
DIR="/opt/lamnezia"
DOMAIN=""
EMAIL=""
PROXY="auto"
IMAGE=""
IMAGE_REPO="ghcr.io/aglamake/lamnezia"

usage() {
    cat <<EOF
Usage: install.sh [--domain NAME] [--email ADDRESS] [--proxy auto|nginx|caddy] [--image TAG] [--dir PATH]

  --domain  panel domain; its DNS A record must point to this server
  --email   address for Let's Encrypt expiry notices (optional)
  --proxy   nginx: use the nginx already on this server, HTTPS via certbot
            caddy: bundled Caddy container on ports 80/443
            auto (default): nginx if it is installed, otherwise caddy
  --image   take the ready image from GHCR instead of building it: a version (1.2.0, 1.2),
            latest or a full image reference; kept in .env for later runs
  --dir     installation directory (default /opt/lamnezia)
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
            -h|--help) usage; exit 0 ;;
            *) usage; die "unknown option: $1" ;;
        esac
    done
    case "$PROXY" in auto|nginx|caddy) ;; *) die "--proxy must be auto, nginx or caddy" ;; esac
}

# Prompts read from the terminal, so they work under "curl ... | bash" too.
ask() {
    local prompt="$1" answer
    [ -r /dev/tty ] || die "$prompt: pass it as an option, there is no terminal to ask"
    read -r -p "$prompt: " answer </dev/tty
    printf '%s' "$answer"
}

check_system() {
    [ "$(id -u)" -eq 0 ] || die "run as root (sudo bash install.sh ...)"
    command -v apt-get >/dev/null || die "only Debian and Ubuntu are supported"
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
        git -C "$DIR" fetch -q origin "$REF"
        git -C "$DIR" checkout -q FETCH_HEAD
    elif [ -f "$DIR/compose.yaml" ]; then
        log "Using code already in $DIR"
    else
        log "Downloading Lamnezia to $DIR"
        git clone -q --branch "$REF" "$REPO" "$DIR"
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
        return
    fi
    [ -n "$DOMAIN" ] || DOMAIN="$(ask "Panel domain (e.g. panel.example.com)")"
    [ -n "$DOMAIN" ] || die "a domain is required"
    log "Creating $env_file with new secrets"
    sed -e "s|^LAMNEZIA_DOMAIN=.*|LAMNEZIA_DOMAIN=$DOMAIN|" \
        -e "s|^LAMNEZIA_SECRET_KEY=.*|LAMNEZIA_SECRET_KEY=$(openssl rand -hex 32)|" \
        -e "s|^LAMNEZIA_DB_PASSWORD=.*|LAMNEZIA_DB_PASSWORD=$(openssl rand -hex 24)|" \
        -e "s|^LAMNEZIA_ENCRYPTION_KEY=.*|LAMNEZIA_ENCRYPTION_KEY=$(openssl rand -base64 32 | tr '+/' '-_')|" \
        -e "s|^LAMNEZIA_HTTP_BIND=.*|LAMNEZIA_HTTP_BIND=127.0.0.1:$(free_port)|" \
        "$DIR/.env.example" >"$env_file"
    # compose reads COMPOSE_PROFILES from .env, so later "docker compose up" keeps Caddy.
    if [ "$PROXY" = "caddy" ]; then
        printf '\n# Start the bundled Caddy reverse proxy.\nCOMPOSE_PROFILES=caddy\n' >>"$env_file"
    fi
    chmod 600 "$env_file"
}

# A bare tag means the published image; the choice is saved in .env, where compose reads it.
save_image() {
    local env_file="$DIR/.env"
    [ -n "$IMAGE" ] || return 0
    case "$IMAGE" in */*) ;; *) IMAGE="$IMAGE_REPO:$IMAGE" ;; esac
    sed -i '/^LAMNEZIA_IMAGE=/d' "$env_file"
    printf 'LAMNEZIA_IMAGE=%s\n' "$IMAGE" >>"$env_file"
}

start_stack() {
    if grep -q '^LAMNEZIA_IMAGE=' "$DIR/.env"; then
        log "Pulling the ready image and starting containers"
        (cd "$DIR" && docker compose pull -q && docker compose up -d --no-build)
    else
        log "Building and starting containers (the first build takes a few minutes)"
        (cd "$DIR" && docker compose up -d --build)
    fi
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
        certbot --nginx -d "$DOMAIN" --non-interactive --agree-tos --redirect "${account[@]}" ||
            die "certbot failed: check that $DOMAIN points to this server, then run install.sh again"
    fi
}

main() {
    parse_args "$@"
    check_system
    install_packages
    fetch_code
    choose_proxy
    write_env
    save_image
    start_stack
    if [ "$PROXY" = "nginx" ]; then setup_nginx; fi
    log "Lamnezia is running at https://$DOMAIN/"
    log "Create the first administrator: cd $DIR && docker compose exec web python manage.py createadmin --username admin"
}

main "$@"
