# SPDX-License-Identifier: AGPL-3.0-only
"""Layered configuration resolution for Darkula (PR 3).

Resolves the exact documented precedence:

.. code-block:: text

    defaults < base < selected profile < local override < non-empty environment

- code defaults are the ``Settings`` field defaults;
- ``base`` is ``config/base.toml`` (required);
- the selected profile is ``config/profiles/<profile>.toml`` (required;
  selected by non-empty ``DARKULA_CONFIG_PROFILE``, default ``development``);
- the local/operator override is ``config/local.toml`` (optional, git-ignored);
- the environment is applied **last** and only non-empty ``DARKULA_*``
  variables have any effect (``env_ignore_empty = True``).

The environment-last guarantee is implemented by reordering the Pydantic
Settings sources: the environment source is consulted **before** the
init/file source, so a non-empty environment variable always beats a value
resolved from lower layers and an unset/empty variable never erases one.
The environment is always the live process environment (never mutated);
tests manage process state through pytest's ``monkeypatch``.

Failures are bounded and fail closed:

- missing required base -> :class:`ConfigError`;
- missing selected profile -> :class:`ConfigError`;
- malformed profile name -> :class:`ValueError`;
- malformed TOML -> :class:`ConfigError` (no file content echoed);
- unknown keys / invalid types -> Pydantic ``ValidationError``.

Diagnostics never dump the environment and redact secret-like configuration
values (see :func:`redact_config` and :func:`diagnostic_summary`).
"""

from __future__ import annotations

import json
import os
import re
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from pydantic_settings import (
    BaseSettings,
    PydanticBaseSettingsSource,
)

from darkula.config.settings import (
    CONFIG_PROFILE_ENV_VAR,
    Settings,
)

#: Shipped default profile (deterministic; never inferred from machine/CI).
DEFAULT_PROFILE = "development"

#: Bounded profile-name grammar: lowercase letter, then lowercase alphanumerics,
#: underscores, or hyphens.
PROFILE_PATTERN = re.compile(r"^[a-z][a-z0-9_-]*$")

#: Default search path for the shipped ``config/`` tree (repo-local layout).
_DEFAULT_CONFIG_DIR = Path(__file__).resolve().parents[3] / "config"

#: Secret-like key/value markers redacted in safe diagnostics.
SECRET_MARKERS: tuple[str, ...] = (
    "secret",
    "password",
    "passwd",
    "token",
    "api_key",
    "apikey",
    "private_key",
    "credential",
    "auth",
    "cookie",
    "session",
)

_LOGGER_SAFE_REDACTION = "<redacted>"


class ConfigError(RuntimeError):
    """A bounded configuration failure (never echoes file contents)."""


@dataclass(frozen=True, slots=True)
class ConfigLayers:
    """The raw resolved layers for one configuration resolution.

    ``effective`` is the deep-merged ``base < profile < local`` mapping
    before environment application and before Pydantic validation. Useful
    for safe diagnostics and for asserting merge semantics.
    """

    config_dir: Path
    profile: str
    base: Mapping[str, object] = field(default_factory=dict)
    profile_config: Mapping[str, object] = field(default_factory=dict)
    local: Mapping[str, object] = field(default_factory=dict)
    effective: Mapping[str, object] = field(default_factory=dict)


def _read_toml(path: Path) -> dict[str, object]:
    """Read and parse one TOML file, translating failures to ConfigError."""
    try:
        with path.open("rb") as handle:
            return tomllib.load(handle)
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"malformed TOML in {path}") from exc
    except OSError as exc:
        raise ConfigError(f"cannot read configuration file {path}") from exc


def _selected_profile() -> str:
    """Return the selected profile name from the live environment.

    An unset or empty ``DARKULA_CONFIG_PROFILE`` selects the deterministic
    default profile; a malformed explicit name is rejected.
    """
    raw = os.environ.get(CONFIG_PROFILE_ENV_VAR, "").strip()
    if not raw:
        return DEFAULT_PROFILE
    if PROFILE_PATTERN.fullmatch(raw) is None:
        raise ValueError(
            "DARKULA_CONFIG_PROFILE must match the profile grammar ^[a-z][a-z0-9_-]*$"
        )
    return raw


