#!/usr/bin/env bash
# SPDX-License-Identifier: AGPL-3.0-only
# Darkula-owned observability infrastructure lifecycle (PR 16).
#
# Manages ONLY exact Darkula-owned resources (names + darkula.owned=true):
#
#   network:   darkula-observability                        (label darkula.owned=true)
#   container: darkula-otel-collector                       (label darkula.owned=true)
#   container: darkula-jaeger                               (label darkula.owned=true)
#   container: darkula-prometheus                           (label darkula.owned=true)
#   image:     docker.io/otel/opentelemetry-collector-contrib:0.115.1
#   image:     docker.io/jaegertracing/all-in-one:1.62.0
#   image:     docker.io/prom/prometheus:v2.55.1
#
# Topology: Darkula --OTLP/HTTP--> Collector --OTLP/gRPC--> Jaeger (traces)
#                                         \--Prometheus scrape--> Prometheus.
# Host ports are explicit (five-digit prefix-3 choices: container port with
# the Darkula `3` prefix); container ports stay standard:
#
#   34317 -> 4317   Collector OTLP/gRPC
#   34318 -> 4318   Collector OTLP/HTTP   (Darkula exporter target)
#   31333 -> 13133  Collector health
#   31686 -> 16686  Jaeger query/UI
#   39090 -> 9090   Prometheus query/UI
#
# Ownership rule: an exact Darkula-looking name is NOT proof of ownership.
# Every mutable/removable resource must positively carry darkula.owned=true
# from exact-resource metadata. Unlabeled, wrongly labeled, or unverifiable
# resources fail closed: never auto-adopt, relabel, recreate, or delete them.
# Never prune, never scan broadly, never touch a foreign listener or port.
#
# Usage:
#   ./scripts/darkula_observability.sh status
#   ./scripts/darkula_observability.sh start
#   ./scripts/darkula_observability.sh stop
#   ./scripts/darkula_observability.sh clean
set -euo pipefail

cd "$(dirname "$0")/.."

NETWORK=darkula-observability
COLLECTOR=darkula-otel-collector
JAEGER=darkula-jaeger
PROMETHEUS=darkula-prometheus

COLLECTOR_IMAGE=docker.io/otel/opentelemetry-collector-contrib:0.115.1
JAEGER_IMAGE=docker.io/jaegertracing/all-in-one:1.62.0
PROMETHEUS_IMAGE=docker.io/prom/prometheus:v2.55.1

COLLECTOR_CONFIG=config/observability/otel-collector.yaml
PROMETHEUS_CONFIG=config/observability/prometheus.yml

COLLECTOR_GRPC_HOST=34317
COLLECTOR_GRPC_CONTAINER=4317
COLLECTOR_HTTP_HOST=34318
COLLECTOR_HTTP_CONTAINER=4318
COLLECTOR_HEALTH_HOST=31333
COLLECTOR_HEALTH_CONTAINER=13133
JAEGER_QUERY_HOST=31686
JAEGER_QUERY_CONTAINER=16686
PROMETHEUS_HOST=39090
PROMETHEUS_CONTAINER=9090

PODMAN="$(command -v podman || true)"
if [[ -z "$PODMAN" ]]; then
  echo "error: podman is required but was not found on PATH" >&2
  exit 2
fi

log() { echo "darkula-observability: $*"; }

# ownership_ok <network|container> <name>
#   0: exists and is darkula.owned=true; 1: exists but foreign; 2: unverifiable.
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

# require_owned <network|container> <name> <context>
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

any_observability_container_exists() {
  local name
  for name in "$COLLECTOR" "$JAEGER" "$PROMETHEUS"; do
    if $PODMAN container exists "$name" 2>/dev/null; then
      return 0
    fi
  done
  return 1
}

# host_port_in_use_by_foreign <port>
host_port_in_use_by_foreign() {
  local port="$1"
  # A Darkula-owned observability container may own a required port.
  if any_observability_container_exists; then
    return 1
  fi
  if command -v ss >/dev/null 2>&1; then
    if ss -ltn 2>/dev/null | awk '{print $4}' | grep -q ":$port$"; then
      return 0
    fi
    return 1
  fi
  if $PODMAN ps --format '{{.Ports}}' 2>/dev/null | grep -q ":$port->"; then
    return 0
  fi
  return 1
}

ensure_network_acceptable() {
  if $PODMAN network exists "$NETWORK" 2>/dev/null; then
    require_owned network "$NETWORK" "refusing to reuse, relabel, or remove it"
  fi
}

ensure_containers_acceptable() {
  local name
  for name in "$COLLECTOR" "$JAEGER" "$PROMETHEUS"; do
    if $PODMAN container exists "$name" 2>/dev/null; then
      require_owned container "$name" "refusing to touch it"
    fi
  done
}

check_required_ports() {
  local port
  for port in "$COLLECTOR_GRPC_HOST" "$COLLECTOR_HTTP_HOST" "$COLLECTOR_HEALTH_HOST" \
              "$JAEGER_QUERY_HOST" "$PROMETHEUS_HOST"; do
    if host_port_in_use_by_foreign "$port"; then
      echo "error: host port $port is occupied by a non-Darkula listener; refusing to proceed (no fallback port, no removal)" >&2
      exit 2
    fi
  done
}

pull_images() {
  local image
  for image in "$COLLECTOR_IMAGE" "$JAEGER_IMAGE" "$PROMETHEUS_IMAGE"; do
    if ! $PODMAN image exists "$image" >/dev/null 2>&1; then
      log "pulling $image"
      $PODMAN pull "$image" >/dev/null
    fi
  done
}

