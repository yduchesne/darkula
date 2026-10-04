# SPDX-License-Identifier: AGPL-3.0-only
"""Migration matrix: migrations apply cleanly and schema is complete (P1-P6)."""

from __future__ import annotations

from typing import Any

import psycopg
import pytest

from darkula.config.settings import DatabaseSettings
from tests.integration.conftest import _raw_connect, row_values

pytestmark = pytest.mark.integration


EXPECTED_TABLES = {
    "source_candidate",
    "source_candidate_event_history",
    "recon_assessment",
    "source",
    "source_endpoint",
    "source_assessment",
    "message_outbox",
    "processed_message",
    "content_artifact",
    "normalized_content",
}

EXPECTED_FUNCTIONS = {
    "candidate_create_v1",
    "candidate_get_v1",
    "candidate_transition_v1",
    "candidate_event_append_v1",
    "candidate_history_list_v1",
    "recon_assessment_append_v1",
    "recon_assessment_list_v1",
    "source_create_v1",
    "source_get_v1",
    "source_endpoint_add_v1",
    "source_endpoint_list_v1",
    "source_assessment_append_v1",
    "source_assessment_list_v1",
    "outbox_append_v1",
    "outbox_claim_v1",
    "outbox_mark_published_v1",
    "processed_message_record_v1",
    "content_artifact_create_v1",
    "content_artifact_get_v1",
    "content_artifact_find_v1",
    "normalized_content_create_v1",
    "normalized_content_get_v1",
}


def _tables(conn: psycopg.Connection[Any]) -> set[str]:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT tablename FROM pg_tables "
            "WHERE schemaname = 'public' AND tablename NOT LIKE 'pg_%'"
        )
        return {row[0] for row in cur.fetchall()}


def _functions(conn: psycopg.Connection[Any]) -> set[str]:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT proname FROM pg_proc "
            "WHERE pronamespace = 'public'::regnamespace "
            "AND proname ~ '_v[0-9]+$'"
        )
        return {row[0] for row in cur.fetchall()}


class TestMigrations:
    """P1/P2: deterministic apply + current-head ledger."""

    def test_empty_db_applies_migrations(
        self, database_settings: DatabaseSettings
    ) -> None:
        # P1 is satisfied by build.sh --intg applying to a fresh container;
        # here we verify the ledger head is present and matches the shipped
        # artifact set.
        conn = _raw_connect(database_settings)
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT version FROM darkula_schema_migrations ORDER BY version"
                )
                ledger = [row[0] for row in cur.fetchall()]
        finally:
            conn.close()
        # The ledger head is the newest shipped artifact (PR 8 added 0003).
        assert ledger[-1].startswith("0003_content.sql")

    def test_expected_tables_exist(self, database_settings: DatabaseSettings) -> None:
        conn = _raw_connect(database_settings)
        try:
            tables = _tables(conn)
        finally:
            conn.close()
        assert tables >= EXPECTED_TABLES

    def test_expected_functions_exist(
        self, database_settings: DatabaseSettings
    ) -> None:
        conn = _raw_connect(database_settings)
        try:
            functions = _functions(conn)
        finally:
            conn.close()
        assert functions >= EXPECTED_FUNCTIONS

    def test_required_constraints_exist(
        self, database_settings: DatabaseSettings
    ) -> None:
        conn = _raw_connect(database_settings)
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT conname FROM pg_constraint "
                    "WHERE connamespace = 'public'::regnamespace"
                )
                constraints = {row[0] for row in cur.fetchall()}
        finally:
            conn.close()
        assert "source_candidate_pkey" in constraints
        assert "source_candidate_event_history_candidate_id_fkey" in constraints
        assert "recon_assessment_candidate_id_fkey" in constraints
        assert "source_endpoint_source_id_fkey" in constraints
        assert "source_assessment_check" in constraints
        assert "source_endpoint_check" in constraints
        assert "source_check" in constraints

    def test_host_exposure_is_35432_to_5432(
        self, database_settings: DatabaseSettings
    ) -> None:
        # The settings the suite connects with must target the Darkula
        # host-published port, and the server must be PostgreSQL 18.x.
        conn = _raw_connect(database_settings)
        try:
            with conn.cursor() as cur:
                cur.execute("SHOW port")
                server_port = row_values(cur, cur.fetchone())[0]
                cur.execute("SHOW server_version")
                version = row_values(cur, cur.fetchone())[0]
        finally:
            conn.close()
        assert database_settings.port == 35432
        assert int(server_port) == 5432
        assert version.startswith("18.")


class TestSchemaContinuity:
    """Warn on schema drift expected to be impossible after contract tests."""

    def test_migrations_idempotent_ledger(
        self, database_settings: DatabaseSettings
    ) -> None:
        # Applying again via the operator tool is a no-op; here we only
        # verify the ledger recorded exactly one applied migration head.
        conn = _raw_connect(database_settings)
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT count(*) FROM darkula_schema_migrations "
                    "WHERE version = '0001_initial.sql'"
                )
                count = row_values(cur, cur.fetchone())[0]
        finally:
            conn.close()
        assert count == 1
