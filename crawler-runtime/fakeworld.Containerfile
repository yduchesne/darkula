# SPDX-FileCopyrightText: 2026 Darkula contributors
# SPDX-License-Identifier: AGPL-3.0-only
# Darkula Fake World HTTP test-infra image (PR 7).
#
# Runs the canonical BlackGate scenario behind a real HTTP server on the
# Darkula-owned internal network. This is *test-side trusted infrastructure*,
# never the crawler sandbox: the crawler container never contains this code.
#
# The fake_world package is pure stdlib (verified), so the image needs no
# pip installs and no third-party packages.
FROM docker.io/library/python:3.14-slim

# Minimal package slice required by the adapter/registry/renderer.
COPY src/darkula/__init__.py /app/darkula/__init__.py
COPY src/darkula/testing/__init__.py /app/darkula/testing/__init__.py
COPY src/darkula/testing/fake_world/ /app/darkula/testing/fake_world/
COPY crawler-runtime/fakeworld_serve.py /app/fakeworld_serve.py

RUN useradd --create-home --uid 10001 --shell /usr/sbin/nologin fakeworld \
    && chmod -R a+rX /app

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PYTHONPATH=/app

USER 10001
EXPOSE 8080
CMD ["python3", "/app/fakeworld_serve.py"]
