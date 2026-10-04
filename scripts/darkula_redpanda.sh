#!/usr/bin/env bash
# SPDX-License-Identifier: AGPL-3.0-only
# Darkula-owned Redpanda lifecycle tooling (PR 5).
#
# Manages ONLY the following Darkula-owned resource (exact name + labels);
# it never discovers, modifies, removes, or prunes any other Podman
# resource, and it never scans broadly for containers/volumes/networks:
#
#   container: darkula-redpanda        (label darkula.owned=true)
#   image:     docker.io/redpandadata/redpanda:v24.3.8 (pinned; never `latest`)
#
# Host mapping (exact): host port 39092 -> container Kafka port 9092.
#
# Usage:
#   ./scripts/darkula_redpanda.sh status
#   ./scripts/darkula_redpanda.sh start      # provision + wait ready
#   ./scripts/darkula_redpanda.sh stop       # stop (container kept)
#   ./scripts/darkula_redpanda.sh clean      # stop + remove owned container
#
# Ownership rule (PR 5B/PR 5): an exact Darkula-looking name is NOT proof
# of ownership. Every mutable/removable resource must positively carry the
# `darkula.owned=true` label. Unlabeled, wrongly labeled, or unverifiable
# resources are foreign/unknown and fail closed: never auto-adopt,
# relabel, recreate, or delete them. `clean` preflights EVERY existing
# Darkula-named resource before any destructive action.
#
# Fail-safe rules:
#   * if 39092 is occupied by anything other than our container -> fail;
#   * if a resource has our name but not our ownership labels -> fail;
#   * never prune, never remove non-Darkula resources, never use another
#     host port (an ambiguous mapping is an explicit STOP, not a fallback).
set -euo pipefail

cd "$(dirname "$0")/.."

CONTAINER=darkula-redpanda
IMAGE=docker.io/redpandadata/redpanda:v24.3.8
HOST_PORT=39092
CONTAINER_PORT=9092
# The advertised Kafka address is the host-reachable 127.0.0.1:39092 so
# host-side Darkula tooling and the integration suite can reach the broker
# through the published port; the in-container listener stays on the
# standard service port 9092 for container-to-container consumers.

PODMAN="$(command -v podman || true)"
if [[ -z "$PODMAN" ]]; then
  echo "error: podman is required but was not found on PATH" >&2
  exit 2
fi

log() { echo "darkula-redpanda: $*"; }

# ownership_ok <name>
#   Returns:
#     0 - container exists with darkula.owned=true
#     1 - container exists but is NOT Darkula-owned
#     2 - ownership could not be verified (inspect failed / metadata unreadable)
# Metadata is read from the exact named resource only (never broad
# discovery).
ownership_ok() {
  local labels status
  labels="$($PODMAN inspect "$CONTAINER" 2>/dev/null | python3 -c '
import json, sys
try:
    d = json.load(sys.stdin)
    print(d[0]["Config"].get("Labels", {}))
except Exception:
    sys.exit(1)' 2>/dev/null)"
  status=$?
  if [[ "$status" -ne 0 ]]; then
    return 2
  fi
  case "$labels" in
    *"'darkula.owned': 'true'"*|*'"darkula.owned": "true"'*) return 0 ;;
    *) return 1 ;;
  esac
}

# require_owned <context message>
# Exits 2 unless the named resource (when present) is positively
# Darkula-owned. Never relabels, adopts, or removes an unverified resource.
require_owned() {
  local context="$1" status
  if ownership_ok; then
    return 0
  else
    status=$?
  fi
  case "$status" in
    1)
      echo "error: Podman container '$CONTAINER' exists but is NOT Darkula-owned (missing darkula.owned=true label); $context" >&2
      ;;
    2)
      echo "error: Podman container '$CONTAINER' could not be verified as Darkula-owned (inspect failed or metadata unreadable); $context" >&2
      ;;
  esac
  exit 2
}

host_port_in_use_by_foreign() {
  # Only our container may publish 39092. We never modify foreign listeners.
  if $PODMAN container exists "$CONTAINER" 2>/dev/null; then
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
  $PODMAN run -d \
    --name "$CONTAINER" \
    --hostname "$CONTAINER" \
    --label darkula.owned=true \
    --label darkula.service=redpanda \
    -p "$HOST_PORT:$CONTAINER_PORT" \
    "$IMAGE" \
    redpanda start --overprovisioned \
    --kafka-addr 0.0.0.0:9092 \
    --advertise-kafka-addr 127.0.0.1:39092 \
    >/dev/null
}

wait_ready() {
  local timeout="${DARKULA_REDPANDA_READY_TIMEOUT:-90}"
  local waited=0
  while (( waited < timeout )); do
    if $PODMAN exec "$CONTAINER" rpk -X brokers=127.0.0.1:9092 cluster info >/dev/null 2>&1; then
      log "ready at host 127.0.0.1:$HOST_PORT -> container $CONTAINER_PORT"
      return 0
    fi
    sleep 2
    waited=$((waited + 2))
  done
  echo "error: redpanda did not become ready within ${timeout}s" >&2
  return 1
}

ensure_container_acceptable() {
  if $PODMAN container exists "$CONTAINER" 2>/dev/null; then
    require_owned "refusing to touch it"
  fi
}

cmd_status() {
  if $PODMAN container exists "$CONTAINER" 2>/dev/null; then
    local state
    state="$($PODMAN inspect --format '{{.State}}' "$CONTAINER")"
    echo "darkula-redpanda: $state"
  else
    echo "darkula-redpanda: absent"
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
  # Preflight ownership before any destructive action; absent is a safe
  # no-op. Only then remove the owned container by exact name.
  if $PODMAN container exists "$CONTAINER" 2>/dev/null; then
    require_owned "refusing destructive cleanup"
    log "removing owned container"
    $PODMAN rm -f "$CONTAINER" >/dev/null
  else
    log "nothing to clean"
  fi
}

case "${1:-}" in
  status) cmd_status ;;
  start) cmd_start ;;
  stop) cmd_stop ;;
  clean) cmd_clean ;;
  *)
    echo "usage: $0 {status|start|stop|clean}" >&2
    exit 2
    ;;
esac
