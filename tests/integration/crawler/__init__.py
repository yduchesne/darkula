# SPDX-FileCopyrightText: 2026 Darkula contributors
# SPDX-License-Identifier: AGPL-3.0-only
"""PR 7 real-Podman crawler integration suite (marked ``integration``).

These tests hit the real Playwright + Chromium runtime against the Fake World
HTTP service inside the Darkula-owned internal network; they are provisioned
and cleaned up by ``./build.sh --intg``.
"""
