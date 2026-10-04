#!/usr/bin/env bash
# SPDX-License-Identifier: AGPL-3.0-only
# Darkula-owned crawler/sandbox integration infrastructure (PR 7).
#
# Manages ONLY the following Darkula-owned resources (exact names + labels);
# it never discovers, modifies, removes, or prunes any other Podman
# resource, and it never scans broadly:
#
#   network:   darkula-intg              (internal bridge; label darkula.owned=true)
#   image:     localhost/darkula-crawler-runtime:1.63.0 (pinned; never `latest`)
#   image:     localhost/darkula-fakeworld-intg:1      (pinned; never `latest`)
#   container: darkula-fake-world-intg   (on darkula-intg; label darkula.owned=true)
#
# The Fake World HTTP service is reachable only container-to-container on the
# Darkula-owned internal network (no host port is published), so the prefix-3
# host-port rules are not involved.
#
# Usage:
#   ./scripts/darkula_crawler.sh status
#   ./scripts/darkula_crawler.sh start   # build images + provision network/container
#   ./scripts/darkula_crawler.sh clean   # stop + remove owned resources
#
# Ownership rule (PR 5B/PR 7): an exact Darkula-looking name is NOT proof of
# ownership. Every mutable/removable resource must positively carry the
# `darkula.owned=true` label. Unlabeled, wrongly labeled, or unverifiable
# resources are foreign/unknown and fail closed. `clean` preflights EVERY
# existing Darkula-named resource before any destructive action.
set -euo pipefail

cd "$(dirname "$0")/.."

NETWORK=darkula-intg
NETWORK_SUBNET=10.177.42.0/24
FAKEWORLD_CONTAINER=darkula-fake-world-intg
FAKEWORLD_IMAGE=localhost/darkula-fakeworld-intg:1
RUNTIME_IMAGE=localhost/darkula-crawler-runtime:1.63.0
# Internal service port (never host-published; prefix-3 rules not needed).
FAKEWORLD_PORT=8080

PLAYWRIGHT_VERSION="${DARKULA_PLAYWRIGHT_VERSION:-1.63.0}"

PODMAN="$(command -v podman || true)"
if [[ -z "$PODMAN" ]]; then
  echo "error: podman is required but was not found on PATH" >&2
  exit 2
fi

log() { echo "darkula-crawler: $*"; }

# ownership_ok <network|container> <name>
#   Returns:
#     0 - resource exists with darkula.owned=true
#     1 - resource exists but is NOT Darkula-owned
#     2 - ownership could not be verified (inspect failed / metadata unreadable)
ownership_ok() {
  local kind="$1" name="$2" labels status
  case "$kind" in
    network)
      labels="$($PODMAN network inspect "$name" 2>/dev/null | python3 -c '
import json, sys
try:
    d = json.load(sys.stdin)
    labels = d[0].get("labels", d[0].get("Labels", {}))
    print(labels if labels is not None else {})
except Exception:
    sys.exit(1)' 2>/dev/null)"
      ;;
    container)
      labels="$($PODMAN inspect "$name" 2>/dev/null | python3 -c '
import json, sys
try:
    d = json.load(sys.stdin)
    print(d[0]["Config"].get("Labels", {}))
except Exception:
    sys.exit(1)' 2>/dev/null)"
      ;;
    *)
      return 2
      ;;
  esac
  status=$?
  if [[ "$status" -ne 0 ]]; then
    return 2
  fi
  case "$labels" in
    *"'darkula.owned': 'true'"*|*'"darkula.owned": "true"'*) return 0 ;;
    *) return 1 ;;
  esac
}

require_owned() {
  local kind="$1" name="$2" context="$3" status
  if ownership_ok "$kind" "$name"; then
    return 0
  else
    status=$?
  fi
  case "$status" in
    1)
      echo "error: Podman $kind '$name' exists but is NOT Darkula-owned (missing darkula.owned=true label); $context" >&2
      ;;
    2)
      echo "error: Podman $kind '$name' could not be verified as Darkula-owned (inspect failed or metadata unreadable); $context" >&2
      ;;
  esac
  exit 2
}

build_images() {
  log "building crawler runtime image: $RUNTIME_IMAGE"
  if ! $PODMAN image exists "$RUNTIME_IMAGE" >/dev/null 2>&1; then
    $PODMAN build --build-arg "PLAYWRIGHT_VERSION=$PLAYWRIGHT_VERSION" \
      -f crawler-runtime/Containerfile -t "$RUNTIME_IMAGE" .
  else
    log "crawler runtime image present: $RUNTIME_IMAGE"
  fi
  log "building fake world image: $FAKEWORLD_IMAGE"
  if ! $PODMAN image exists "$FAKEWORLD_IMAGE" >/dev/null 2>&1; then
    $PODMAN build -f crawler-runtime/fakeworld.Containerfile -t "$FAKEWORLD_IMAGE" .
  else
    log "fake world image present: $FAKEWORLD_IMAGE"
  fi
}