provision_network() {
  if $PODMAN network exists "$NETWORK" >/dev/null 2>&1; then
    log "network present: $NETWORK"
    return 0
  fi
  log "creating network $NETWORK"
  $PODMAN network create --label darkula.owned=true --label darkula.service=observability "$NETWORK" >/dev/null
}

run_owned_container() {
  # run_owned_container <name> <service-label> <args...>
  local name="$1" service="$2"
  shift 2
  if $PODMAN container exists "$name" 2>/dev/null; then
    local state
    state="$($PODMAN inspect --format '{{.State}}' "$name")"
    if [[ "$state" != "running" ]]; then
      log "starting existing $name"
      $PODMAN start "$name" >/dev/null
    else
      log "$name already running"
    fi
    return 0
  fi
  log "creating $name"
  $PODMAN run -d \
    --name "$name" \
    --hostname "$name" \
    --network "$NETWORK" \
    --label darkula.owned=true \
    --label "darkula.service=$service" \
    "$@" >/dev/null
}

provision_jaeger() {
  run_owned_container "$JAEGER" jaeger \
    -p "$JAEGER_QUERY_HOST:$JAEGER_QUERY_CONTAINER" \
    -e COLLECTOR_OTLP_ENABLED=true \
    "$JAEGER_IMAGE"
}

provision_prometheus() {
  run_owned_container "$PROMETHEUS" prometheus \
    -p "$PROMETHEUS_HOST:$PROMETHEUS_CONTAINER" \
    -v "$PWD/$PROMETHEUS_CONFIG:/etc/prometheus/prometheus.yml:ro" \
    "$PROMETHEUS_IMAGE" \
    --config.file=/etc/prometheus/prometheus.yml \
    --storage.tsdb.path=/prometheus
}

provision_collector() {
  run_owned_container "$COLLECTOR" otel-collector \
    -p "$COLLECTOR_GRPC_HOST:$COLLECTOR_GRPC_CONTAINER" \
    -p "$COLLECTOR_HTTP_HOST:$COLLECTOR_HTTP_CONTAINER" \
    -p "$COLLECTOR_HEALTH_HOST:$COLLECTOR_HEALTH_CONTAINER" \
    -v "$PWD/$COLLECTOR_CONFIG:/etc/otelcol-contrib/config.yaml:ro" \
    "$COLLECTOR_IMAGE" \
    --config=/etc/otelcol-contrib/config.yaml
}

wait_http_ready() {
  # wait_http_ready <url> <timeout-seconds> <label>
  local url="$1" timeout="$2" label="$3" waited=0
  while (( waited < timeout )); do
    if python3 -c '
import sys, urllib.request
try:
    urllib.request.urlopen(sys.argv[1], timeout=3).read(1)
except Exception:
    sys.exit(1)
' "$url" >/dev/null 2>&1; then
      log "$label ready"
      return 0
    fi
    sleep 1
    waited=$((waited + 1))
  done
  echo "error: $label did not become ready within ${timeout}s" >&2
  return 1
}

cmd_status() {
  if $PODMAN network exists "$NETWORK" 2>/dev/null; then
    echo "darkula-observability: network present"
  else
    echo "darkula-observability: network absent"
  fi
  local name state
  for name in "$COLLECTOR" "$JAEGER" "$PROMETHEUS"; do
    if $PODMAN container exists "$name" 2>/dev/null; then
      state="$($PODMAN inspect --format '{{.State}}' "$name")"
      echo "darkula-observability: $name $state"
    else
      echo "darkula-observability: $name absent"
    fi
  done
}

cmd_start() {
  local timeout="${DARKULA_OBSERVABILITY_READY_TIMEOUT:-90}"
  ensure_network_acceptable
  ensure_containers_acceptable
  check_required_ports
  pull_images
  provision_network
  provision_jaeger
  provision_prometheus
  provision_collector
  if [[ "${DARKULA_OBSERVABILITY_SKIP_READY_CHECK:-false}" != "true" ]]; then
    wait_http_ready "http://127.0.0.1:$JAEGER_QUERY_HOST/api/services" "$timeout" "jaeger"
    wait_http_ready "http://127.0.0.1:$PROMETHEUS_HOST/-/ready" "$timeout" "prometheus"
    wait_http_ready "http://127.0.0.1:$COLLECTOR_HEALTH_HOST/" "$timeout" "otel-collector"
  fi
}

cmd_stop() {
  local name
  for name in "$COLLECTOR" "$JAEGER" "$PROMETHEUS"; do
    if $PODMAN container exists "$name" 2>/dev/null; then
      require_owned container "$name" "refusing to stop it"
      log "stopping $name"
      $PODMAN stop "$name" >/dev/null
    fi
  done
}

cmd_clean() {
  # Preflight every existing Darkula-named resource before any destructive
  # action; absent resources are safe no-ops.
  local name
  for name in "$COLLECTOR" "$JAEGER" "$PROMETHEUS"; do
    if $PODMAN container exists "$name" 2>/dev/null; then
      require_owned container "$name" "refusing destructive cleanup"
    fi
  done
  if $PODMAN network exists "$NETWORK" 2>/dev/null; then
    require_owned network "$NETWORK" "refusing destructive cleanup"
  fi
  for name in "$COLLECTOR" "$JAEGER" "$PROMETHEUS"; do
    if $PODMAN container exists "$name" 2>/dev/null; then
      log "removing owned container $name"
      $PODMAN rm -f "$name" >/dev/null
    fi
  done
  if $PODMAN network exists "$NETWORK" 2>/dev/null; then
    log "removing owned network $NETWORK"
    $PODMAN network rm "$NETWORK" >/dev/null
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
