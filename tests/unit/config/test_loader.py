# SPDX-License-Identifier: AGPL-3.0-only
"""Unit tests for the layered configuration loader (C-* test matrix).

Environment tests manage the live process environment through pytest's
``monkeypatch`` (restored automatically at teardown) and an autouse fixture
that clears every ambient ``DARKULA_*`` variable first, keeping the suite
order-independent.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from pydantic import ValidationError
from pytest import MonkeyPatch

from darkula.config.loader import (
    DEFAULT_PROFILE,
    PROFILE_PATTERN,
    ConfigError,
    deep_merge,
    diagnostic_summary,
    load_settings,
    redact_config,
    resolve_layers,
)
from darkula.config.settings import DataStreamDriver, ObjectStoreDriver, Settings

_BASE = """\
[database]
host = "base-host"
port = 6432

[object_store]
driver = "local"
"""

_DEV = """\
[database]
port = 6543

[telemetry]
service_name = "scaffold-development"
"""

_LOCAL = """\
[database]
host = "local-host"
"""


@pytest.fixture(autouse=True)
def _clean_darkula_env(monkeypatch: MonkeyPatch) -> None:
    """Remove every ambient DARKULA_* variable before each test."""
    for key in [key for key in os.environ if key.startswith("DARKULA_")]:
        monkeypatch.delenv(key, raising=False)


def _scaffold(
    tmp_path: Path,
    *,
    base: str = _BASE,
    profiles: dict[str, str] | None = None,
    local: str | None = None,
) -> Path:
    """Build an isolated config tree and return its directory."""
    config_dir = tmp_path / "config"
    (config_dir / "profiles").mkdir(parents=True)
    (config_dir / "base.toml").write_text(base, encoding="utf-8")
    for name, content in (profiles or {}).items():
        (config_dir / "profiles" / f"{name}.toml").write_text(content, encoding="utf-8")
    if local is not None:
        (config_dir / "local.toml").write_text(local, encoding="utf-8")
    return config_dir


class TestPrecedence:
    """C01-C07: exact precedence defaults < base < profile < local < env."""

    def test_c01_no_env_lower_layers_survive(self, tmp_path: Path) -> None:
        config_dir = _scaffold(tmp_path, profiles={"development": _DEV})
        settings = load_settings(config_dir=config_dir)
        assert settings.config_profile == DEFAULT_PROFILE
        assert settings.database.host == "base-host"
        # base port overridden by the development profile port.
        assert settings.database.port == 6543

    def test_c02_empty_env_has_no_effect(
        self, tmp_path: Path, monkeypatch: MonkeyPatch
    ) -> None:
        config_dir = _scaffold(tmp_path, profiles={"development": _DEV})
        monkeypatch.setenv("DARKULA_DATABASE__HOST", "")
        monkeypatch.setenv("DARKULA_OBJECT_STORE__DRIVER", "")
        settings = load_settings(config_dir=config_dir)
        assert settings.database.host == "base-host"
        assert settings.object_store.driver is ObjectStoreDriver.LOCAL

    def test_c03_non_empty_env_wins(
        self, tmp_path: Path, monkeypatch: MonkeyPatch
    ) -> None:
        config_dir = _scaffold(tmp_path, profiles={"development": _DEV})
        monkeypatch.setenv("DARKULA_DATABASE__HOST", "env-host")
        settings = load_settings(config_dir=config_dir)
        assert settings.database.host == "env-host"

    def test_c04_base_beats_code_defaults(self, tmp_path: Path) -> None:
        # The profile must not touch `port` so only base's value shows through.
        neutral_profile = """\
[telemetry]
service_name = "scaffold-development"
"""
        config_dir = _scaffold(tmp_path, profiles={"development": neutral_profile})
        settings = load_settings(config_dir=config_dir)
        # Python default is 5432; base.toml sets 6432 -> base wins.
        assert settings.database.port == 6432

    def test_c05_profile_beats_base(self, tmp_path: Path) -> None:
        profile = """\
