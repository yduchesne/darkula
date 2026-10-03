# SPDX-License-Identifier: AGPL-3.0-only
"""Positive static SQL boundary guard (PR 5B).

Production Python contains no database-behavior/data-access SQL. The only
SQL allowed in production PostgreSQL persistence modules is a *fixed,
parameterized invocation* of an approved versioned stored function:

    SELECT * FROM <name>_vN(<zero or more %s placeholders>)

Whitespace/newlines may vary; everything else is rejected: direct table
selects, DML/DDL/CTEs/transaction SQL, non-versioned function names,
embedded literal arguments, f-string SQL, concatenated/formatted SQL, and
multiple statements.

The guard is deliberately scoped to the production PostgreSQL persistence
package; test-only SQL outside that package (setup/cleanup, fixtures,
assertions, fault injection) is unaffected (SQL14).
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[5]
_PACKAGE = (
    _REPO_ROOT / "src" / "darkula" / "infrastructure" / "persistence" / "postgresql"
)

# Full allowed grammar, including multiline whitespace flexibility.
_INVOCATION_RE = re.compile(
    r"^\s*SELECT\s+\*\s+FROM\s+[a-z][a-z0-9_]*_v[1-9][0-9]*\s*"
    r"\(\s*(?:%s(?:\s*,\s*%s)*)?\s*\)\s*$",
    re.IGNORECASE,
)
# Loose skeleton for granular diagnostics (name + argument list).
_INVOCATION_HEAD_RE = re.compile(
    r"^\s*SELECT\s+\*\s+FROM\s+([^\s(]+)\s*\((.*)\)\s*$",
    re.IGNORECASE | re.DOTALL,
)
_FN_RE = re.compile(r"[a-z][a-z0-9_]*_v[1-9][0-9]*", re.IGNORECASE)
# Statement-starting keywords that mark a string as SQL.
_STATEMENT_RE = re.compile(
    r"^\s*(?:SELECT|INSERT|UPDATE|DELETE|MERGE|WITH|CREATE|ALTER|DROP|"
    r"TRUNCATE|BEGIN|COMMIT|ROLLBACK|SET|GRANT|REVOKE|VACUUM|ANALYZE|"
    r"EXPLAIN|DO|CALL)\b",
    re.IGNORECASE,
)


def _is_sql_statement(text: str) -> bool:
    return _STATEMENT_RE.match(text) is not None


def _fstring_text(node: ast.JoinedStr) -> str:
    """Reconstruct the rendered text of an f-string (placeholders shown
    as ``{expr}``) so it can be classified and validated like a literal."""
    parts: list[str] = []
    for value in node.values:
        if isinstance(value, ast.FormattedValue):
            inner = ast.unparse(value.value)
            if value.conversion != -1:
                inner += f"!{chr(value.conversion)}"
            if value.format_spec is not None and isinstance(
                value.format_spec, ast.JoinedStr
            ):
                inner += ":" + _fstring_text(value.format_spec)
            parts.append("{" + inner + "}")
        elif isinstance(value, ast.Constant):
            parts.append(str(value.value))
        elif isinstance(value, ast.JoinedStr):
            parts.append(_fstring_text(value))
    return "".join(parts)


def validate_sql_literal(text: str) -> str | None:
    """Return a violation reason for *literal* SQL, or None when the text
    is exactly one fixed parameterized stored-function invocation."""
    stripped = text.strip()
    if _INVOCATION_RE.match(stripped) is not None:
        return None
    if ";" in stripped:
        return "multiple statements (unexpected ';')"
    head = _INVOCATION_HEAD_RE.match(stripped)
    if head is None:
        return (
            "not a stored-function invocation "
            "(expected SELECT * FROM <name>_v<positive integer>(...))"
        )
    name, args = head.group(1), head.group(2)
    if _FN_RE.fullmatch(name) is None:
        return (
            f"non-versioned function name {name!r} "
            "(expected <name>_v<positive integer>)"
        )
    for arg in (part.strip() for part in args.split(",") if part.strip()):
        if arg != "%s":
            return (
                f"embedded literal argument {arg!r} (only %s placeholders are allowed)"
            )
    return "malformed stored-function invocation"


def _format_call_base(node: ast.Call) -> str | None:
    """Return the base string of a ``str.format(...)`` call, else None."""
    if not isinstance(node.func, ast.Attribute) or node.func.attr != "format":
        return None
    base = node.func.value
    if isinstance(base, ast.Constant) and isinstance(base.value, str):
        return base.value
    if isinstance(base, ast.JoinedStr):
        return _fstring_text(base)
    return None


def _operand_text(node: ast.expr) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr):
        return _fstring_text(node)
    if isinstance(node, ast.Call):
        return _format_call_base(node)
    return None


def validate_module_source(source: str) -> list[tuple[str, str]]:
    """Return ``[(reason, snippet)]`` for every SQL violation in a module.

    Detects SQL passed to the driver via constants, f-strings, string
    concatenation, ``str.format()``, and ``%``-formatting.
    """
    try:
        tree = ast.parse(source)
    except SyntaxError as exc:
        return [(f"syntax error: {exc}", "")]

    violations: list[tuple[str, str]] = []

    # Constants that are part of an f-string are classified as f-strings
    # below; do not double-report their fragments here.
    fstring_descendants: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.JoinedStr):
            for child in ast.walk(node):
                fstring_descendants.add(id(child))

    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and id(node) not in fstring_descendants
        ):
            text = node.value
            if not _is_sql_statement(text):
                continue
            reason = validate_sql_literal(text)
            if reason is not None:
                violations.append((reason, text))
    for node in ast.walk(tree):
        if isinstance(node, ast.JoinedStr):
            text = _fstring_text(node)
            if _is_sql_statement(text):
                violations.append(("f-string SQL", text))
    for node in ast.walk(tree):
        if isinstance(node, ast.BinOp) and isinstance(node.op, (ast.Add, ast.Mod)):
            sides = [
                operand
                for side in (node.left, node.right)
                if (operand := _operand_text(side)) is not None
            ]
            if any(_is_sql_statement(side) for side in sides):
                violations.append(("concatenated or %/mod-format SQL", "".join(sides)))
        elif isinstance(node, ast.Call):
            base = _format_call_base(node)
            if base is not None and _is_sql_statement(base):
                violations.append(("str.format() SQL", base))

    # De-duplicate identical (reason, snippet) pairs while preserving order.
    seen: set[tuple[str, str]] = set()
    unique: list[tuple[str, str]] = []
    for violation in violations:
        if violation not in seen:
            seen.add(violation)
            unique.append(violation)
    return unique


def scan_package(package: Path) -> dict[str, list[tuple[str, str]]]:
    """Scan every production ``.py`` module in ``package`` (except
    ``__init__.py``) for SQL boundary violations keyed by package path."""
    found: dict[str, list[tuple[str, str]]] = {}
    for path in sorted(package.glob("*.py")):
        if path.name == "__init__.py":
            continue
        violations = validate_module_source(path.read_text(encoding="utf-8"))
        if violations:
            found[path.name] = violations
    return found


class TestInvocationGrammar:
    """SQL1/SQL2: fixed parameterized versioned-function calls are the only
    accepted literals, including multiline whitespace."""

    @pytest.mark.parametrize(
        "sql",
        [
            "SELECT * FROM candidate_get_v1(%s)",
            "SELECT * FROM source_assessment_append_v1("
            "%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
            "select * from candidate_transition_v1(%s, %s, %s, %s, %s, %s, %s, %s, %s)",
            "SELECT * FROM candidate_create_v1(\n"
            "    %s, %s, %s, %s, %s,\n"
            "    %s, %s, %s, %s, %s, %s, %s\n"
            ")",
            "SELECT   *   FROM   source_get_v1( %s )",
            "SELECT * FROM recon_assessment_list_v1()",
        ],
    )
    def test_accepts_parameterized_invocations(self, sql: str) -> None:
        assert validate_sql_literal(sql) is None

    @pytest.mark.parametrize(
        ("sql", "reason"),
        [
            # SQL3: direct table SELECT.
            (
                "SELECT * FROM source_candidate WHERE candidate_id = %s",
                "not a stored-function invocation",
            ),
            # SQL4: INSERT.
            (
                "INSERT INTO source_candidate (candidate_id) VALUES (%s)",
                "not a stored-function invocation",
            ),
            # SQL5: UPDATE.
            (
                "UPDATE source_candidate SET status = %s WHERE candidate_id = %s",
                "not a stored-function invocation",
            ),
            # SQL6: DELETE.
            (
                "DELETE FROM source_candidate WHERE candidate_id = %s",
                "not a stored-function invocation",
            ),
            # SQL7: DDL / TRUNCATE.
            (
                "CREATE TABLE source_candidate (candidate_id uuid PRIMARY KEY)",
                "not a stored-function invocation",
            ),
            ("TRUNCATE TABLE source_candidate", "not a stored-function invocation"),
            # SQL8: CTE.
            (
                "WITH recent AS (SELECT * FROM source_candidate) SELECT * FROM recent",
                "not a stored-function invocation",
            ),
            # SQL9: non-versioned function name.
            (
                "SELECT * FROM candidate_get(%s)",
                "non-versioned function name",
            ),
            (
                "SELECT * FROM candidate_get_v0(%s)",
                "non-versioned function name",
            ),
            # SQL10: embedded literal argument.
            (
                "SELECT * FROM candidate_get_v1('literal-id')",
                "embedded literal argument",
            ),
            ("SELECT * FROM candidate_get_v1(%s, 5)", "embedded literal argument"),
            ("SELECT * FROM candidate_get_v1(%d)", "embedded literal argument"),
            # SQL13: multiple statements.
            (
                "SELECT * FROM candidate_get_v1(%s); DROP TABLE source_candidate",
                "multiple statements",
            ),
        ],
    )
    def test_rejects_non_invocations(self, sql: str, reason: str) -> None:
        found = validate_sql_literal(sql)
        assert found is not None
        assert reason in found


class TestDynamicSqlRejection:
    """SQL11/SQL12: f-strings and dynamically built SQL are rejected even
    when the assembled text would match the invocation grammar."""

    def test_fstring_sql_is_rejected(self) -> None:
        source = 'sql = f"SELECT * FROM candidate_get_v1({candidate_id})"\n'
        violations = validate_module_source(source)
        assert any(reason == "f-string SQL" for reason, _ in violations)

    def test_fstring_even_without_placeholders_is_rejected(self) -> None:
        source = 'sql = f"SELECT * FROM candidate_get_v1(%s)"\n'
        violations = validate_module_source(source)
        assert any(reason == "f-string SQL" for reason, _ in violations)

    def test_concatenation_is_rejected(self) -> None:
        source = 'sql = "SELECT * FROM " + "candidate_get_v1(%s)"\n'
        violations = validate_module_source(source)
        assert any("concatenated" in reason for reason, _ in violations)

    def test_format_is_rejected(self) -> None:
        source = 'sql = "SELECT * FROM candidate_get_v1({0})".format(candidate_id)\n'
        violations = validate_module_source(source)
        assert any(reason == "str.format() SQL" for reason, _ in violations)

    def test_percent_format_is_rejected(self) -> None:
        source = 'sql = "SELECT * FROM candidate_get_v1(%s)" % (candidate_id,)\n'
        violations = validate_module_source(source)
        assert any("mod-format SQL" in reason for reason, _ in violations)


class TestProductionPackageGuard:
    """The weak keyword denylist is replaced by positive validation: the
    whole production PostgreSQL persistence package must contain ONLY fixed
    parameterized stored-function invocations."""

    MODULES = scan_package(_PACKAGE)

    def test_package_contains_only_invocations(self) -> None:
        assert self.MODULES == {}, self.MODULES


class TestScope:
    """SQL14: test-only SQL outside the production package is unaffected —
    the guard is scoped to the production package, not weakened."""

    def test_test_only_sql_outside_production_is_not_scanned(
        self, tmp_path: Path
    ) -> None:
        helper = tmp_path / "raw_sql_helper.py"
        helper.write_text(
            'cur.execute("SELECT tablename FROM pg_tables "'
            " \"WHERE schemaname = 'public'\")\n",
            encoding="utf-8",
        )
        # Guard scope is the production package only; a raw-SQL helper under
        # a test path is never inspected.
        scanned = {p.name for p in _PACKAGE.glob("*.py") if p.name != "__init__.py"}
        assert "raw_sql_helper.py" not in scanned
        # The same raw SQL would be a violation in production: scope
        # discipline, not a weaker rule.
        violations = validate_module_source(helper.read_text(encoding="utf-8"))
        assert any(
            "not a stored-function invocation" in reason for reason, _ in violations
        )
        # And the production package itself stays clean.
        assert TestProductionPackageGuard.MODULES == {}
