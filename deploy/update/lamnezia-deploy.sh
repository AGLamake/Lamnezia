#!/bin/sh
# Updates the panel on its host. It runs in one of two ways:
#
#   deploy <commit>  forced command of the deploy SSH key (the Deploy workflow): moves the
#                    checkout to that commit of the development branch (BRANCH, dev). The GitHub
#                    token for the fetch comes on stdin and is never written to disk.
#   release          run by lamnezia-update.service of install.sh when the panel leaves a request
#                    in UPDATE_DIR/request: moves to that release tag vX.Y.Z of the branch the
#                    installation follows, checked against the signed manifest of the GitHub
#                    release, and goes back to the previous version when the panel does not come
#                    up healthy.
#                    The status and the log are written to UPDATE_DIR/status on any exit.
#
# Neither runs while the panel moves to another machine (move_under_way below).
#
# Install as /usr/local/sbin/lamnezia-deploy (install.sh does it). For the SSH key allow only this
# command in ~/.ssh/authorized_keys:
#   restrict,command="/usr/local/sbin/lamnezia-deploy" ssh-ed25519 AAAA...
# Settings (optional) are read from /etc/default/lamnezia-deploy.
set -eu

LAMNEZIA_DIR=${LAMNEZIA_DIR:-/opt/lamnezia}
BRANCH=dev
HEALTH_URL=
HEALTH_TRIES=60
HEALTH_DELAY=5
RELEASES_URL=
RELEASE_SIGNERS=
LOCK_FILE=${LOCK_FILE:-/run/lock/lamnezia-deploy.lock}
DEPLOY_SETTINGS=${DEPLOY_SETTINGS:-/etc/default/lamnezia-deploy}
if [ -r "$DEPLOY_SETTINGS" ]; then
    . "$DEPLOY_SETTINGS"
fi
COMPOSE_PROJECT=${COMPOSE_PROJECT:-$(basename "$LAMNEZIA_DIR")}
UPDATE_DIR=${UPDATE_DIR:-$LAMNEZIA_DIR/update}
# Published with every release (.github/workflows/release.yml): the version, the tagged commit and
# the image digest, with an SSH signature in the namespace below.
MANIFEST=lamnezia-release.txt
SIGNATURE=$MANIFEST.sig
SIGNING_NAMESPACE=lamnezia-release
# The keys a release must be signed with: those of the running version, never of the one it gets.
RELEASE_SIGNERS=${RELEASE_SIGNERS:-$LAMNEZIA_DIR/deploy/update/release-signers}

compose() {
    docker compose -p "$COMPOSE_PROJECT" "$@"
}

# The ready image (LAMNEZIA_IMAGE in .env, see compose.yaml); empty when the image is built here.
env_image() {
    sed -n 's/^LAMNEZIA_IMAGE=//p' .env 2>/dev/null | tail -n 1 | tr -d "\"'\r"
}

# Every build leaves the previous image untagged: drop those of this project and old build cache.
prune_build() {
    docker image prune -f --filter "label=com.docker.compose.project=$COMPOSE_PROJECT" >/dev/null
    docker builder prune -f --filter until=168h >/dev/null
}

# HEALTH_URL when it is set, otherwise the health check of the web container.
wait_healthy() {
    for _ in $(seq "$HEALTH_TRIES"); do
        if [ -n "$HEALTH_URL" ]; then
            curl -fs -o /dev/null --max-time 5 "$HEALTH_URL" && return 0
        elif compose ps --format '{{.Service}} {{.Health}}' | grep -qx 'web healthy'; then
            return 0
        fi
        sleep "$HEALTH_DELAY"
    done
    return 1
}

# The panel keeps UPDATE_DIR/request/moving ("<move id> <until, Unix time>") while an operation of
# a panel move runs: a rebuild would kill it halfway. A flag past its time was left by a dead
# worker and counts for nothing. Only a regular file is read, as with the request.
move_under_way() {
    flag=$UPDATE_DIR/request/moving
    [ -f "$flag" ] && [ ! -L "$flag" ] || return 1
    till=$(timeout 10 head -c 100 "$flag" | head -n 1 | tr -d '\r' |
        sed -n 's/^[0-9][0-9]* \([0-9][0-9]*\)$/\1/p') || return 1
    [ -n "$till" ] && [ "$till" -gt "$(date +%s)" ]
}

