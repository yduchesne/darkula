#!/usr/bin/env bash
# SPDX-License-Identifier: AGPL-3.0-only
# Bootstrap Darkula's Python toolchain and project environment.
#
# Idempotent: safe to re-run at any time.
#
#   1. ensures the `uv` launcher is available (installs it without sudo when
#      missing, via the official astral.sh installer);
#   2. drives the initial dependency download and project setup with
#      `uv sync --locked` (creates/refreshes `.venv` from the committed
#      `uv.lock` — never silently upgrades outside the lock);
#   3. installs the pre-commit git hooks from the locked environment.
set -Eeuo pipefail

ROOT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
#: Minimum `uv` version able to consume the committed lockfile schema.
UV_MIN_VERSION=${UV_MIN_VERSION:-0.12.0}

have() { command -v "$1" >/dev/null 2>&1; }

if ! have uv; then
  command -v curl >/dev/null || {
    echo 'curl is required to install uv (or place uv on PATH and rerun).' >&2
    exit 1
  }
  echo '== uv not found: installing the uv launcher (no sudo required) =='
  curl --proto '=https' --tlsv1.2 -LsSf https://astral.sh/uv/install.sh | sh
  # The official installer drops the binary in ~/.local/bin on Linux/macOS.
  export PATH="$HOME/.local/bin:$PATH"
fi

have uv || {
  echo 'uv is still not on PATH; add ~/.local/bin to PATH or export UV_BIN, then rerun.' >&2
  exit 1
}

UV_VERSION=$(uv --version | awk '{print $2}')
if [[ "$(printf '%s\n' "$UV_MIN_VERSION" "$UV_VERSION" | sort -V | head -n1)" != "$UV_MIN_VERSION" ]]; then
  echo "uv >= $UV_MIN_VERSION is required; found $UV_VERSION." >&2
  echo 'Upgrade uv (curl -LsSf https://astral.sh/uv/install.sh | sh) and rerun.' >&2
  exit 1
fi
echo "== uv $UV_VERSION =="

cd "$ROOT_DIR"
echo '== uv sync --locked (creates/refreshes .venv from uv.lock) =='
uv sync --locked

if have git && [[ -d .git ]]; then
  echo '== pre-commit hooks =='
  uv run pre-commit install
else
  echo '== no git repository found; skipping pre-commit hook install =='
fi

printf '\nInstallation complete. Validate with ./build.sh --qa (./build.sh --sec for security gates).\n'