[database]
host = "dev-host"
"""
        config_dir = _scaffold(tmp_path, profiles={"development": profile})
        settings = load_settings(config_dir=config_dir)
        assert settings.database.host == "dev-host"

    def test_c06_local_beats_profile(self, tmp_path: Path) -> None:
        config_dir = _scaffold(tmp_path, profiles={"development": _DEV}, local=_LOCAL)
        settings = load_settings(config_dir=config_dir)
        assert settings.database.host == "local-host"

    def test_c07_env_beats_local(
        self, tmp_path: Path, monkeypatch: MonkeyPatch
    ) -> None:
        config_dir = _scaffold(tmp_path, profiles={"development": _DEV}, local=_LOCAL)
        monkeypatch.setenv("DARKULA_DATABASE__HOST", "env-host")
        settings = load_settings(config_dir=config_dir)
        assert settings.database.host == "env-host"

    def test_c08_nested_partial_profile_preserves_unrelated_values(
        self, tmp_path: Path
    ) -> None:
        # The profile overrides only `port`; `host` from base must survive.
        config_dir = _scaffold(tmp_path, profiles={"development": _DEV})
        settings = load_settings(config_dir=config_dir)
        assert settings.database.host == "base-host"
        assert settings.database.port == 6543

    def test_c09_lists_and_scalars_replace_wholesale(self) -> None:
        merged = deep_merge(
            {"allow": ["a", "b"], "count": 1, "nested": {"keep": True}},
            {"allow": ["z"], "count": 2},
        )
        assert merged["allow"] == ["z"]
        assert merged["count"] == 2
        assert merged["nested"] == {"keep": True}
        # A mapping override deep-merges rather than replacing.
        merged2 = deep_merge({"nested": {"keep": True, "x": 1}}, {"nested": {"x": 2}})
        assert isinstance(merged2["nested"], dict)
        assert merged2["nested"] == {"keep": True, "x": 2}


class TestFailClosed:
    """C10-C15: bounded failures."""

    def test_c10_unknown_key_fails(self, tmp_path: Path) -> None:
        base = _BASE + "\n[unknown_section]\nvalue = 1\n"
        config_dir = _scaffold(tmp_path, base=base, profiles={"development": _DEV})
        with pytest.raises(ValidationError):
            load_settings(config_dir=config_dir)

    def test_c11_missing_selected_profile_fails(
        self, tmp_path: Path, monkeypatch: MonkeyPatch
    ) -> None:
        config_dir = _scaffold(tmp_path, profiles={"development": _DEV})
        monkeypatch.setenv("DARKULA_CONFIG_PROFILE", "missing")
        with pytest.raises(ConfigError, match="profile not found"):
            load_settings(config_dir=config_dir)

    def test_c12_malformed_toml_fails_bounded(self, tmp_path: Path) -> None:
        config_dir = _scaffold(
            tmp_path, base="[database\nhost = broken", profiles={"development": _DEV}
        )
        with pytest.raises(ConfigError, match="malformed TOML"):
            load_settings(config_dir=config_dir)

    @pytest.mark.parametrize(
        "bad",
        ["Dev Profile", "-leading", "9number", "UPPER", "with space"],
    )
    def test_c13_malformed_profile_name_rejected(
        self, tmp_path: Path, monkeypatch: MonkeyPatch, bad: str
    ) -> None:
        config_dir = _scaffold(tmp_path, profiles={"development": _DEV})
        monkeypatch.setenv("DARKULA_CONFIG_PROFILE", bad)
        with pytest.raises(ValueError, match="profile grammar"):
            load_settings(config_dir=config_dir)

    def test_c13_profile_pattern_accepts_valid_names(self) -> None:
        for name in ("development", "test", "staging-1", "prod_blue"):
            assert PROFILE_PATTERN.fullmatch(name) is not None

    def test_c14_missing_optional_local_accepted(self, tmp_path: Path) -> None:
        config_dir = _scaffold(tmp_path, profiles={"development": _DEV})
        settings = load_settings(config_dir=config_dir)
        assert settings.database.host == "base-host"
        layers = resolve_layers(config_dir=config_dir)
        assert layers.local == {}

    def test_c15_malformed_typed_env_fails(
        self, tmp_path: Path, monkeypatch: MonkeyPatch
    ) -> None:
        config_dir = _scaffold(tmp_path, profiles={"development": _DEV})
        monkeypatch.setenv("DARKULA_DATABASE__PORT", "not-a-port")
        with pytest.raises(ValidationError):
            load_settings(config_dir=config_dir)

    def test_required_base_missing_fails(self, tmp_path: Path) -> None:
        config_dir = tmp_path / "empty"
        config_dir.mkdir()
        with pytest.raises(ConfigError, match="base configuration"):
            load_settings(config_dir=config_dir)


class TestDiagnostics:
    """C16: secret-like configuration values are redacted."""

    def test_c16_secret_like_diagnostic_redacted(self, tmp_path: Path) -> None:
        base = """\
