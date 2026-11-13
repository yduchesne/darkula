# Controlled public HTTPS egress — operations runbook (PR 20)

This runbook covers the **explicitly opt-in** public HTTPS crawler path. It is
never enabled by default, never used by ordinary CI, and never a general
Internet-access feature. If you are unsure whether you have legal/operational
authorization to inspect a host, do not run it.

## Preconditions

1. Rootless Podman is available and working.
2. You have explicit authorization to inspect the chosen public host.
3. `crawler.real_https_enabled=true` and the host's exact
   `https://<fqdn>:443` origin is in `crawler.real_https_allowed_origins`
   (operator config or secure environment; never commit an open allowlist).
4. The chosen URL is under the granted path prefix. The smoke CLI defaults to
   `/`; prefer a narrower prefix.
5. No credentials are used. PR 20 does not support authenticated sessions.

## Provision the outer egress resources

```bash
./scripts/darkula_https_egress.sh status
./scripts/darkula_https_egress.sh start
```

`start` verifies rootless Podman, creates (or verifies) the exact
Darkula-owned, **non-internal** `darkula-live-egress` network with
`darkula.owned=true`, and builds the pinned local gateway image
`localhost/darkula-https-egress:1` from
`crawler-runtime/https-egress.Containerfile` if absent. It starts no
listener/crawler/proxy. It refuses foreign/mislabeled resources and never
relabels, adopts, or deletes them. It is never called by `./build.sh` or the
Fake World crawler script.

## Run one operator-approved smoke

```bash
uv run python scripts/darkula_https_smoke.py \
  --live --operator-approved \
  --origin 'https://approved-host.example.org:443' \
  --start-url 'https://approved-host.example.org/' \
  --path-prefix '/' \
  --max-pages 2 --max-requests 10 --max-depth 1 --timeout-seconds 30
```

`approved-host.example.org` is a **placeholder**. Replace it with a real
host you own or are authorized to inspect. Both `--live` and
`--operator-approved` are mandatory before any Podman initialization; missing
either exits `2`.

The CLI prints only counters: `status`, `pages`, `requests`, `duration`,
`reason_code`, `cleanup_verified`. It never prints URLs, hostnames, content,
headers, cookies, DNS answers or credentials, and it never retries.

A result other than `completed` exits non-zero. Common mappings:

| Result | Meaning |
|---|---|
| `workload_failed` / `timed_out` | Target unreachable, TLS/verification failure, proxy refusal, or no authorized page |
| `policy_violation` | Redirect/subresource/method/WebSocket/request-budget violation; no partial pages |
| `invalid_output` | Runtime output failed version/identity/grant validation |
| `sandbox_unavailable` | Missing/foreign egress network, image, rootless topology, or Podman failure |
| `terminated` / `resource_limited` | Cancellation/resource ceiling |

## Clean up

```bash
./scripts/darkula_https_egress.sh clean
```

`clean` refuses to remove the network while any container is still attached
and never touches unowned resources. Per-crawl cleanup already removes the
crawler, gateway and ephemeral internal network on success, timeout,
cancellation and failure; only the shared outer network persists between
authorized runs.

## Security constraints and limitations

- The crawler container joins **only** the per-execution internal network,
  with `--dns none` and `--http-proxy=false`; the gateway is the only
  multi-homed member. Never use `--network=host`, a privileged container, a
  broad HTTP proxy, or a shared Internet bridge to “make it work”.
- The gateway accepts exactly one approved FQDN on TCP 443, resolves it
  afresh, vets every answer, and dials only the selected numeric IPv4. It
  publishes no host port and cannot forward arbitrary destinations.
- Cross-origin redirects/subresources, CDN/static assets outside the prefix,
  WebSockets, POST/login, downloads and IPv6-only endpoints are denied. PR 20
  intentionally fails closed rather than widening the grant.
- Tor/onion, authenticated sessions, recursive link discovery, Source
  promotion and collection scheduling are **not** part of PR 20.
- CI (`--qa`/`--sec`/`--intg`) performs zero public requests. A successful
  real-world crawl is only evidence after an operator runs the L20 protocol in
  [tests/manual/test_public_https_smoke.md](../tests/manual/test_public_https_smoke.md).

## Rollback / disable

Remove the origin from `crawler.real_https_allowed_origins` or set
`crawler.real_https_enabled=false`; new live requests are then rejected before
any Podman resource is created. Historical results and existing data are
unchanged. Run `./scripts/darkula_https_egress.sh clean` to remove the shared
outer network/image readiness when no execution is in flight.
