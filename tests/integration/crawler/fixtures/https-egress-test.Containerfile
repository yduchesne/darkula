# SPDX-License-Identifier: AGPL-3.0-only
# TEST-ONLY fail-closed HTTPS egress gateway image (PR 20 integration).
# Never used by production composition or operator scripts.
FROM docker.io/library/python:3.14-slim

COPY crawler-runtime/https_egress/ /app/https_egress/
COPY src/darkula/crawler/public_https_policy.py /app/public_https_policy.py
COPY tests/integration/crawler/fixtures/test_gateway_main.py /app/https_egress/__main__.py

RUN useradd --create-home --uid 10002 --shell /usr/sbin/nologin egress \
    && chmod -R a+rX /app \
    && python3 -m compileall -q /app/https_egress /app/public_https_policy.py

ENV HOME=/tmp \
    XDG_RUNTIME_DIR=/tmp \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app
USER 10002
CMD ["python3", "-m", "https_egress"]
