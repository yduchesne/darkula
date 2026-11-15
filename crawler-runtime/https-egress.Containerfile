# SPDX-FileCopyrightText: 2026 Darkula contributors
# SPDX-License-Identifier: AGPL-3.0-only
# Darkula HTTPS egress gateway image (PR 20).
#
# Minimal purpose-built CONNECT gateway: pinned Python slim base, only the
# https_egress package and the shared pure policy module copied in. There is
# no Playwright, no Darkula application code, no database/DataStream/ObjectStore
# client, no secret resolver, no LLM and no shell entrypoint that accepts
# caller commands.
FROM docker.io/library/python:3.14-slim

COPY crawler-runtime/https_egress/ /app/https_egress/
COPY src/darkula/crawler/public_https_policy.py /app/public_https_policy.py

RUN useradd --create-home --uid 10002 --shell /usr/sbin/nologin egress \
    && chmod -R a+rX /app \
    && python3 -m compileall -q /app/https_egress /app/public_https_policy.py

ENV HOME=/tmp \
    XDG_RUNTIME_DIR=/tmp \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app
USER 10002

# The sandbox adapter always passes `python3 -m https_egress` explicitly.
CMD ["python3", "-m", "https_egress"]
