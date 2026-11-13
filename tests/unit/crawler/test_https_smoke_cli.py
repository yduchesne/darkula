# SPDX-License-Identifier: AGPL-3.0-only
"""PR 20 operator smoke CLI tests (U20-47)."""

from __future__ import annotations

import json
from types import SimpleNamespace

import darkula_https_smoke as smoke  # type: ignore[import-not-found]
import pytest

from darkula.config.settings import CrawlerSettings
from darkula.crawler.contracts import CrawlResult, CrawlStatus

ORIGIN = "https://approved.example.org:443"


@pytest.fixture
def enabled_settings() -> SimpleNamespace:
    return SimpleNamespace(
        crawler=CrawlerSettings(
            real_https_enabled=True,
            real_https_allowed_origins=(ORIGIN,),
        )
    )


def test_u20_47_missing_either_flag_exits_2_before_podman(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail() -> None:
        raise AssertionError("settings must not load without both flags")

    monkeypatch.setattr(smoke, "load_settings", fail)
    base = ["--start-url", f"{ORIGIN}/docs/", "--origin", ORIGIN]
    assert smoke.main(base) == 2
    assert smoke.main([*base, "--live"]) == 2
    assert smoke.main([*base, "--operator-approved"]) == 2


def test_u20_47_budget_over_limit_exits_2_before_podman(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail() -> None:
        raise AssertionError("settings must not load for an over-limit budget")

    monkeypatch.setattr(smoke, "load_settings", fail)
    code = smoke.main(
        [
            "--live",
            "--operator-approved",
            "--start-url",
            f"{ORIGIN}/docs/",
            "--origin",
            ORIGIN,
            "--max-requests",
            "61",
        ]
    )
    assert code == 2


def test_u20_47_missing_allowlist_exits_2(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(
        smoke,
        "load_settings",
        lambda: SimpleNamespace(crawler=CrawlerSettings()),
    )
    code = smoke.main(
        [
            "--live",
            "--operator-approved",
            "--start-url",
            f"{ORIGIN}/docs/",
            "--origin",
            ORIGIN,
        ]
    )
    assert code == 2
    captured = capsys.readouterr()
    assert ORIGIN not in captured.out + captured.err


def test_u20_47_valid_run_prints_only_counters(
    monkeypatch: pytest.MonkeyPatch,
    enabled_settings: SimpleNamespace,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(smoke, "load_settings", lambda: enabled_settings)

    class _Sandbox:
        @classmethod
        def from_settings(cls, settings: object) -> _Sandbox:
            return cls()

    class _Controller:
        def __init__(self, *, settings: object, sandbox: object) -> None:
            pass

        async def crawl(self, request: object) -> CrawlResult:
            return CrawlResult(
                request_id="https-smoke",
                status=CrawlStatus.COMPLETED,
                pages=(),
                requests=3,
                duration_seconds=1.25,
                cleanup_succeeded=True,
            )

    monkeypatch.setattr(smoke, "PodmanSandbox", _Sandbox)
    monkeypatch.setattr(smoke, "CrawlerController", _Controller)
    code = smoke.main(
        [
            "--live",
            "--operator-approved",
            "--start-url",
            f"{ORIGIN}/docs/",
            "--origin",
            ORIGIN,
            "--path-prefix",
            "/docs/",
            "--max-pages",
            "2",
            "--max-requests",
            "10",
            "--max-depth",
            "1",
            "--timeout-seconds",
            "30",
        ]
    )
    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    assert set(payload) == {
        "status",
        "pages",
        "requests",
        "duration",
        "reason_code",
        "cleanup_verified",
    }
    assert payload["status"] == "completed"
    assert payload["cleanup_verified"] is True
    assert "approved" not in json.dumps(payload)
