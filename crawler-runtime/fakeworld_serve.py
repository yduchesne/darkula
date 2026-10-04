# SPDX-FileCopyrightText: 2026 Darkula contributors
# SPDX-License-Identifier: AGPL-3.0-only
"""Fake World HTTP serve entry point (PR 7).

Runs the canonical BlackGate scenario adapter on the Darkula-owned internal
network service port 8080 (no host publishing; container-to-container only,
per the prefix-3 host-port rules). Quiet by design: hostile request lines
never enter logs.
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, "/app")

from darkula.testing.fake_world.http_adapter import (
    FakeWorldHttpServer,
    FakeWorldHttpService,
)

HOST = "0.0.0.0"
PORT = int(os.environ.get("DARKULA_FAKEWORLD_PORT", "8080"))


def main() -> int:
    service = FakeWorldHttpService()
    server = FakeWorldHttpServer(HOST, PORT, service)
    print(f"darkula-fakeworld: canonical BlackGate HTTP on {HOST}:{PORT}", flush=True)
    server.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
