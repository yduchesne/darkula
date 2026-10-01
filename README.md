# Darkula

<p align="center">
  <img src="docs/img/DarkulaLogo-384x384.png" alt="Darkula logo">
</p>

Darkula is an agentic Dark Web monitoring platform. It uses specialized AI agents together with deterministic collection and extraction services to discover and evaluate useful sources, collect them repeatedly, and turn their content into structured intelligence. The platform is designed to look for signals such as network indicators, domains and URLs, online identities, threat actors and malware, organizations and people, leaked or compromised credentials and access, cryptocurrency addresses, geographic references, relationships, and changes in the activity or relevance of a source. Because Dark Web pages and files must be treated as potentially hostile, Darkula is designed so that browsing and dangerous content handling happen inside tightly restricted, disposable sandboxes; the rest of the platform works from constrained outputs rather than giving AI agents unrestricted access to the Dark Web.

See [Architecture](docs/ARCHITECTURE.md) for the authoritative architecture description.

> **Status:** Darkula is in development. The current foundation provides the Python project and quality gates, layered configuration, provider-neutral application contracts, deterministic test implementations, local/in-memory artifact storage, OpenTelemetry instrumentation, and centralized composition. Source persistence, production infrastructure, crawling, reconnaissance, extraction, source analysis, and end-to-end Fake World workflows are being added incrementally.

## How Darkula works

Darkula separates reasoning from collection. A **ReconAgent** examines newly discovered source candidates and recommends whether they appear useful and collectible. A coordinator retains control of lifecycle and collection decisions. Approved sources are collected according to explicit policies by deterministic services, while crawler execution is isolated behind a sandbox boundary. Collected material is normalized and passed through extraction components that identify useful facts and relationships. A **SourceAnalyst** then reasons over those facts, collection history, and earlier observations to assess how a source is changing and how useful it may be.

In simplified form:

```text
Source discovery
      |
      v
  ReconAgent
      |
      v
Source qualification
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

## Security model

Darkula assumes that everything collected from an external source is untrusted. A page can contain malicious scripts or files, attempt to exploit a browser or parser, consume excessive resources, or contain instructions intended to manipulate an AI model.

The architecture therefore separates hostile network access from the trusted application. Retrieval, rendering, and other dangerous processing are intended to run in restricted, disposable sandboxes with narrowly defined permissions and resource limits. Those sandboxes do not receive general application, database, message-broker, object-storage, or cloud credentials. Content that leaves the sandbox remains untrusted, even after normalization, and AI agents receive constrained data rather than unrestricted browsing capability.

See [Security](docs/SECURITY.md) for the detailed threat model and security architecture.

## Requirements

- Python 3.14;
- [`uv`](https://docs.astral.sh/uv/) for Python package and environment management.

Additional infrastructure requirements will be documented as production adapters and integration environments are introduced.

## Installation

```bash
./install.sh
```

The installer verifies the required Python version, installs `uv` when necessary, synchronizes the locked environment, and installs the repository pre-commit hooks.

## Validation

| Command | Gate |
| --- | --- |
| `./build.sh --qa` | Ruff formatting/linting, strict Mypy, unit tests, coverage >= 85% |
| `./build.sh --sec` | Bandit source scan and dependency audit |

Pre-commit provides fast local feedback:

```bash
uv run pre-commit run --all-files
```

Infrastructure integration testing is intentionally not part of the current foundation; it will be added as real infrastructure adapters arrive.

## Architecture at a glance

Darkula uses explicit boundaries between its application logic and infrastructure:

- **PostgreSQL** is intended to hold authoritative structured domain state.
- **DataStream** carries asynchronous commands and events through a Darkula-owned abstraction; Redpanda/Kafka is the intended initial production implementation.
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
- [v0.1 Roadmap](docs/ROADMAP_V01.md): Tracks the incremental implementation plan.

## License

Darkula is licensed under the GNU Affero General Public License v3.0. See [LICENSE](LICENSE).