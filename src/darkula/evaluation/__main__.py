# SPDX-FileCopyrightText: 2026 Darkula contributors
# SPDX-License-Identifier: AGPL-3.0-only
"""Module entry point: ``python -m darkula.evaluation`` (PR 15)."""

from __future__ import annotations

import sys

from darkula.evaluation.cli import main

if __name__ == "__main__":  # pragma: no cover - module entry point
    sys.exit(main())
