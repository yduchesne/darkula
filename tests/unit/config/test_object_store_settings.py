# SPDX-License-Identifier: AGPL-3.0-only
"""C-series: S3/R2 object-store configuration semantics (PR 8)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from pytest import MonkeyPatch

from darkula.config.loader import load_settings, redact_config
from darkula.config.settings import ObjectStoreDriver, ObjectStoreSettings

_BASE = """
[object_store]
driver = "s3"
bucket = "file-bucket"
access_key_id = "file-access-key"
"""

_DEV = ""


def _scaffold(tmp_path: Path) -> Path:
    config_dir = tmp_path / "config"
    (config_dir / "profiles").mkdir(parents=True)
    (config_dir / "base.toml").write_text(_BASE, encoding="utf-8")
    (config_dir / "profiles" / "development.toml").write_text(
        "\n".join(filter(None, [_DEV])), encoding="utf-8"
    )
    return config_dir


class TestRemoteSettings:
    """C6/C7: env precedence and empty-env no-op for remote fields."""

    def test_c6_env_credential_overrides_file(
        self, tmp_path: Path, monkeypatch: MonkeyPatch
    ) -> None:
        monkeypatch.setenv("DARKULA_OBJECT_STORE__ACCESS_KEY_ID", "AKIA-ENV")
        settings = load_settings(config_dir=_scaffold(tmp_path))
        # Env wins over the lower file layer.
        assert settings.object_store.access_key_id == "AKIA-ENV"
        assert settings.object_store.bucket == "file-bucket"
        assert settings.object_store.driver is ObjectStoreDriver.S3

    def test_c7_empty_env_credential_keeps_lower_layer(
        self, tmp_path: Path, monkeypatch: MonkeyPatch
    ) -> None:
        monkeypatch.setenv("DARKULA_OBJECT_STORE__ACCESS_KEY_ID", "")
        settings = load_settings(config_dir=_scaffold(tmp_path))
        assert settings.object_store.access_key_id == "file-access-key"

    def test_c8_diagnostic_summary_redacts_credentials(self) -> None:
        rendered: Any = redact_config(
            {
                "object_store": {
                    "driver": "s3",
                    "bucket": "darkula-bucket",
                    "access_key_id": "AKIA-SECRET",
                    "secret_access_key": "S3CRET",
                    "session_token": "token123",
                }
            }
        )
        obj = rendered["object_store"]
        assert obj["access_key_id"] == "<redacted>"
        assert obj["secret_access_key"] == "<redacted>"
        assert obj["session_token"] == "<redacted>"
        # Non-secret fields survive for diagnostics.
        assert obj["bucket"] == "darkula-bucket"
        assert obj["driver"] == "s3"

    def test_bucket_allowed_at_settings_layer(self) -> None:
        # bucket is optional at the settings layer; composition enforces it.
        settings = ObjectStoreSettings(driver=ObjectStoreDriver.S3, bucket=None)
        assert settings.bucket is None

    def test_blank_bucket_rejected(self) -> None:
        with pytest.raises(ValueError, match="blank"):
            ObjectStoreSettings(driver=ObjectStoreDriver.S3, bucket="   ")