provision_network() {
  if $PODMAN network exists "$NETWORK" >/dev/null 2>&1; then
    require_owned network "$NETWORK" "refusing to reuse, relabel, or remove it"
    log "network present: $NETWORK"
    return 0
  fi
  log "creating internal network $NETWORK ($NETWORK_SUBNET)"
  $PODMAN network create --internal --subnet "$NETWORK_SUBNET" \
    --label darkula.owned=true --label darkula.service=crawler-sandbox \
    "$NETWORK" >/dev/null
}

provision_fakeworld() {
  if $PODMAN container exists "$FAKEWORLD_CONTAINER" >/dev/null 2>&1; then
    require_owned container "$FAKEWORLD_CONTAINER" "refusing to touch it"
    local state
    state="$($PODMAN inspect --format '{{.State}}' "$FAKEWORLD_CONTAINER")"
    if [[ "$state" != "running" ]]; then
      log "starting existing fake world container"
      $PODMAN start "$FAKEWORLD_CONTAINER" >/dev/null
    else
      log "fake world container already running"
    fi
    return 0
  fi
  log "running fake world container $FAKEWORLD_CONTAINER on $NETWORK"
  $PODMAN run -d \
    --name "$FAKEWORLD_CONTAINER" \
    --network "$NETWORK" \
    --label darkula.owned=true \
    --label darkula.service=fake-world \
    --read-only \
    --tmpfs /tmp:rw,size=16m,mode=1777 \
    --user 10001 \
    "$FAKEWORLD_IMAGE" >/dev/null
}

wait_ready() {
  local timeout="${DARKULA_FAKEWORLD_READY_TIMEOUT:-60}"
  local waited=0
  while (( waited < timeout )); do
    if $PODMAN exec "$FAKEWORLD_CONTAINER" \
      python3 -c 'import urllib.request; urllib.request.urlopen("http://127.0.0.1:8080/", timeout=3)' \
      >/dev/null 2>&1; then
      log "fake world HTTP ready inside the internal network"
      return 0
    fi
    sleep 1
    waited=$((waited + 1))
  done
  echo "error: fake world HTTP did not become ready within ${timeout}s" >&2
  return 1
}

cmd_status() {
  for item in "$NETWORK" "$FAKEWORLD_CONTAINER"; do
    if $PODMAN network exists "$item" >/dev/null 2>&1 || $PODMAN container exists "$item" >/dev/null 2>&1; then
      echo "darkula-crawler: $item present"
    else
      echo "darkula-crawler: $item absent"
    fi
  done
}

cmd_start() {
  ensure_network_acceptable
  ensure_container_acceptable
  build_images
  provision_network
  provision_fakeworld
  wait_ready
}

ensure_network_acceptable() {
  if $PODMAN network exists "$NETWORK" >/dev/null 2>&1; then
    require_owned network "$NETWORK" "refusing to touch it"
  fi
}

ensure_container_acceptable() {
  if $PODMAN container exists "$FAKEWORLD_CONTAINER" >/dev/null 2>&1; then
    require_owned container "$FAKEWORLD_CONTAINER" "refusing to touch it"
  fi
}

cmd_clean() {
  # Preflight EVERY existing Darkula-named resource before any destructive
  # action; absent resources are safe no-ops.
  if $PODMAN container exists "$FAKEWORLD_CONTAINER" >/dev/null 2>&1; then
    require_owned container "$FAKEWORLD_CONTAINER" "refusing destructive cleanup"
  fi
  if $PODMAN network exists "$NETWORK" >/dev/null 2>&1; then
    require_owned network "$NETWORK" "refusing destructive cleanup"
  fi
  if $PODMAN container exists "$FAKEWORLD_CONTAINER" >/dev/null 2>&1; then
    log "removing owned container"
    $PODMAN rm -f "$FAKEWORLD_CONTAINER" >/dev/null
  fi
  if $PODMAN network exists "$NETWORK" >/dev/null 2>&1; then
    log "removing owned network"
    $PODMAN network rm "$NETWORK" >/dev/null
  fi
}

case "${1:-}" in
  status) cmd_status ;;
  start) cmd_start ;;
  clean) cmd_clean ;;
  *)
    echo "usage: $0 {status|start|clean}" >&2
    exit 2
    ;;
esac
