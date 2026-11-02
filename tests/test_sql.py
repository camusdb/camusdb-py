import pytest

from camusdb._sql import (
    StatementKind,
    classify,
    has_returning,
    is_dml,
    keywords,
    runs_in_own_transaction,
)


@pytest.mark.parametrize(
    ("sql", "kind"),
    [
        ("SELECT * FROM robots", StatementKind.QUERY),
        ("  select 1", StatementKind.QUERY),
        ("(SELECT 1)", StatementKind.QUERY),
        ("(SELECT a FROM t ORDER BY a LIMIT 5) UNION ALL SELECT b FROM u", StatementKind.QUERY),
        ("-- leading comment\nSELECT 1", StatementKind.QUERY),
        ("/* block */ SHOW TABLES", StatementKind.QUERY),
        ("WITH x AS (SELECT 1) SELECT * FROM x", StatementKind.QUERY),
        ("WITH x AS (SELECT a FROM t FOR UPDATE) SELECT * FROM x", StatementKind.QUERY),
        ("WITH x (a) AS (VALUES (1)) SELECT (SELECT 1) FROM x", StatementKind.QUERY),
        ("WITH d AS (DELETE FROM t RETURNING *) SELECT * FROM d", StatementKind.NON_QUERY),
        ("WITH d AS MATERIALIZED (UPDATE t SET a = 1) SELECT 1", StatementKind.NON_QUERY),
        ("WITH RECURSIVE x AS (SELECT 1) INSERT INTO t SELECT * FROM x", StatementKind.NON_QUERY),
        ("with x as (select 1), y as (select 2) delete from t", StatementKind.NON_QUERY),
        ("WITH x AS (SELECT 'DELETE' AS `update`) SELECT * FROM x", StatementKind.QUERY),
        ("EXPLAIN SELECT 1", StatementKind.QUERY),
        ("DESCRIBE robots", StatementKind.QUERY),
        ("SET TRANSACTION ISOLATION LEVEL READ COMMITTED", StatementKind.QUERY),
        ("INSERT INTO robots (id) VALUES (GEN_ID())", StatementKind.NON_QUERY),
        ("UPDATE robots SET year = 1", StatementKind.NON_QUERY),
        ("DELETE FROM robots", StatementKind.NON_QUERY),
        ("UPSERT INTO counters (k, n) VALUES ('a', 1)", StatementKind.NON_QUERY),
        ("UPSERT INTO counters (k, n) VALUES ('a', 1) RETURNING n", StatementKind.NON_QUERY),
        (
            "INSERT INTO t (k, n) VALUES (1, 2) ON CONFLICT (k) DO UPDATE SET n = excluded.n",
            StatementKind.NON_QUERY,
        ),
        ("CALL transfer(@a)", StatementKind.NON_QUERY),
        ("CREATE USER app IDENTIFIED BY @password", StatementKind.NON_QUERY),
        ("CREATE DATABASE IF NOT EXISTS shop", StatementKind.NON_QUERY),
        ("GRANT SELECT ON shop.* TO app", StatementKind.NON_QUERY),
        ("CREATE TABLE t (id OID PRIMARY KEY NOT NULL)", StatementKind.DDL),
        ("create unique index i on t (a)", StatementKind.DDL),
        ("CREATE OR REPLACE VIEW v AS SELECT 1", StatementKind.DDL),
        ("CREATE MATERIALIZED VIEW v AS SELECT 1", StatementKind.DDL),
        ("TRUNCATE robots", StatementKind.DDL),
        ("ALTER TABLE t ADD COLUMN c INT64", StatementKind.DDL),
        ("DROP SEQUENCE s", StatementKind.DDL),
        ("CREATE TRIGGER audit ON orders AFTER INSERT EXTERNAL NAME 'X'", StatementKind.DDL),
        ("", StatementKind.NON_QUERY),
    ],
)
def test_classify(sql, kind):
    assert classify(sql) is kind


def test_keywords_skip_literals_and_quoted_names():
    sql = "SELECT 'RETURNING', `returning`, \"it''s\" -- RETURNING\n/* RETURNING */ FROM t"
    assert keywords(sql) == ["SELECT", "FROM", "T"]


def test_a_backslash_escapes_the_next_character():
    assert keywords(r"SELECT 'a\'b' FROM t") == ["SELECT", "FROM", "T"]


def test_returning():
    assert has_returning("INSERT INTO t (a) VALUES (1) RETURNING id")
    assert not has_returning("INSERT INTO t (a) VALUES ('RETURNING')")


@pytest.mark.parametrize(
    "sql",
    ["INSERT INTO t (a) VALUES (1)", "  upsert into t (a) values (1)", "UPDATE t SET a = 1"],
)
def test_writes(sql):
    assert is_dml(sql)


def test_a_query_is_not_a_write():
    assert not is_dml("SELECT upsert FROM t")


@pytest.mark.parametrize(
    "sql",
    [
        "WITH d AS (DELETE FROM t RETURNING *) SELECT * FROM d",
        "WITH x AS (SELECT 1) UPDATE t SET a = 1",
        "/* c */ WITH x AS (SELECT 1) UPSERT INTO t (a) VALUES (1)",
    ],
)
def test_a_writable_cte_is_a_write(sql):
    assert is_dml(sql)


@pytest.mark.parametrize(
    "sql",
    ["WITH x AS (SELECT 1) SELECT * FROM x", "WITH x AS (SELECT 1) SELECT * FROM x FOR UPDATE"],
)
def test_a_reading_cte_is_not_a_write(sql):
    assert not is_dml(sql)


def test_truncate_runs_in_its_own_transaction():
    assert runs_in_own_transaction("TRUNCATE TABLE t")
    assert not runs_in_own_transaction("DELETE FROM t")
