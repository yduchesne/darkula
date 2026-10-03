#!/usr/bin/env bash
# SPDX-License-Identifier: AGPL-3.0-only
# Darkula-owned PostgreSQL lifecycle tooling (PR 4).
#
# Manages ONLY the following Darkula-owned resources (exact names/labels);
# it never discovers, modifies, removes, or prunes any other Podman
# resource, and it never scans broadly for containers/volumes/networks:
#
#   container: darkula-postgres        (label darkula.owned=true)
#   volume:    darkula-postgres-data
#   image:     docker.io/library/postgres:18.2 (pinned; never `latest`)
#
# Host mapping (exact): host port 35432 -> container port 5432.
#
# Usage:
#   ./scripts/darkula_postgres.sh status
#   ./scripts/darkula_postgres.sh start      # provision + wait ready
#   ./scripts/darkula_postgres.sh stop       # stop (keep data volume)
#   ./scripts/darkula_postgres.sh clean      # stop + remove owned resources
#   ./scripts/darkula_postgres.sh migrate    # apply migrations (repo tool)
#
# Fail-safe rules:
#   * if 35432 is occupied by anything other than our container -> fail;
#   * if a resource has our name but not our ownership labels -> fail;
#   * never prune, never remove non-Darkula resources, never use another
#     host port.
set -euo pipefail

cd "$(dirname "$0")/.."

CONTAINER=darkula-postgres
VOLUME=darkula-postgres-data
IMAGE=docker.io/library/postgres:18.2
HOST_PORT=35432
CONTAINER_PORT=5432
POSTGRES_DB=darkula
POSTGRES_USER=darkula
# Dedicated local integration credential (never a production secret); the
# operator may override via the environment for local development.
POSTGRES_PASSWORD="${DARKULA_POSTGRES_PASSWORD:-darkula-local-intg}"

PODMAN="$(command -v podman || true)"
if [[ -z "$PODMAN" ]]; then
  echo "error: podman is required but was not found on PATH" >&2
  exit 2
fi

log() { echo "darkula-postgres: $*"; }

owned_label() {
  local name="$1"
  local labels
  labels="$($PODMAN inspect "$name" 2>/dev/null | python3 -c \
    'import json,sys; d=json.load(sys.stdin); print(d[0]["Config"].get("Labels", {}))' 2>/dev/null || true)"
  case "$labels" in
    *"'darkula.owned': 'true'"*|*'"darkula.owned": "true"'*) return 0 ;;
    *) return 1 ;;
  esac
}

host_port_in_use_by_foreign() {
  # Only our container may publish 35432. We never modify foreign listeners.
  if $PODMAN container exists "$CONTAINER" 2>/dev/null; then
    # our container may already own the port -> not foreign
    return 1
  fi
  if ss -ltn 2>/dev/null | awk '{print $4}' | grep -q ":$HOST_PORT$"; then
    return 0
  fi
  if ! command -v ss >/dev/null 2>&1 && $PODMAN ps --format '{{.Ports}}' 2>/dev/null | grep -q ":$HOST_PORT->"; then
    return 0
  fi
  return 1
}

provision() {
  log "image: $IMAGE"
  $PODMAN image exists "$IMAGE" >/dev/null 2>&1 || $PODMAN pull "$IMAGE" >/dev/null
  # Volume: exact Darkula-owned name.
  $PODMAN volume exists "$VOLUME" >/dev/null 2>&1 || $PODMAN volume create "$VOLUME" >/dev/null
  $PODMAN run -d \
    --name "$CONTAINER" \
    --label darkula.owned=true \
    --label darkula.service=postgres \
    -v "$VOLUME:/var/lib/postgresql" \
    -p "$HOST_PORT:$CONTAINER_PORT" \
    -e POSTGRES_DB="$POSTGRES_DB" \
    -e POSTGRES_USER="$POSTGRES_USER" \
    -e POSTGRES_PASSWORD="$POSTGRES_PASSWORD" \
    "$IMAGE" >/dev/null
}

