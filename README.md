# Darkula

<p align="center">
  <img src="docs/img/DarkulaLogo-384x384.png" alt="Darkula logo">
</p>

Darkula is an agentic Dark Web monitoring platform. It uses specialized AI agents together with deterministic collection and extraction services to discover and evaluate useful sources, collect them repeatedly, and turn their content into structured intelligence. The platform is designed to look for signals such as network indicators, domains and URLs, online identities, threat actors and malware, organizations and people, leaked or compromised credentials and access, cryptocurrency addresses, geographic references, relationships, and changes in the activity or relevance of a source. Because Dark Web pages and files must be treated as potentially hostile, Darkula is designed so that browsing and dangerous content handling happen inside tightly restricted, disposable sandboxes; the rest of the platform works from constrained outputs rather than giving AI agents unrestricted access to the Dark Web.

See [Architecture](docs/ARCHITECTURE.md) for the authoritative architecture description.

> **Status (November 2026):** The **v0.1 roadmap (PRs 1–16) is complete**, with verified local integration of source persistence, secure sandboxed crawling, reconnaissance, collection scheduling, normalization, extraction, source analysis, evaluation, Redpanda messaging, and OTEL against the **synthetic Fake World**. This is **not yet a live Tor/Internet collector or a production-ready deployment**. [v0.2 planning](docs/ROADMAP_V02.md) covers structured and recursive source discovery, controlled real-world HTTPS/Tor collection, MonitoringProfiles, and programmatic access.

## How Darkula works

Darkula separates reasoning from collection. In **v0.1, source candidates are supplied externally**; autonomous discovery is planned for v0.2. A **ReconAgent** examines candidate sources and recommends whether they appear useful and collectible. A coordinator retains control of lifecycle and collection decisions. Approved sources are collected according to explicit policies by deterministic services, while crawler execution is isolated behind a sandbox boundary. Collected material is normalized and passed through extraction components that identify useful facts and relationships. A **SourceAnalyst** then reasons over those facts, collection history, and earlier observations to assess how a source is changing and how useful it may be.

In simplified form:

```text
Externally supplied SourceCandidate (v0.1)
      |
      v
  ReconCoordinator + ReconAgent
      |
      v
Source qualification -> explicit promotion
      |
      v
Secure, sandboxed collection
      |
      v
Normalization
      |
      v
Content extraction
      |
      v
 SourceAnalyst
      |
      v
Source assessment
```

The agents do not autonomously roam the Dark Web. Collection is bounded by explicit policies and budgets, and the coordinator—not an agent—owns the final decisions about what work is authorized.

For v0.2, sufficiently complete, non-conflicting candidate records from explicitly approved structured registries may be **qualified deterministically**, without invoking ReconAgent. Ambiguous or untrusted records instead require authorized agent reconnaissance or remain pending. Both routes retain evidence and decision provenance, and neither authorizes collection or promotes a source automatically.

For authenticated external sources in the planned v0.2 release, an operator must **create/obtain accounts and provision credentials manually**. Darkula will track authentication readiness separately from source qualification: sources requiring credentials remain visible as `CREDENTIALS_PENDING` rather than being rejected, and protected collection requires independently validated access before it can resume. Darkula itself has no user accounts or multitenancy. See [v0.2 Roadmap](docs/ROADMAP_V02.md).

## Security model

Darkula assumes that everything collected from an external source is untrusted. A page can contain malicious scripts or files, attempt to exploit a browser or parser, consume excessive resources, or contain instructions intended to manipulate an AI model.

The architecture therefore separates hostile network access from the trusted application. Retrieval, rendering, and other dangerous processing are intended to run in restricted, disposable sandboxes with narrowly defined permissions and resource limits. Those sandboxes do not receive general application, database, message-broker, object-storage, or cloud credentials. Content that leaves the sandbox remains untrusted, even after normalization, and AI agents receive constrained data rather than unrestricted browsing capability.

See [Security](docs/SECURITY.md) for the detailed threat model and security architecture.

## Requirements

- Python 3.14;
- [`uv`](https://docs.astral.sh/uv/) for Python package and environment management;
- [Podman](https://podman.io/) for the local integration stack (`./build.sh --intg`);
  the full Podman-backed toolchain is normally provisioned by the installer.

Additional infrastructure requirements will be documented as production adapters and integration environments are introduced.

## Installation

```bash
./install.sh                     # uv + Podman + locked environment + hooks
./install.sh --mock              # print the plan; execute nothing
./install.sh --no-podman         # Python toolchain only
```

The installer is idempotent: it ensures `uv` is available (installing it without sudo when missing), ensures `podman` is available (installing it through the platform package manager, which requires root or sudo), verifies the minimum `podman` version, synchronizes the locked environment, and installs the repository pre-commit hooks. `--mock` mandates dry-run behavior: every mutating command is printed rather than executed, while read-only version/command probes still run. `--no-podman` keeps the setup Python-only on hosts that provide Podman differently or only run the unit/QA gates.

## Validation

| Command | Gate |
| --- | --- |
| `./build.sh --qa` | Ruff formatting/linting, strict Mypy, unit tests, coverage >= 85% |
| `./build.sh --sec` | Bandit source scan and dependency audit |

Pre-commit provides fast local feedback:

```bash
uv run pre-commit run --all-files
```

Real local integration testing is provided via `./build.sh --intg` on a provisioned Podman-capable host. The canonical end-to-end integration uses the synthetic Fake World rather than live Internet or Tor endpoints. Model-backed evaluations are opt-in via `./build.sh --eval`.

## Architecture at a glance

Darkula uses explicit boundaries between its application logic and infrastructure:

- **PostgreSQL** holds authoritative structured domain state through versioned stored functions and repository/UnitOfWork contracts.
- **DataStream** carries asynchronous commands and events through a Darkula-owned abstraction, with the Redpanda adapter and transactional outbox implemented.
- **ObjectStore** holds large or raw artifacts such as pages, screenshots, PDFs, images, and attachments.
- **LlmClient** isolates agents and semantic extraction from specific model frameworks/providers.
- **OpenTelemetry** provides operational tracing and metrics.
- **Agent observability** is kept behind a separate provider-neutral interface.

These boundaries allow deterministic test implementations to exercise application behavior without contacting real Dark Web sources or external infrastructure.

## Authoritative documentation

- [Architecture](docs/ARCHITECTURE.md): Describes Darkula's architecture.
- [Domain Model](docs/DOMAIN_MODEL.md): Persistence/domain model.
- [Configuration](docs/CONFIGURATION.md): Darkula's configuration framework, based on Pydantic Settings.
- [Observability](docs/OBSERVABILITY.md): Describes the observability stack and provides guidance.
- [Testing](docs/TESTING.md): Documents testing guidelines.
- [Fake World](docs/FAKE_WORLD.md): Describes Darkula's synthetic-data ("Fake World") approach used for testing and experimentation.
- [Security](docs/SECURITY.md): Describes the threat model and security boundaries.
- [Evaluations](docs/EVALUATIONS.md): Describes evaluation strategy for model-backed behavior.
- [v0.1 Roadmap](docs/ROADMAP_V01.md): Records the completed PR 1–16 scope.
- [v0.2 Roadmap](docs/ROADMAP_V02.md): Proposed source discovery, real-world collection, and MonitoringProfile work (not yet delivered).
- [Production Readiness](docs/PRODUCTION_READINESS.md): Verified local capabilities and gaps before production deployment.

## License

Darkula is licensed under the GNU Affero General Public License v3.0. See [LICENSE](LICENSE).
