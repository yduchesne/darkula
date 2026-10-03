#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""Deterministic Darkula migration runner (operator/tooling, not runtime).

Applies the versioned ``migrations/NNNN_*.sql`` artifacts in lexical order
to a PostgreSQL database. Records applied versions in the
``darkula_schema_migrations`` ledger table (the only bookkeeping SQL that
lives in Python tooling; every data/schema operation lives in the migration
artifacts themselves).

Usage:

    uv run python scripts/darkula_migrate.py --apply --db darkula --user darkula ...
    uv run python scripts/darkula_migrate.py --current

Connection parameters come from ``--db/--user/--host/--port`` flags or from
the process environment (``DARKULA_DATABASE__*`` variables, same naming as
configuration) so `./build.sh --intg` stays deterministic. DSNs/credentials
are never printed.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path
from typing import NoReturn

import psycopg
from psycopg import Connection

MIGRATIONS_DIR = Path(__file__).resolve().parents[1] / "migrations"

#: Ledger table recording every applied migration artifact filename.
LEDGER_DDL = """
CREATE TABLE IF NOT EXISTS darkula_schema_migrations (
    version    text PRIMARY KEY,
    applied_at timestamptz NOT NULL DEFAULT now()
)
"""


def _env_or(flag: str, env_name: str, default: str) -> str:
    """Prefer an explicit CLI flag, then a non-empty env value."""
    value = os.environ.get(env_name)
    if value:
        return value
    return flag or default


def migration_files() -> list[Path]:
    """Return migration artifacts in deterministic lexical order."""
    candidates = sorted(MIGRATIONS_DIR.glob("[0-9][0-9][0-9][0-9]_*.sql"))
    if not candidates:
        raise RuntimeError(f"no migration artifacts found under {MIGRATIONS_DIR}")
    return candidates


def applied_versions(conn: Connection) -> set[str]:
    """Return the set of already-applied migration filenames."""
    with conn.cursor() as cur:
        cur.execute("SELECT version FROM darkula_schema_migrations")
        return {row[0] for row in cur.fetchall()}


def apply_migrations(
    *,
    conn: Connection,
    files: list[Path] | None = None,
) -> list[str]:
    """Apply every pending migration artifact; return the applied names.

    Each artifact runs in its own transaction, then its filename is recorded
    in the ledger. Migrations are applied exactly once each.
    """
    pending = migration_files() if files is None else sorted(set(files))
    applied = applied_versions(conn)
    executed: list[str] = []
    for path in pending:
        if path.name in applied:
            continue
        sql = path.read_text(encoding="utf-8")
        with conn.transaction():
            conn.execute(sql)
            conn.execute(
                "INSERT INTO darkula_schema_migrations (version) VALUES (%s)",
                (path.name,),
            )
        executed.append(path.name)
    return executed


def current_head(conn: Connection) -> str:
    """Return the newest-applied migration, or ``(none)``."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT version FROM darkula_schema_migrations "
            "ORDER BY version DESC LIMIT 1"
        )
        row = cur.fetchone()
    return row[0] if row else "(none)"


def _connect(args: argparse.Namespace) -> Connection:
    host = _env_or(args.host, "DARKULA_DATABASE__HOST", "localhost")
    port = int(_env_or(str(args.port), "DARKULA_DATABASE__PORT", "35432"))
    dbname = _env_or(args.db, "DARKULA_DATABASE__NAME", "darkula")
    user = _env_or(args.user, "DARKULA_DATABASE__USER", "darkula")
    password = os.environ.get("DARKULA_DATABASE__PASSWORD")
    if args.password:
        password = args.password
    conninfo = {
        "host": host,
        "port": port,
        "dbname": dbname,
        "user": user,
        "connect_timeout": 10,
    }
    if password:
        conninfo["password"] = password
    try:
        return psycopg.connect(**conninfo)
    except psycopg.OperationalError as exc:
        raise RuntimeError("cannot connect to the migration database") from exc


def _apply(args: argparse.Namespace) -> None:
    conn = _connect(args)
    try:
        with conn.cursor() as cur:
            cur.execute(LEDGER_DDL)
        executed = apply_migrations(conn=conn)
        conn.commit()
    finally:
        conn.close()
    if executed:
        print(f"applied migrations: {', '.join(executed)}")
    else:
        print("no pending migrations")


def _current(args: argparse.Namespace) -> None:
    conn = _connect(args)
    try:
        print(f"database head: {current_head(conn)}")
    finally:
        conn.close()


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--host", default="", help="database host")
    parser.add_argument("--port", type=int, default=0, help="database port")
    parser.add_argument("--db", default="", help="database name")
    parser.add_argument("--user", default="", help="database user")
    parser.add_argument("--password", default="", help="database password")
    parser.add_argument(
        "--apply",
        action="store_true",
        help="apply all pending migrations",
    )
    parser.add_argument(
        "--current",
        action="store_true",
        help="print the current database head",
    )
    return parser


def main(argv: list[str] | None = None) -> NoReturn:
    parser = _parser()
    args = parser.parse_args(argv)
    try:
        if args.apply:
            _apply(args)
        elif args.current:
            _current(args)
        else:
            parser.error("select exactly one of --apply / --current")
    except (RuntimeError, psycopg.Error) as exc:
        print(f"error: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
    raise SystemExit(0)


if __name__ == "__main__":
    main()