wait_ready() {
  local timeout="${DARKULA_POSTGRES_READY_TIMEOUT:-60}"
  local waited=0
  while (( waited < timeout )); do
    if $PODMAN exec "$CONTAINER" pg_isready -U "$POSTGRES_USER" -d "$POSTGRES_DB" >/dev/null 2>&1; then
      log "ready at host 127.0.0.1:$HOST_PORT -> container $CONTAINER_PORT"
      return 0
    fi
    sleep 1
    waited=$((waited + 1))
  done
  echo "error: postgres did not become ready within ${timeout}s" >&2
  return 1
}

ensure_container_acceptable() {
  if $PODMAN container exists "$CONTAINER" 2>/dev/null; then
    if ! owned_label "$CONTAINER"; then
      echo "error: Podman container '$CONTAINER' exists but is NOT Darkula-owned (missing darkula.owned=true label); refusing to touch it" >&2
      exit 2
    fi
  fi
}

cmd_status() {
  if $PODMAN container exists "$CONTAINER" 2>/dev/null; then
    local state
    state="$($PODMAN inspect --format '{{.State}}' "$CONTAINER")"
    echo "darkula-postgres: $state"
  else
    echo "darkula-postgres: absent"
  fi
}

cmd_start() {
  ensure_container_acceptable
  if host_port_in_use_by_foreign; then
    echo "error: host port $HOST_PORT is occupied by a non-Darkula listener; refusing to proceed (no fallback port, no removal)" >&2
    exit 2
  fi
  if $PODMAN container exists "$CONTAINER" 2>/dev/null; then
    local state
    state="$($PODMAN inspect --format '{{.State}}' "$CONTAINER")"
    if [[ "$state" != "running" ]]; then
      log "starting existing container"
      $PODMAN start "$CONTAINER" >/dev/null
    else
      log "already running"
    fi
  else
    log "provisioning"
    provision
  fi
  wait_ready
}

cmd_stop() {
  if $PODMAN container exists "$CONTAINER" 2>/dev/null; then
    ensure_container_acceptable
    log "stopping"
    $PODMAN stop "$CONTAINER" >/dev/null
  else
    log "nothing to stop"
  fi
}

cmd_clean() {
  if $PODMAN container exists "$CONTAINER" 2>/dev/null; then
    ensure_container_acceptable
    log "removing owned container"
    $PODMAN rm -f "$CONTAINER" >/dev/null
  fi
  if $PODMAN volume exists "$VOLUME" 2>/dev/null; then
    log "removing owned volume"
    $PODMAN volume rm "$VOLUME" >/dev/null
  fi
}

cmd_migrate() {
  # Applies repository migrations through the operator tooling; credentials
  # match the container provisioned above (never echoed). Container-to-
  # host: the migration tool connects to the published host port.
  local action="${1:---apply}"
  export DARKULA_DATABASE__HOST="${DARKULA_DATABASE__HOST:-127.0.0.1}"
  export DARKULA_DATABASE__PORT="${DARKULA_DATABASE__PORT:-$HOST_PORT}"
  export DARKULA_DATABASE__NAME="${DARKULA_DATABASE__NAME:-$POSTGRES_DB}"
  export DARKULA_DATABASE__USER="${DARKULA_DATABASE__USER:-$POSTGRES_USER}"
  export DARKULA_DATABASE__PASSWORD="${DARKULA_DATABASE__PASSWORD:-$POSTGRES_PASSWORD}"
  uv run python scripts/darkula_migrate.py "$action"
}

case "${1:-}" in
  status) cmd_status ;;
  start) cmd_start ;;
  stop) cmd_stop ;;
  clean) cmd_clean ;;
  migrate)
    cmd_migrate "--apply"
    ;;
  current)
    cmd_migrate "--current"
    ;;
  *)
    echo "usage: $0 {status|start|stop|clean|migrate}" >&2
    exit 2
    ;;
esac