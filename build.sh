#!/usr/bin/env bash
# SPDX-License-Identifier: AGPL-3.0-only
# Deterministic local quality entry point.
#
# Usage:
#   ./build.sh --qa     ruff format check, ruff lint, strict mypy, unit tests + coverage gate
#   ./build.sh --sec    bandit source scan and pip-audit vulnerability audit
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

case "${1:-}" in
  --qa)
    run_qa
    ;;
  --sec)
    run_sec
    ;;
  "")
    echo "usage: $0 [--qa|--sec]" >&2
    exit 2
    ;;
  *)
    echo "unknown mode: $1" >&2
    echo "usage: $0 [--qa|--sec]" >&2
    exit 2
    ;;
esac