def deep_merge(
    base: Mapping[str, object],
    override: Mapping[str, object],
) -> dict[str, object]:
    """Deep-merge nested mappings; scalars/lists/tuples replace wholesale.

    Nested ``Mapping`` values are merged recursively; every other value
    (including lists and tuples) replaces the lower-layer value entirely.
    Neither input mapping is mutated.
    """
    result: dict[str, object] = dict(base)
    for key, value in override.items():
        existing = result.get(key)
        if isinstance(value, Mapping) and isinstance(existing, Mapping):
            result[key] = deep_merge(existing, value)
        else:
            result[key] = value
    return result


def resolve_layers(*, config_dir: Path | None = None) -> ConfigLayers:
    """Resolve and deep-merge the file layers for the selected profile.

    Reads only files; performs no Pydantic validation. Profile selection
    reads the live process environment.
    """
    base_dir = _DEFAULT_CONFIG_DIR if config_dir is None else config_dir
    profile = _selected_profile()

    base_path = base_dir / "base.toml"
    if not base_path.is_file():
        raise ConfigError(f"required base configuration not found: {base_path}")
    base = _read_toml(base_path)

    profile_path = base_dir / "profiles" / f"{profile}.toml"
    if not profile_path.is_file():
        raise ConfigError(f"selected configuration profile not found: {profile_path}")
    profile_config = _read_toml(profile_path)

    local_path = base_dir / "local.toml"
    local: dict[str, object] = {}
    if local_path.is_file():
        local = _read_toml(local_path)

    merged = deep_merge(base, profile_config)
    merged = deep_merge(merged, local)
    return ConfigLayers(
        config_dir=base_dir,
        profile=profile,
        base=base,
        profile_config=profile_config,
        local=local,
        effective=merged,
    )


class _EnvFirstSettings(Settings):
    """Settings whose sources are ordered so the environment wins last.

    The default pydantic-settings order (init above env) would let lower
    file layers outrank the environment; PR 3's documented contract requires
    the environment as the ultimate override. This subclass reverses the
    first two sources: non-empty environment first, merged file layers
    second, and code defaults last. Dotenv/secrets-file sources stay unused.
    """

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        """Return the sources ordered env -> init -> dotenv -> secrets."""
        return (
            env_settings,
            init_settings,
            dotenv_settings,
            file_secret_settings,
        )


def load_settings(*, config_dir: Path | None = None) -> Settings:
    """Resolve the full layered configuration into validated Settings.

    Reads the live process environment (never mutates it). The returned
    :class:`Settings` reflects the documented precedence with non-empty
    environment variables as the ultimate override.
    """
    layers = resolve_layers(config_dir=config_dir)
    init_values: dict[str, Any] = dict(layers.effective)
    # Reflect the selected profile name in the resolved settings. The
    # environment source (higher priority) overrides this when
    # DARKULA_CONFIG_PROFILE is set.
    init_values["config_profile"] = layers.profile
    return _EnvFirstSettings(**init_values)


def redact_config(value: object) -> object:
    """Return a recursively copied value with secret-like values redacted.

    Mapping keys whose lowercased name contains a secret marker (secret,
    password, token, api-key, credential, auth, cookie, session, ...) have
    their whole value replaced with ``<redacted>``; nested mappings and
    lists are recursed into; every other value is preserved.
    """
    if isinstance(value, Mapping):
        return {
            str(key): (
                _LOGGER_SAFE_REDACTION
                if _is_secret_like(str(key))
                else redact_config(item)
            )
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [redact_config(item) for item in value]
    return value


def _is_secret_like(name: str) -> bool:
    """Return whether a lowercased name contains a secret marker."""
    lowered = name.lower()
    return any(marker in lowered for marker in SECRET_MARKERS)


def diagnostic_summary(layers: ConfigLayers) -> str:
    """Return a deterministic, redacted, single-line diagnostic summary.

    Contains the selected profile, the loaded layer set, and the redacted
    effective configuration. Never contains environment values, secrets, or
    secret-like keys.
    """
    loaded = ["base", f"profile:{layers.profile}"]
    if layers.local:
        loaded.append("local")
    rendered = json.dumps(redact_config(layers.effective), sort_keys=True, default=repr)
    return (
        f"config profile={layers.profile} layers={','.join(loaded)} "
        f"effective={rendered}"
    )


__all__ = [
    "DEFAULT_PROFILE",
    "PROFILE_PATTERN",
    "SECRET_MARKERS",
    "ConfigError",
    "ConfigLayers",
    "deep_merge",
    "diagnostic_summary",
    "load_settings",
    "redact_config",
    "resolve_layers",
]
