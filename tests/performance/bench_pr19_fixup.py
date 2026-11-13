# SPDX-License-Identifier: AGPL-3.0-only
"""PR 19-F1 local performance benchmark (standalone, never pytest-collected).

Compares the previous full-history access-evidence read with the new
constant-shape ``EXISTS`` probe, and measures the current-managed-endpoint and
verified-evidence queries. All fixtures are synthetic and created inside one
transaction that is rolled back; no source, credential, or network data is
used.

Usage::

    uv run python tests/performance/bench_pr19_fixup.py \
        --warmup 40 --samples 200 --output .benchmarks/pr19f1-local.json

The database must be the Darkula-owned integration PostgreSQL on the
host-published port 35432, migrated to ``0012_candidate_qualification_fixup.sql``
and otherwise empty of business data.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import statistics
import sys
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import psycopg

from darkula.config.loader import load_settings

FIXED_TIME = datetime(2026, 11, 12, 12, 0, 0, tzinfo=UTC)
CASES = (0, 1, 64, 512)
ACCESS_COUNTS = (0, 1, 64, 512)
EXPECTED_HEAD = "0012_candidate_qualification_fixup.sql"
OUTPUT_ROOT = Path(".benchmarks")


class BenchmarkError(RuntimeError):
    """Bounded benchmark failure (no fixture values or credentials)."""


def _positive(value: str) -> int:
    parsed = int(value)
    if parsed < 1:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return parsed


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="PR 19-F1 local benchmark")
    parser.add_argument("--warmup", type=_positive, default=40)
    parser.add_argument("--samples", type=_positive, default=200)
    parser.add_argument(
        "--output",
        type=Path,
        default=OUTPUT_ROOT / "pr19f1-local.json",
    )
    return parser.parse_args(argv)


def _resolve_output(path: Path) -> Path:
    root = OUTPUT_ROOT.resolve()
    resolved = path.resolve()
    if root not in resolved.parents:
        raise BenchmarkError("output path must stay under .benchmarks/")
    if resolved.suffix != ".json":
        raise BenchmarkError("output path must be a .json file")
    return resolved


def _locator(case: int) -> str:
    return f"http://pr19f1-bench-{case}.example.test/"


def _slot_key(locator: str) -> str:
    return hashlib.sha256(locator.encode("utf-8")).hexdigest()


def _percentile(sorted_samples: list[int], percentile: int) -> float:
    index = (percentile * len(sorted_samples) + 99) // 100 - 1
    index = max(0, min(index, len(sorted_samples) - 1))
    return sorted_samples[index] / 1_000_000


def _tim(
    cur: Any,
    sql: str,
    params: tuple[object, ...],
    *,
    warmup: int,
    samples: int,
) -> tuple[list[int], list[Any]]:
    for _ in range(warmup):
        cur.execute(sql, params)
        cur.fetchall()
    recorded: list[int] = []
    rows: list[Any] = []
    for _ in range(samples):
        started = time.perf_counter_ns()
        cur.execute(sql, params)
        rows = list(cur.fetchall())
        recorded.append(time.perf_counter_ns() - started)
    return recorded, rows


def _summarize(samples: list[int], rows: int) -> dict[str, object]:
    ordered = sorted(samples)
    return {
        "samples": len(samples),
        "returned_rows": rows,
        "p50_ms": statistics.median(ordered) / 1_000_000,
        "p95_ms": _percentile(ordered, 95),
        "min_ms": ordered[0] / 1_000_000,
        "max_ms": ordered[-1] / 1_000_000,
    }


def _server_metadata(conn: Any) -> dict[str, str]:
    with conn.cursor() as cur:
        cur.execute("SHOW server_version")
        row = cur.fetchone()
        assert row is not None
        postgres_version = str(row[0])
    return {
        "os": f"{platform.system()} {platform.release()}",
        "machine": platform.machine(),
        "processor": platform.processor() or "unknown",
        "python": platform.python_version(),
        "postgres_version": postgres_version,
    }


def _count(conn: Any, table: str) -> int:
    with conn.cursor() as cur:
        cur.execute(f"SELECT count(*) FROM {table}")
        row = cur.fetchone()
    assert row is not None
    return int(row[0])


def _require_empty(conn: Any) -> None:
    for table in (
        "source_candidate",
        "discovery_observation",
        "access_evidence",
        "trusted_registry_observation",
        "source",
        "source_endpoint",
        "qualification_decision",
    ):
        if _count(conn, table) != 0:
            raise BenchmarkError("the benchmark database is not empty")


def _require_migration(conn: Any) -> None:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT version FROM darkula_schema_migrations "
            "ORDER BY version DESC LIMIT 1"
        )
        row = cur.fetchone()
    if row is None or str(row[0]) != EXPECTED_HEAD:
        raise BenchmarkError("required fixup migration is not applied")


def _insert_candidate(cur: Any, case: int) -> uuid.UUID:
    candidate_id = uuid.uuid4()
    locator = _locator(case)
    cur.execute(
        "INSERT INTO source_candidate "
        "(id, discovered_at, discovery_method, entrypoint, status, "
        " discovery_context) VALUES (%s, %s, 'bench', %s, 'DISCOVERED', NULL)",
        (str(candidate_id), FIXED_TIME, locator),
    )
    cur.execute(
        "INSERT INTO source_candidate_event_history "
        "(event_id, candidate_id, event_type, occurred_at, context, reason, "
        " provenance) VALUES (%s, %s, 'DISCOVERED', %s, NULL, 'bench', 'bench')",
        (str(uuid.uuid4()), str(candidate_id), FIXED_TIME),
    )
    return candidate_id


def _insert_observation(
    cur: Any,
    *,
    case: int,
    index: int,
    candidate_id: uuid.UUID,
    requires_auth: bool,
) -> uuid.UUID:
    observation_id = uuid.uuid4()
    locator = _locator(case)
    record_id = f"pr19f1-{case}-{index}"
    semantic_key = hashlib.sha256(
        ("|".join(("ransomlook", record_id, "v1", "REGISTRY", "LOCATOR"))).encode()
    ).hexdigest()
    cur.execute(
        "INSERT INTO discovery_observation ("
        "observation_id, provider_id, provider_record_id, "
        "provider_record_version, detection_method, reference_kind, "
        "asserted_reference, normalized_locator, validation_status, "
        "source_kind_hint, requires_auth_claim, origin_reference, "
        "evidence_reference, evidence_excerpt, observed_at, ingested_at, "
        "candidate_id, semantic_key) VALUES ("
        "%s,'ransomlook',%s,'v1','REGISTRY','LOCATOR',%s,%s,'VALID_LOCATOR',"
        "'CLEARNET',%s,'https://www.ransomlook.io/api/group/benchmark',%s,NULL,"
        "%s,%s,NULL,%s)",
        (
            str(observation_id),
            record_id,
            locator,
            locator,
            requires_auth,
            f"registry://ransomlook/test/{case}/{index}",
            FIXED_TIME,
            FIXED_TIME,
            semantic_key,
        ),
    )
    if requires_auth:
        cur.execute(
            "INSERT INTO access_evidence ("
            "access_evidence_id, observation_id, candidate_id, "
            "source_endpoint_id, status, reason_code, recorded_at, "
            "evidence_reference) VALUES ("
            "%s,%s,%s,NULL,'CREDENTIALS_PENDING','PROVIDER_AUTH_CLAIM',%s,%s)",
            (
                str(uuid.uuid4()),
                str(observation_id),
                str(candidate_id),
                FIXED_TIME,
                f"registry://ransomlook/test/{case}/{index}",
            ),
        )
    return observation_id


def _insert_slot(
    cur: Any,
    *,
    case: int,
    candidate_id: uuid.UUID,
    first_observation_id: uuid.UUID,
) -> None:
    locator = _locator(case)
    cur.execute(
        "INSERT INTO candidate_locator_slot "
        "(locator_key, normalized_locator, candidate_id, first_observation_id) "
        "VALUES (%s, %s, %s, %s)",
        (_slot_key(locator), locator, str(candidate_id), str(first_observation_id)),
    )


def _insert_admission_and_marker(
    cur: Any,
    *,
    observation_id: uuid.UUID,
    candidate_id: uuid.UUID,
) -> None:
    cur.execute(
        "INSERT INTO discovery_candidate_admission "
        "(observation_id, outcome, reason, candidate_id, processed_at) "
        "VALUES (%s, 'ADMITTED_EXISTING', 'EXISTING_LEGACY_CANDIDATE', %s, %s)",
        (str(observation_id), str(candidate_id), FIXED_TIME),
    )
    cur.execute(
        "SELECT trusted_registry_observation_record_v1(%s, %s)",
        (str(observation_id), FIXED_TIME),
    )
    row = cur.fetchone()
    if row is None or str(row[0]) not in ("added", "replayed"):
        raise BenchmarkError("the verified marker fixture could not be written")


def _seed_fixtures(cur: Any) -> dict[str, str]:
    """Create every synthetic fixture inside the open benchmark transaction."""
    first_observations: dict[int, uuid.UUID] = {}
    for case in CASES:
        candidate_id = _insert_candidate(cur, case)
        first_observation: uuid.UUID | None = None
        for index in range(case):
            observation_id = _insert_observation(
                cur,
                case=case,
                index=index,
                candidate_id=candidate_id,
                requires_auth=True,
            )
            if first_observation is None:
                first_observation = observation_id
        if first_observation is None:
            # The zero-pending case still needs a valid slot reference.
            first_observation = _insert_observation(
                cur,
                case=case,
                index=case,
                candidate_id=candidate_id,
                requires_auth=False,
            )
        _insert_slot(
            cur,
            case=case,
            candidate_id=candidate_id,
            first_observation_id=first_observation,
        )
        first_observations[case] = first_observation
    # One source row for endpoint fixtures.
    source_id = uuid.uuid4()
    cur.execute(
        "INSERT INTO source (id, status, created_at, updated_at, name) "
        "VALUES (%s, 'ACTIVE', %s, %s, 'bench')",
        (str(source_id), FIXED_TIME, FIXED_TIME),
    )
    return {"source_id": str(source_id)}


def _verified_fixtures(cur: Any) -> None:
    """Attach the first 65 verified admitted observations of candidate 512."""
    cur.execute(
        "SELECT a.candidate_id, o.observation_id FROM "
        "discovery_observation o "
        "JOIN candidate_locator_slot a ON a.normalized_locator = "
        "o.normalized_locator WHERE o.requires_auth_claim IS TRUE "
        "AND o.normalized_locator = %s "
        "ORDER BY o.ingested_at, o.observation_id LIMIT 65",
        (_locator(512),),
    )
    rows = list(cur.fetchall())
    for candidate_id, observation_id in rows:
        cur.execute(
            "INSERT INTO discovery_candidate_admission "
            "(observation_id, outcome, reason, candidate_id, processed_at) "
            "VALUES (%s, 'ADMITTED_EXISTING', 'EXISTING_LEGACY_CANDIDATE', "
            "%s, %s) ON CONFLICT (observation_id) DO NOTHING",
            (str(observation_id), str(candidate_id), FIXED_TIME),
        )
        cur.execute(
            "SELECT trusted_registry_observation_record_v1(%s, %s)",
            (str(observation_id), FIXED_TIME),
        )
        cur.fetchone()


def _candidate_ids(cur: Any, case: int) -> tuple[uuid.UUID, uuid.UUID]:
    cur.execute(
        "SELECT s.candidate_id, s.first_observation_id "
        "FROM candidate_locator_slot s WHERE s.normalized_locator = %s",
        (_locator(case),),
    )
    row = cur.fetchone()
    if row is None:
        raise BenchmarkError("the benchmark slot fixture is missing")
    return uuid.UUID(str(row[0])), uuid.UUID(str(row[1]))


def _verified_candidate_with_one(cur: Any) -> uuid.UUID:
    cur.execute(
        "SELECT s.candidate_id FROM candidate_locator_slot s "
        "WHERE s.normalized_locator = %s",
        (_locator(1),),
    )
    row = cur.fetchone()
    if row is None:
        raise BenchmarkError("the one-observation fixture is missing")
    candidate_id = uuid.UUID(str(row[0]))
    cur.execute(
        "SELECT o.observation_id FROM discovery_observation o "
        "JOIN candidate_locator_slot s ON s.normalized_locator = "
        "o.normalized_locator WHERE s.candidate_id = %s AND "
        "o.requires_auth_claim IS TRUE LIMIT 1",
        (str(candidate_id),),
    )
    observation = cur.fetchone()
    if observation is None:
        raise BenchmarkError("the one-observation fixture is incomplete")
    _insert_admission_and_marker(
        cur,
        observation_id=uuid.UUID(str(observation[0])),
        candidate_id=candidate_id,
    )
    return candidate_id


def _explain(cur: Any, sql: str, params: tuple[object, ...]) -> str:
    cur.execute(
        "EXPLAIN (ANALYZE, BUFFERS, TIMING OFF, SUMMARY ON) " + sql,
        params,
    )
    return "\n".join(str(row[0]) for row in cur.fetchall())


def run(argv: list[str]) -> int:
    args = _parse_args(argv)
    output = _resolve_output(args.output)
    os.environ.setdefault("DARKULA_DATABASE__PASSWORD", "darkula-local-intg")
    settings = load_settings()
    if settings.database.port != 35432:
        raise BenchmarkError("the benchmark requires host port 35432")
    if settings.database.host not in ("localhost", "127.0.0.1"):
        raise BenchmarkError("the benchmark requires a loopback database host")

    conn = psycopg.connect(settings.database.conninfo())
    report: dict[str, object] = {
        "warmup": args.warmup,
        "samples": args.samples,
        "host": _server_metadata(conn),
    }
    try:
        _require_migration(conn)
        _require_empty(conn)
        cur = conn.cursor()
        _seed_fixtures(cur)
        _verified_fixtures(cur)
        one_candidate = _verified_candidate_with_one(cur)

        access: dict[str, object] = {}
        for case in ACCESS_COUNTS:
            candidate_id, _first = _candidate_ids(cur, case)
            old_sql = "SELECT * FROM access_evidence_list_for_candidate_v1(%s)"
            new_sql = "SELECT * FROM access_evidence_has_pending_v1(%s)"
            old_times, old_rows = _tim(
                cur,
                old_sql,
                (str(candidate_id),),
                warmup=args.warmup,
                samples=args.samples,
            )
            if len(old_rows) != case:
                raise BenchmarkError("the access fixture row count is wrong")
            new_times, new_rows = _tim(
                cur,
                new_sql,
                (str(candidate_id),),
                warmup=args.warmup,
                samples=args.samples,
            )
            if len(new_rows) != 1:
                raise BenchmarkError("the access probe did not return one row")
            expected = case != 0
            if bool(new_rows[0][0]) is not expected:
                raise BenchmarkError("the access probe returned a wrong value")
            access[f"n{case}"] = {
                "old": _summarize(old_times, len(old_rows)),
                "new": _summarize(new_times, len(new_rows)),
            }
        report["access"] = access
        report["access_explain"] = {
            "old_n512": _explain(
                cur,
                "SELECT * FROM access_evidence_list_for_candidate_v1(%s)",
                (str(_candidate_ids(cur, 512)[0]),),
            ),
            "new_n512": _explain(
                cur,
                "SELECT * FROM access_evidence_has_pending_v1(%s)",
                (str(_candidate_ids(cur, 512)[0]),),
            ),
            "has_pending_inner_n512": _explain(
                cur,
                "SELECT EXISTS (SELECT 1 FROM access_evidence a "
                "WHERE a.candidate_id = %s "
                "AND a.status = 'CREDENTIALS_PENDING')",
                (str(_candidate_ids(cur, 512)[0]),),
            ),
        }

        endpoint_candidate, _first = _candidate_ids(cur, 512)
        endpoint_sql = "SELECT * FROM candidate_current_endpoint_match_v1(%s)"
        endpoints: dict[str, object] = {}
        inserted = 0
        source_id = str(
            _scalar_single(conn, "SELECT id FROM source ORDER BY id LIMIT 1")
        )
        for target in (0, 1, 33):
            while inserted < target:
                _insert_endpoint(cur, source_id=source_id, case=512)
                inserted += 1
            times, rows = _tim(
                cur,
                endpoint_sql,
                (str(endpoint_candidate),),
                warmup=args.warmup,
                samples=args.samples,
            )
            if len(rows) != 1:
                raise BenchmarkError("the endpoint guard did not return one row")
            slot_found, endpoint_present = bool(rows[0][0]), bool(rows[0][1])
            if slot_found is not True:
                raise BenchmarkError("the endpoint guard lost the locator slot")
            if endpoint_present is not (target > 0):
                raise BenchmarkError("the endpoint guard returned a wrong value")
            endpoints[f"endpoints_{target}"] = _summarize(times, len(rows))
        missing = uuid.uuid4()
        cur.execute(endpoint_sql, (str(missing),))
        missing_row = cur.fetchone()
        if missing_row is None or tuple(missing_row) != (False, False):
            raise BenchmarkError("the missing-slot guard is wrong")
        report["endpoint"] = endpoints
        report["endpoint_explain"] = _explain(
            cur, endpoint_sql, (str(endpoint_candidate),)
        )

        verified_sql = "SELECT * FROM trusted_registry_candidate_ids_v1(%s, %s)"
        verified: dict[str, object] = {}
        for label, candidate, expected_count in (
            ("one", one_candidate, 1),
            ("sixty_five", _candidate_ids(cur, 512)[0], 65),
        ):
            times, rows = _tim(
                cur,
                verified_sql,
                (str(candidate), 65),
                warmup=args.warmup,
                samples=args.samples,
            )
            if len(rows) != expected_count:
                raise BenchmarkError("the verified-evidence count is wrong")
            verified[label] = _summarize(times, len(rows))
        report["verified_ids"] = verified
        report["verified_ids_explain"] = _explain(
            cur, verified_sql, (str(_candidate_ids(cur, 512)[0]), 65)
        )
        conn.rollback()
        cur.close()

        fresh = psycopg.connect(settings.database.conninfo())
        try:
            _require_empty(fresh)
        finally:
            fresh.close()
        report["rollback_verified"] = True
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
        print(json.dumps(report, indent=2, sort_keys=True))
        return 0
    finally:
        conn.rollback()
        conn.close()


def _scalar_single(conn: Any, sql: str) -> object:
    with conn.cursor() as cur:
        cur.execute(sql)
        row = cur.fetchone()
    if row is None:
        raise BenchmarkError("the benchmark fixture query returned no row")
    return row[0]


def _insert_endpoint(cur: Any, *, source_id: str, case: int) -> None:
    cur.execute(
        "INSERT INTO source_endpoint ("
        "endpoint_id, source_id, uri, endpoint_type, status, "
        "first_observed_at, last_observed_at, metadata) VALUES ("
        "%s,%s,%s,'CLEARNET','ACTIVE',%s,%s,NULL)",
        (
            str(uuid.uuid4()),
            source_id,
            _locator(case),
            FIXED_TIME,
            FIXED_TIME,
        ),
    )


def main(argv: list[str] | None = None) -> int:
    try:
        return run(sys.argv[1:] if argv is None else argv)
    except BenchmarkError as exc:
        print(f"benchmark: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