deploy() {
    if [ $# -ne 1 ] || ! printf '%s' "$1" | grep -Eqx '[0-9a-f]{40}'; then
        echo "usage: deploy <commit, 40 hex digits>" >&2
        exit 2
    fi
    commit=$1
    IFS= read -r token || true
    token=$(printf '%s' "$token" | tr -d '\r')
    if [ -z "$token" ]; then
        echo "no GitHub token on stdin" >&2
        exit 2
    fi

    # One update at a time.
    exec 9>"$LOCK_FILE"
    flock 9
    if move_under_way; then
        echo "The panel is moving to another machine: nothing is updated, deploy again later" >&2
        exit 75
    fi

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

    # A ready image is pulled, not built.
    image=${LAMNEZIA_IMAGE:-$(env_image)}
    if [ -n "$image" ]; then
        export LAMNEZIA_IMAGE="$image"
        previous=$(docker image inspect -f '{{.Id}}' "$image" 2>/dev/null || true)
        echo "Pulling $image"
        compose pull
        compose up -d --no-build
        # The replaced version is no longer tagged; it is removed unless something still uses it.
        current=$(docker image inspect -f '{{.Id}}' "$image")
        if [ -n "$previous" ] && [ "$previous" != "$current" ]; then
            docker image rm "$previous" >/dev/null 2>&1 || true
        fi
    else
        compose up -d --build
        prune_build
    fi

    if [ -z "$HEALTH_URL" ]; then
        exit 0
    fi
    if wait_healthy; then
        echo "$HEALTH_URL answers"
        exit 0
    fi
    echo "$HEALTH_URL does not answer after the update" >&2
    exit 1
}

# --- release: the update the panel asks for ---------------------------------------------------

# Status for the panel (System -> Panel update), replaced whole so it is never read half-written.
write_status() {
    {
        echo "run=$run"
        echo "state=$state"
        echo "reason=$reason"
        echo "version=$version"
        echo "previous=$previous_version"
        echo "started=$started"
        echo "finished=$finished"
    } >"$status_dir/.status"
    mv -f "$status_dir/.status" "$status_dir/status"
}

finish() {
    code=$?
    if [ "$state" = running ]; then
        state=failed
        reason=${reason:-interrupted}
    fi
    finished=$(date +%s)
    write_status
    if [ -n "$work" ]; then
        rm -rf "$work"
    fi
    exit "$code"
}

# Nothing has changed yet: the panel runs the version it ran before.
fail() {
    reason=$1
    state=failed
    echo "Not updated: $reason"
    exit 1
}

# The request is the only thing the panel gives: "vX.Y.Z <run id>", taken once and removed.
# The panel owns the request folder, so only a regular file is read, and only its first line.
read_request() {
    file=$UPDATE_DIR/request/update
    line=
    if [ -f "$file" ] && [ ! -L "$file" ]; then
        line=$(timeout 10 head -c 100 "$file" | head -n 1 | tr -d '\r') || line=
    fi
    rm -f "$file" 2>/dev/null || true
    if printf '%s\n' "$line" | grep -Eqx 'v[0-9]+\.[0-9]+\.[0-9]+ [0-9a-f]{32}'; then
        version=${line% *}
        run=${line#* }
    fi
}

manifest_value() {
    printf '%s\n' "$manifest" | sed -n "s/^$1 //p" | head -n 1
}

releases_url() {
    if [ -n "$RELEASES_URL" ]; then
        printf '%s' "$RELEASES_URL"
        return
    fi
    origin=$(git remote get-url origin)
    case "$origin" in
        https://github.com/*) printf '%s/releases/download' "${origin%.git}" ;;
        git@github.com:*) origin=${origin#git@github.com:}; printf 'https://github.com/%s/releases/download' "${origin%.git}" ;;
    esac
}

# The manifest counts only with a valid signature by a key of RELEASE_SIGNERS. It is read from
# files, so the signature covers exactly the bytes that are read.
fetch_manifest() {
    work=$(mktemp -d)
    curl -fsSL --max-time 60 "$url/$version/$MANIFEST" >"$work/$MANIFEST" || fail unverified
    curl -fsSL --max-time 60 "$url/$version/$SIGNATURE" >"$work/$SIGNATURE" || fail unsigned
    if ! grep -q '^[^#[:space:]]' "$RELEASE_SIGNERS" 2>/dev/null; then
        echo "No release signing key in $RELEASE_SIGNERS"
        fail bad_signature
    fi
    ssh-keygen -Y verify -f "$RELEASE_SIGNERS" -I "$SIGNING_NAMESPACE" -n "$SIGNING_NAMESPACE" \
        -s "$work/$SIGNATURE" <"$work/$MANIFEST" || fail bad_signature
    manifest=$(cat "$work/$MANIFEST")
}

# The tag must be on the branch of the installation, newer than the running version, and named
# by the signed manifest the release workflow published.
check_release() {
    git fetch -q --no-tags origin "refs/heads/$channel:refs/remotes/origin/$channel" \
        "refs/tags/$version:refs/tags/$version" || fail fetch_failed
    commit=$(git rev-parse -q --verify "refs/tags/$version^{commit}") || fail fetch_failed
    git merge-base --is-ancestor "$commit" "refs/remotes/origin/$channel" || fail not_in_channel
    if [ "$commit" = "$previous" ] || ! git merge-base --is-ancestor "$previous" "$commit"; then
        fail not_newer
    fi
    url=$(releases_url)
    [ -n "$url" ] || fail unverified
    fetch_manifest
    [ "$(manifest_value version)" = "$version" ] || fail unverified
    [ "$(manifest_value commit)" = "$commit" ] || fail unverified
    echo "$version is commit $commit, as its signed release says"
}

# The image of the release, pulled and checked before anything is stopped.
pull_release_image() {
    digest=$(manifest_value image)
    printf '%s' "$digest" | grep -Eqx 'sha256:[0-9a-f]{64}' || fail unverified
    repository=${old_image%@*}
    case "${repository##*/}" in *:*) repository=${repository%:*} ;; esac
    new_image=$repository:${version#v}
    echo "Pulling $new_image"
    docker pull -q "$new_image" || fail pull_failed
    docker image inspect -f '{{range .RepoDigests}}{{println .}}{{end}}' "$new_image" |
        grep -q "@$digest\$" || fail unverified
}

set_env_image() {
    sed -i "s|^LAMNEZIA_IMAGE=.*|LAMNEZIA_IMAGE=$1|" .env
}

move_code() {
    if [ -n "$branch" ]; then
        git merge -q --ff-only "$commit"
    else
        git checkout -q --detach "$commit"
    fi
}

restore_code() {
    if [ -n "$branch" ]; then
        git reset -q --keep "$previous"
    else
        git checkout -q --detach "$previous"
    fi
}

# After a failure past the switch: the previous code and image are started again.
roll_back() {
    reason=$1
    echo "Update failed ($reason), going back to $previous_version"
    if restore_code && start_version "$old_image" && wait_healthy; then
        state=rolled_back
    else
        state=failed
        reason=rollback_failed
    fi
    exit 1
}

start_version() {
    if [ -n "$1" ]; then
        set_env_image "$1"
        compose up -d --no-build
    else
        compose up -d --build
    fi
}

release() {
    # systemd never runs two of these at once; a deploy over SSH is waited for, not skipped, so the
    # request is not left for the path unit to trigger on again and again.
    exec 9>"$LOCK_FILE"
    flock 9
    status_dir=$UPDATE_DIR/status
    run= version= reason= previous_version= finished= work=
    state=running
    started=$(date +%s)
    read_request
    exec >"$status_dir/log" 2>&1
    trap finish EXIT
    trap 'exit 143' HUP INT TERM
    write_status
    [ -n "$version" ] || fail bad_request
    if move_under_way; then
        fail moving
    fi
    cd "$LAMNEZIA_DIR"
    [ -d .git ] || fail not_git
    # An installation on a detached HEAD (install.sh before 1.1) follows the releases branch.
    branch=$(git symbolic-ref -q --short HEAD || true)
    channel=${branch:-releases}
    echo "$channel" >"$status_dir/agent"
    previous=$(git rev-parse HEAD)
    previous_version=$(git describe --tags --exact-match HEAD 2>/dev/null || git rev-parse --short HEAD)
    echo "Updating $previous_version to $version ($channel)"
    write_status
    check_release
    old_image=$(env_image)
    if [ -n "$old_image" ]; then
        pull_release_image
    fi

    move_code || fail switch_failed
    if [ -n "$old_image" ]; then
        set_env_image "$new_image"
    else
        compose build -q || roll_back build_failed
    fi
    compose up -d --no-build || roll_back start_failed
    wait_healthy || roll_back unhealthy
    state=done
    echo "Lamnezia $version is running"

    if [ -n "$old_image" ] && [ "$old_image" != "$new_image" ]; then
        docker image rm "$old_image" >/dev/null 2>&1 || true
    elif [ -z "$old_image" ]; then
        prune_build
    fi
    # The installed copy of this script follows the release it updated to.
    self=$(readlink -f "$0")
    script=$LAMNEZIA_DIR/deploy/update/lamnezia-deploy.sh
    if [ -f "$script" ] && [ "$self" != "$(readlink -f "$script")" ]; then
        install -m 755 "$script" "$self"
    fi
}

case "${1:-}" in
    release) release ;;
    *)
        # shellcheck disable=SC2086  # the command is split into words on purpose
        set -- ${SSH_ORIGINAL_COMMAND:-}
        if [ "${1:-}" != deploy ]; then
            echo "usage: deploy <commit, 40 hex digits>" >&2
            exit 2
        fi
        shift
        deploy "$@"
        ;;
esac
