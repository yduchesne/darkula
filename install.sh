#!/usr/bin/env bash
# SPDX-License-Identifier: AGPL-3.0-only
# Bootstrap Darkula's Python toolchain and local integration prerequisites.
#
# Idempotent: safe to re-run at any time.
#
#   1. ensures the `uv` launcher is available (installs it without sudo when
#      missing, via the official astral.sh installer);
#   2. ensures `podman` is available (installs it through the platform
#      package manager; Podman is required by `./build.sh --intg` and by the
#      Darkula-owned PostgreSQL/Redpanda/observability/crawler tooling);
#   3. drives the initial dependency download and project setup with
#      `uv sync --locked` (creates/refreshes `.venv` from the committed
#      `uv.lock` — never silently upgrades outside the lock);
#   4. installs the pre-commit git hooks from the locked environment.
#
# Usage: ./install.sh [--mock] [--no-podman] [--help]
#
#   --mock       Mandate mock mode: every mutating command is printed
#                instead of executed. Read-only probes (command discovery and
#                version checks) still run. Nothing is installed, synchronized,
#                hooked, or written. Use it to preview the plan (for example
#                before granting sudo) or to exercise the installer offline.
#   --no-podman  Python-toolchain-only setup: skip Podman installation. Useful
#                on hosts that already provide Podman outside the package
#                manager or that only run the unit/QA gates.
#   --help       Print this usage and exit.
set -Eeuo pipefail

ROOT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
#: Minimum `uv` version able to consume the committed lockfile schema.
UV_MIN_VERSION=${UV_MIN_VERSION:-0.12.0}
#: Minimum Podman feature level required by the Darkula scripts
#: (`podman container exists`, volume/network inspection, labels).
PODMAN_MIN_VERSION=${PODMAN_MIN_VERSION:-4.0.0}

MOCK=0
INSTALL_PODMAN=1

usage() {
  cat <<'USAGE'
Usage: ./install.sh [--mock] [--no-podman] [--help]

  --mock       Mandate mock mode: every mutating command is printed instead
               of executed. Read-only probes (command discovery and version
               checks) still run. Nothing is installed, synchronized, hooked,
               or written.
  --no-podman  Python-toolchain-only setup: skip Podman installation.
  --help       Print this usage and exit.
USAGE
}

die() {
  echo "error: $*" >&2
  exit 1
}

have() { command -v "$1" >/dev/null 2>&1; }

# mutate <command...>
#   Run one mutating command, or only print it when mock mode is mandated.
mutate() {
  if ((MOCK)); then
    printf '  [mock] %s\n' "$*"
    return 0
  fi
  "$@"
}

# version_ge <candidate> <minimum>
version_ge() {
  [[ "$(printf '%s\n%s\n' "$1" "$2" | sort -V | head -n1)" == "$2" ]]
}

# ---------------------------------------------------------------------------
# Command-line options
# ---------------------------------------------------------------------------
while (($#)); do
  case "$1" in
    --mock | --dry-run) MOCK=1 ;;
    --no-podman) INSTALL_PODMAN=0 ;;
    -h | --help)
      usage
      exit 0
      ;;
    *)
      echo "unknown option: $1" >&2
      usage >&2
      exit 2
      ;;
  esac
  shift
done

if ((MOCK)); then
  echo '== mock mode mandated: mutating commands are printed, never executed =='
fi

# ---------------------------------------------------------------------------
# uv
# ---------------------------------------------------------------------------
install_uv_launcher() {
  curl --proto '=https' --tlsv1.2 -LsSf https://astral.sh/uv/install.sh | sh
}

if have uv; then
  UV_VERSION=$(uv --version | awk '{print $2}')
  if ! version_ge "$UV_VERSION" "$UV_MIN_VERSION"; then
    echo "uv >= $UV_MIN_VERSION is required; found $UV_VERSION." >&2
    echo 'Upgrade uv (curl -LsSf https://astral.sh/uv/install.sh | sh) and rerun.' >&2
    exit 1
  fi
  echo "== uv $UV_VERSION =="
elif ((MOCK)); then
  echo '== uv not found (mock mode: installation planned only) =='
  echo '  [mock] curl --proto =https --tlsv1.2 -LsSf https://astral.sh/uv/install.sh | sh'
  export PATH="$HOME/.local/bin:$PATH"
  echo "== uv $UV_MIN_VERSION or newer expected after installation (mock mode) =="
