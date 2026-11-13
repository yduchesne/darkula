#!/usr/bin/env bash
# SPDX-License-Identifier: AGPL-3.0-only
# Darkula-owned public HTTPS egress infrastructure (PR 20).
#
# Manages ONLY the following Darkula-owned resources (exact names + labels):
#
#   network: darkula-live-egress   (non-internal bridge; darkula.owned=true)
#   image:   localhost/darkula-https-egress:1 (pinned; never `latest`)
#
# It never starts a listener, crawler or proxy: the outer network alone grants
# no live crawl capability. It is never invoked by ./build.sh --qa/--sec/--intg
# or by scripts/darkula_crawler.sh.
#
# Usage:
#   ./scripts/darkula_https_egress.sh status
#   ./scripts/darkula_https_egress.sh start
#   ./scripts/darkula_https_egress.sh clean
#
# Ownership rule: an exact Darkula-looking name is NOT proof of ownership.
# Every mutable/removable resource must positively carry `darkula.owned=true`;
# unlabeled, wrongly labeled, or unverifiable resources fail closed and are
# never adopted, relabeled, recreated, or deleted.
set -euo pipefail

cd "$(dirname "$0")/.."

NETWORK=darkula-live-egress
GATEWAY_IMAGE=localhost/darkula-https-egress:1
GATEWAY_CONTAINERFILE=crawler-runtime/https-egress.Containerfile

PODMAN="$(command -v podman || true)"
if [[ -z "$PODMAN" ]]; then
  echo "error: podman is required but was not found on PATH" >&2
  exit 2
fi

log() { echo "darkula-https-egress: $*"; }

# ownership_ok <network> <name>
#   0 - exists with darkula.owned=true
#   1 - exists but is NOT Darkula-owned
#   2 - ownership could not be verified
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
      echo "error: Podman $kind '$name' could not be verified as Darkula-owned; $context" >&2
      ;;
  esac
  exit 2
}

network_is_internal() {
  local raw
  raw="$($PODMAN network inspect "$NETWORK" --format '{{.Internal}}' 2>/dev/null || true)"
  [[ "$raw" == "true" ]]
}

require_rootless() {
  local rootless
  rootless="$($PODMAN info --format '{{.Host.Security.Rootless}}' 2>/dev/null || true)"
  if [[ "$rootless" != "true" ]]; then
    echo "error: the PR 20 egress topology requires rootless Podman" >&2
    exit 2
  fi
}

image_present() {
  $PODMAN image exists "$GATEWAY_IMAGE" >/dev/null 2>&1
}

provision_network() {
  if $PODMAN network exists "$NETWORK" >/dev/null 2>&1; then
    require_owned network "$NETWORK" "refusing to reuse, relabel, or remove it"
    if network_is_internal; then
      echo "error: network '$NETWORK' is internal; refusing to change it" >&2
      exit 2
    fi
    log "network present: $NETWORK"
    return 0
  fi
  log "creating non-internal Darkula-owned network $NETWORK"
  $PODMAN network create \
    --label darkula.owned=true \
    --label darkula.service=https-egress \
    "$NETWORK" >/dev/null
}

build_image() {
  if image_present; then
    log "gateway image present: $GATEWAY_IMAGE"
    return 0
  fi
  log "building gateway image: $GATEWAY_IMAGE"
  $PODMAN build -f "$GATEWAY_CONTAINERFILE" -t "$GATEWAY_IMAGE" .
}

cmd_start() {
  require_rootless
  if $PODMAN network exists "$NETWORK" >/dev/null 2>&1; then
    require_owned network "$NETWORK" "refusing to touch it"
    if network_is_internal; then
      echo "error: network '$NETWORK' is internal; refusing to change it" >&2
      exit 2
    fi
  fi
  build_image
  provision_network
  cmd_status
}

cmd_status() {
  if $PODMAN network exists "$NETWORK" >/dev/null 2>&1; then
    if ownership_ok network "$NETWORK"; then
      echo "darkula-https-egress: network $NETWORK present (owned)"
    else
      echo "darkula-https-egress: network $NETWORK present (UNVERIFIED/FOREIGN)"
    fi
  else
    echo "darkula-https-egress: network $NETWORK absent"
  fi
  if image_present; then
    echo "darkula-https-egress: image $GATEWAY_IMAGE present"
  else
    echo "darkula-https-egress: image $GATEWAY_IMAGE absent"
  fi
}

cmd_clean() {
  if ! $PODMAN network exists "$NETWORK" >/dev/null 2>&1; then
    log "network absent: $NETWORK"
    cmd_status
    return 0
  fi
  require_owned network "$NETWORK" "refusing destructive cleanup"
  local attached
  attached="$($PODMAN ps -a --filter "network=$NETWORK" --format '{{.Names}}' 2>/dev/null || true)"
  if [[ -n "$attached" ]]; then
    echo "error: network '$NETWORK' still has attached containers; refusing cleanup" >&2
    exit 2
  fi
  log "removing owned network $NETWORK"
  $PODMAN network rm "$NETWORK" >/dev/null
  cmd_status
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
