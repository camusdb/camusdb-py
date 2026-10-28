import pytest

from camusdb._sql import StatementKind, classify, has_returning, keywords, runs_in_own_transaction


@pytest.mark.parametrize(
    ("sql", "kind"),
    [
        ("SELECT * FROM robots", StatementKind.QUERY),
        ("  select 1", StatementKind.QUERY),
        ("(SELECT 1)", StatementKind.QUERY),
        ("-- leading comment\nSELECT 1", StatementKind.QUERY),
        ("/* block */ SHOW TABLES", StatementKind.QUERY),
        ("WITH x AS (SELECT 1) SELECT * FROM x", StatementKind.QUERY),
        ("EXPLAIN SELECT 1", StatementKind.QUERY),
        ("DESCRIBE robots", StatementKind.QUERY),
        ("SET TRANSACTION ISOLATION LEVEL READ COMMITTED", StatementKind.QUERY),
        ("INSERT INTO robots (id) VALUES (GEN_ID())", StatementKind.NON_QUERY),
        ("UPDATE robots SET year = 1", StatementKind.NON_QUERY),
        ("DELETE FROM robots", StatementKind.NON_QUERY),
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


def test_truncate_runs_in_its_own_transaction():
    assert runs_in_own_transaction("TRUNCATE TABLE t")
    assert not runs_in_own_transaction("DELETE FROM t")
