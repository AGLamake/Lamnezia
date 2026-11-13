#!/bin/sh
# Forced command of the deploy SSH key: `deploy <commit>` updates the panel to that commit of
# the development branch (dev).
#
# Install as /usr/local/sbin/lamnezia-deploy and allow the key only this command in
# ~/.ssh/authorized_keys:  restrict,command="/usr/local/sbin/lamnezia-deploy" ssh-ed25519 AAAA...
# The GitHub token for the fetch comes on stdin and is never written to disk.
# Settings (optional) are read from /etc/default/lamnezia-deploy.
set -eu

LAMNEZIA_DIR=/opt/lamnezia
COMPOSE_PROJECT=lamnezia
BRANCH=dev
HEALTH_URL=
if [ -r /etc/default/lamnezia-deploy ]; then
    . /etc/default/lamnezia-deploy
fi

# shellcheck disable=SC2086  # the command is split into words on purpose
set -- ${SSH_ORIGINAL_COMMAND:-}
if [ $# -ne 2 ] || [ "$1" != deploy ] || ! printf '%s' "$2" | grep -Eqx '[0-9a-f]{40}'; then
    echo "usage: deploy <commit, 40 hex digits>" >&2
    exit 2
fi
commit=$2
IFS= read -r token || true
token=$(printf '%s' "$token" | tr -d '\r')
if [ -z "$token" ]; then
    echo "no GitHub token on stdin" >&2
    exit 2
fi

# One update at a time.
exec 9>/run/lock/lamnezia-deploy.lock
flock 9

cd "$LAMNEZIA_DIR"
if [ "$(git symbolic-ref --short HEAD)" != "$BRANCH" ]; then
    echo "$LAMNEZIA_DIR is not on $BRANCH, nothing is updated" >&2
    exit 1
fi
auth=$(printf 'x-access-token:%s' "$token" | base64 -w0)
GIT_CONFIG_COUNT=1 \
    GIT_CONFIG_KEY_0=http.https://github.com/.extraheader \
    GIT_CONFIG_VALUE_0="AUTHORIZATION: basic $auth" \
    git fetch -q origin "$BRANCH"
if ! git merge-base --is-ancestor "$commit" "origin/$BRANCH"; then
    echo "$commit is not on $BRANCH" >&2
    exit 1
fi
# A commit older than the checked-out one changes nothing: a late run never rolls back.
git merge -q --ff-only "$commit"
echo "Lamnezia at $(git log --oneline -1)"

# A ready image (LAMNEZIA_IMAGE, usually in .env, see compose.yaml) is pulled, not built.
image=${LAMNEZIA_IMAGE:-$(sed -n 's/^LAMNEZIA_IMAGE=//p' .env 2>/dev/null | tail -n 1 | tr -d "\"'\r")}
if [ -n "$image" ]; then
    export LAMNEZIA_IMAGE="$image"
    previous=$(docker image inspect -f '{{.Id}}' "$image" 2>/dev/null || true)
    echo "Pulling $image"
    docker compose -p "$COMPOSE_PROJECT" pull
    docker compose -p "$COMPOSE_PROJECT" up -d --no-build
    # The replaced version is no longer tagged; it is removed unless something still uses it.
    current=$(docker image inspect -f '{{.Id}}' "$image")
    if [ -n "$previous" ] && [ "$previous" != "$current" ]; then
        docker image rm "$previous" >/dev/null 2>&1 || true
    fi
else
    docker compose -p "$COMPOSE_PROJECT" up -d --build
    # Every build leaves the previous image untagged: drop those of this project and old build cache.
    docker image prune -f --filter "label=com.docker.compose.project=$COMPOSE_PROJECT" >/dev/null
    docker builder prune -f --filter until=168h >/dev/null
fi

if [ -z "$HEALTH_URL" ]; then
    exit 0
fi
for _ in $(seq 30); do
    if curl -fs -o /dev/null "$HEALTH_URL"; then
        echo "$HEALTH_URL answers"
        exit 0
    fi
    sleep 5
done
echo "$HEALTH_URL does not answer after the update" >&2
exit 1