[database]
host = "base-host"
port = 6432
password = "hunter2"

[object_store]
driver = "local"
"""
        config_dir = _scaffold(tmp_path, base=base, profiles={"development": _DEV})
        layers = resolve_layers(config_dir=config_dir)
        summary = diagnostic_summary(layers)
        assert "hunter2" not in summary
        assert "<redacted>" in summary

    def test_redact_config_recurses_and_preserves(self) -> None:
        value = {
            "ok": {"name": "bob"},
            "api_key": "k-123",
            "list": [{"session_token": "s-1", "keep": "yes"}, "plain"],
        }
        redacted = redact_config(value)
        assert isinstance(redacted, dict)
        assert redacted["api_key"] == "<redacted>"
        entries = redacted["list"]
        assert isinstance(entries, list)
        first = entries[0]
        assert isinstance(first, dict)
        assert first["session_token"] == "<redacted>"
        assert first["keep"] == "yes"
        assert redacted["ok"] == {"name": "bob"}

    def test_diagnostic_summary_lists_loaded_layers(self, tmp_path: Path) -> None:
        config_dir = _scaffold(tmp_path, profiles={"development": _DEV}, local=_LOCAL)
        summary = diagnostic_summary(resolve_layers(config_dir=config_dir))
        assert "profile=development" in summary
        assert "layers=base,profile:development,local" in summary


class TestDeterminism:
    """C17-C18: reproducible and side-effect-free."""

    def test_c17_same_inputs_twice_equal_settings(self, tmp_path: Path) -> None:
        config_dir = _scaffold(tmp_path, profiles={"development": _DEV})
        first = load_settings(config_dir=config_dir)
        second = load_settings(config_dir=config_dir)
        assert first == second
        assert isinstance(first, Settings)

    def test_c18_loader_never_mutates_process_environment(self, tmp_path: Path) -> None:
        config_dir = _scaffold(tmp_path, profiles={"development": _DEV})
        snapshot = dict(os.environ)
        load_settings(config_dir=config_dir)
        assert dict(os.environ) == snapshot

    def test_c18_live_environment_read_is_restored(
        self, tmp_path: Path, monkeypatch: MonkeyPatch
    ) -> None:
        config_dir = _scaffold(tmp_path, profiles={"development": _DEV})
        monkeypatch.setenv("DARKULA_DATABASE__HOST", "live-host")
        settings = load_settings(config_dir=config_dir)
        assert settings.database.host == "live-host"
        monkeypatch.undo()
        assert os.environ.get("DARKULA_DATABASE__HOST") is None


class TestResolution:
    """Profile selection and driver defaults through the loader."""

    def test_env_profile_selection(
        self, tmp_path: Path, monkeypatch: MonkeyPatch
    ) -> None:
        test_profile = """\
[object_store]
driver = "in_memory"
"""
        config_dir = _scaffold(tmp_path, profiles={"test": test_profile})
        monkeypatch.setenv("DARKULA_CONFIG_PROFILE", "test")
        settings = load_settings(config_dir=config_dir)
        assert settings.config_profile == "test"
        assert settings.object_store.driver is ObjectStoreDriver.IN_MEMORY

    def test_shipped_defaults_are_deterministic_fake(self) -> None:
        # The real shipped config tree resolves the fake datastream driver
        # with no environment at all (offline by default in PR 3).
        shipped = _shipped_config_dir()
        if shipped is not None:
            settings = load_settings(config_dir=shipped)
            assert settings.datastream.driver is DataStreamDriver.FAKE
            assert settings.object_store.driver is ObjectStoreDriver.LOCAL


def _shipped_config_dir() -> Path | None:
    """Return the repository config directory when present, else None."""
    candidate = Path(__file__).resolve().parents[3] / "config"
    return candidate if candidate.is_dir() else None
