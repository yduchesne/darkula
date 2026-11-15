# Manual public HTTPS acceptance protocol (PR 20, L20)

**Not a pytest-collected test.** Never run without explicit operator approval
for a specific host. Do not use random dark-web/third-party sites, do not use
credentials, and do not copy content into logs or tickets.

Record each case as `PASS`, `FAIL`, or `NOT_RUN_<reason>`. Do not report an
unrun case as passed.

## Setup

```bash
./scripts/darkula_https_egress.sh start
# Set crawler.real_https_enabled=true and the exact origin allowlist through
# local operator configuration/secure environment. Never commit it.
```

Use only an HTTPS endpoint you own/control or are explicitly authorized to
inspect. Prefer a harmless static page.

## L20-01 — approved public HTTPS success

```bash
uv run python scripts/darkula_https_smoke.py \
  --live --operator-approved \
  --origin 'https://<approved-host>:443' \
  --start-url 'https://<approved-host>/' \
  --path-prefix '/' --max-pages 2 --max-requests 10 --max-depth 1 \
  --timeout-seconds 30
```

Expect `status=completed`, at least one page, bounded requests, and
`cleanup_verified=true`. Confirm `podman ps -a`/`podman network ls` show no
`darkula-https-*` residuals. Evidence must be recorded without URL/content
bytes in logs.

## L20-02 — cross-host redirect

Requires an operator-owned redirect endpoint from the approved origin to a
**different** host. Expect a policy violation before the second host is
contacted, no partial successful pages, and zero access to the second host
(verify on the controlled fixture). If no controlled fixture exists, record
`NOT_RUN_OPERATOR_FIXTURE_MISSING`.

## L20-03 — same-origin redirect outside the granted path

Requires a controlled same-origin redirect to a path outside the prefix.
Expect denial before the outside-path navigation and no partial pages. Same
`NOT_RUN_OPERATOR_FIXTURE_MISSING` rule applies.

## L20-04 — invalid certificate

Requires a controlled endpoint with a self-signed/mismatched/expired
certificate. Expect a non-success (`workload_failed`) and no insecure
fallback. Never set `ignore_https_errors=True`, install a permissive CA, or
use `--ignore-certificate-errors`.

## L20-05 — revocation

Remove the origin from the allowlist (or set `real_https_enabled=false`) and
retry the same CLI invocation. Expect rejection before any Podman resource is
created. Historical results/status must remain unchanged.

## Cleanup

```bash
./scripts/darkula_https_egress.sh clean
```

If any L20 case leaks a `darkula-https-*` container or ephemeral network, stop
and report a security defect; do not hand-delete unverified resources.