else
  have curl || die 'curl is required to install uv (or place uv on PATH and rerun)'
  echo '== uv not found: installing the uv launcher (no sudo required) =='
  install_uv_launcher
  # The official installer drops the binary in ~/.local/bin on Linux/macOS.
  export PATH="$HOME/.local/bin:$PATH"
  hash -r
  have uv || die 'uv is still not on PATH; add ~/.local/bin to PATH or export UV_BIN, then rerun'
  UV_VERSION=$(uv --version | awk '{print $2}')
  if ! version_ge "$UV_VERSION" "$UV_MIN_VERSION"; then
    die "uv >= $UV_MIN_VERSION is required; found $UV_VERSION"
  fi
  echo "== uv $UV_VERSION =="
fi

# ---------------------------------------------------------------------------
# podman
# ---------------------------------------------------------------------------
install_podman_packages() {
  # $@ holds the privilege prefix (empty for root, (sudo) otherwise).
  local os
  os=$(uname -s)
  case "$os" in
    Darwin)
      have brew || die 'Homebrew is required to install Podman on macOS (https://brew.sh)'
      mutate "$@" brew install podman
      echo '   note: run "podman machine init" and "podman machine start" before ./build.sh --intg'
      ;;
    Linux)
      if have apt-get; then
        mutate "$@" apt-get update
        mutate "$@" apt-get install -y podman
      elif have dnf; then
        mutate "$@" dnf install -y podman
      elif have yum; then
        mutate "$@" yum install -y podman
      elif have pacman; then
        mutate "$@" pacman -Sy --noconfirm podman
      elif have zypper; then
        mutate "$@" zypper --non-interactive install podman
      elif have apk; then
        mutate "$@" apk add --no-cache podman
      else
        die 'no supported package manager found to install Podman; install Podman manually and rerun'
      fi
      ;;
    *)
      die "unsupported platform '$os' for automatic Podman installation; install Podman manually and rerun"
      ;;
  esac
}

verify_podman() {
  local version
  version=$(podman --version | awk '{print $3}')
  if ! version_ge "$version" "$PODMAN_MIN_VERSION"; then
    die "podman >= $PODMAN_MIN_VERSION is required; found $version"
  fi
  echo "== podman $version =="
}

ensure_podman() {
  if have podman; then
    verify_podman
    return 0
  fi
  echo "== podman not found: installing (minimum $PODMAN_MIN_VERSION) =="
  SUDO=()
  if [[ "${EUID}" -ne 0 ]]; then
    if have sudo; then
      SUDO=(sudo)
    elif ((MOCK)); then
      SUDO=(sudo) # planned only; never executed in mock mode
      echo '  [mock] sudo is not available; the real run would require root'
    else
      die 'Podman installation requires root or sudo; install Podman manually and rerun'
    fi
  fi
  install_podman_packages "${SUDO[@]+"${SUDO[@]}"}"
  if ((MOCK)); then
    echo '  [mock] podman verification skipped (binary remains absent in mock mode)'
    return 0
  fi
  hash -r
  have podman || die 'Podman installation did not provide a podman binary on PATH; install Podman manually and rerun'
  verify_podman
  echo '   note: verify rootless operation with "podman info" before ./build.sh --intg'
}

if ((INSTALL_PODMAN)); then
  ensure_podman
else
  echo '== podman installation skipped (--no-podman) =='
fi

# ---------------------------------------------------------------------------
# Python environment
# ---------------------------------------------------------------------------
cd "$ROOT_DIR"
if have uv; then
  echo '== uv sync --locked (creates/refreshes .venv from uv.lock) =='
  mutate uv sync --locked
else
  echo '  [mock] uv sync --locked (skipped: uv unavailable in mock mode)'
fi

if ! [[ -d .git ]] || ! have git; then
  echo '== no git repository found; skipping pre-commit hook install =='
else
  echo '== pre-commit hooks =='
  mutate uv run pre-commit install
fi

if ((MOCK)); then
  printf '\nMock mode complete: no installation, synchronization, or hook changes were made.\n'
  printf 'Re-run without --mock to apply the plan. Validate with ./build.sh --qa.\n'
else
  printf '\nInstallation complete. Validate with ./build.sh --qa (./build.sh --sec for security gates).\n'
fi
