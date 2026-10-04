#!/usr/bin/env bash
# SPDX-License-Identifier: AGPL-3.0-only
# Deterministic local quality entry point.
#
# Usage:
#   ./build.sh --qa     ruff format check, ruff lint, strict mypy, unit tests + coverage gate
#   ./build.sh --sec    bandit source scan and pip-audit vulnerability audit
#   ./build.sh --intg   provision Darkula-owned PostgreSQL, apply migrations, run
#                       the real-PostgreSQL integration suite, then clean up
set -euo pipefail

cd "$(dirname "$0")"

run_qa() {
  uv run ruff format --check src tests
  uv run ruff check src tests
  uv run mypy
  uv run pytest
}

run_sec() {
  echo "==> bandit (source scan)"
  uv run bandit -r src -c pyproject.toml
  echo "==> pip-audit (dependency vulnerability audit)"
  uv run pip-audit
}

run_intg() {
  echo "==> provisioning Darkula-owned PostgreSQL (35432:5432)"
  ./scripts/darkula_postgres.sh start
  echo "==> provisioning Darkula-owned Redpanda (39092:9092)"
  ./scripts/darkula_redpanda.sh start
  echo "==> applying migrations"
  ./scripts/darkula_postgres.sh migrate --apply
  echo "==> running integration suite"
  status=0
  uv run pytest tests/integration -m integration --no-cov -q || status=$?
  echo "==> cleaning up verified Darkula-owned resources (preserving exit status $status)"
  ./scripts/darkula_redpanda.sh clean >/dev/null 2>&1 || true
  ./scripts/darkula_postgres.sh clean >/dev/null 2>&1 || true
  exit "$status"
}

case "${1:-}" in
  --qa)
    run_qa
    ;;
  --sec)
    run_sec
    ;;
  --intg)
    run_intg
    ;;
  "")
    echo "usage: $0 [--qa|--sec|--intg]" >&2
    exit 2
    ;;
  *)
    echo "unknown mode: $1" >&2
    echo "usage: $0 [--qa|--sec|--intg]" >&2
    exit 2
    ;;
esac
