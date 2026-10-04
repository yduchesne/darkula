# SPDX-License-Identifier: AGPL-3.0-only
"""PR 9 CollectionSettings tests (CFG matrix).

Defaults are conservative; batch sizes, lease, and attempts are bounded;
environment overrides follow the standard env-last/empty-ignored rules;
diagnostics never expose credentials (PR 9 policies hold references only).
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from pydantic import ValidationError

from darkula.config.settings import CollectionSettings, Settings

_SHIPPED_CONFIG = Path(__file__).resolve().parents[3] / "config"


class TestCollectionSettings:
    """CFG: defaults and validation bounds."""

    def test_cfg_defaults_are_conservative(self) -> None:
        settings = CollectionSettings()
        assert settings.schedule_batch_size == 10
        assert settings.worker_poll_batch_size == 10
        assert settings.execution_lease_seconds == 600.0
        assert settings.max_run_attempts == 3

    def test_cfg_rejects_non_positive_batches(self) -> None:
        with pytest.raises(ValidationError):
            CollectionSettings(schedule_batch_size=0)
        with pytest.raises(ValidationError):
            CollectionSettings(worker_poll_batch_size=-1)

    def test_cfg_rejects_non_positive_lease(self) -> None:
        with pytest.raises(ValidationError):
            CollectionSettings(execution_lease_seconds=0.0)
        with pytest.raises(ValidationError):
            CollectionSettings(execution_lease_seconds=-5)

    def test_cfg_rejects_out_of_bound_attempts(self) -> None:
        with pytest.raises(ValidationError):
            CollectionSettings(max_run_attempts=0)
        with pytest.raises(ValidationError):
            CollectionSettings(max_run_attempts=11)

    def test_cfg_unknown_field_fails_closed(self) -> None:
        with pytest.raises(ValidationError):
            CollectionSettings(retry_backoff_seconds=5)  # type: ignore[call-arg]

    def test_cfg_env_override_beats_default(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        for key in [key for key in os.environ if key.startswith("DARKULA_")]:
            monkeypatch.delenv(key, raising=False)
        monkeypatch.setenv("DARKULA_COLLECTION__MAX_RUN_ATTEMPTS", "7")
        settings = Settings()
        assert settings.collection.max_run_attempts == 7
        assert settings.collection.schedule_batch_size == 10

    def test_cfg_empty_env_is_ignored(self, monkeypatch: pytest.MonkeyPatch) -> None:
        for key in [key for key in os.environ if key.startswith("DARKULA_")]:
            monkeypatch.delenv(key, raising=False)
        monkeypatch.setenv("DARKULA_COLLECTION__MAX_RUN_ATTEMPTS", "")
        settings = Settings()
        assert settings.collection.max_run_attempts == 3


class TestComposedCollection:
    """Collection composition wires the real PR 9 capabilities."""

    def test_compose_wires_collection_capability(self) -> None:
        from darkula.app.collection import SourceCollectionService
        from darkula.app.collection_scheduler import CollectionScheduler
        from darkula.app.collection_worker import CollectionWorker
        from darkula.composition import Runtime, compose
        from darkula.config.loader import load_settings

        runtime = compose(settings=load_settings(config_dir=_SHIPPED_CONFIG))
        assert isinstance(runtime, Runtime)
        assert isinstance(runtime.collection_service, SourceCollectionService)
        assert isinstance(runtime.collection_scheduler, CollectionScheduler)
        assert isinstance(runtime.collection_worker, CollectionWorker)
        # The worker consumes the Work stream with the canonical consumer id.
        assert runtime.collection_worker._consumer_id.value == "collection-worker"
