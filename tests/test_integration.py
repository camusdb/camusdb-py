"""End-to-end tests against a CamusDB server. Run with CAMUSDB_TEST_ENDPOINT=http://localhost:5095."""

import datetime as dt
import decimal
import uuid

import pytest

import camusdb
from camusdb import ColumnType, ObjectId, Typed, codes

pytestmark = pytest.mark.integration

UTC = dt.timezone.utc


def test_ping(conn):
    assert conn.ping()


def test_every_type_round_trips(conn, table):
    row = {
        "id": ObjectId.generate(),
        "name": "T-800",
        "year": -(2**62),
        "price": 10.25,
        "score": Typed(9.5, ColumnType.FLOAT32),
        "total": decimal.Decimal("12345678901234567890.123456789"),
        "enabled": True,
        "ref": uuid.uuid4(),
        "payload": b"\x00\xde\xad\xbe\xef",
        "day": dt.date(2026, 5, 1),
        "seen": dt.datetime(2026, 5, 1, 12, 30, 15, 123456, tzinfo=UTC),
        "tags": [1, 2, 3],
    }
    columns = ", ".join(row)
    placeholders = ", ".join("@" + key for key in row)
    cur = conn.execute(f"INSERT INTO {table} ({columns}) VALUES ({placeholders})", row)
    assert cur.rowcount == 1
    conn.commit()

    cur = conn.execute(f"SELECT {columns} FROM {table} WHERE id = @id", {"id": row["id"]})
    assert [d[0] for d in cur.description] == list(row)
    assert cur.description[1][1] == camusdb.STRING
    assert cur.description[2][1] == camusdb.NUMBER
    assert cur.description[9][1] == camusdb.DATETIME
    got = cur.fetchone()
    assert got == (
        str(row["id"]),
        "T-800",
        -(2**62),
        10.25,
        9.5,
        row["total"],
        True,
        row["ref"],
        row["payload"],
        row["day"],
        row["seen"],
        [1, 2, 3],
    )
    assert cur.fetchone() is None


def test_nulls(conn, table):
    conn.execute(
        f"INSERT INTO {table} (id, name, year, ref, tags) VALUES (GEN_ID(), @name, @year, @ref, @tags)",
        {"name": "nothing", "year": None, "ref": None, "tags": None},
    )
    assert conn.execute(f"SELECT year, ref, tags FROM {table}").fetchall() == [(None, None, None)]


def test_returning(conn, table):
    cur = conn.execute(
        f"INSERT INTO {table} (id, name, year) VALUES (GEN_ID(), @name, @year) RETURNING id, year * 2 AS doubled",
        {"name": "R2-D2", "year": 1977},
    )
    assert cur.rowcount == 1
    assert [d[0] for d in cur.description] == ["id", "doubled"]
    generated, doubled = cur.fetchone()
    assert len(generated) == 24
    assert doubled == 3954


def test_commit_and_rollback(endpoint, database, table):
    with camusdb.connect(endpoint=endpoint, database=database) as a:
        a.execute(f"INSERT INTO {table} (id, name) VALUES (GEN_ID(), 'kept')")
        a.commit()
        a.execute(f"INSERT INTO {table} (id, name) VALUES (GEN_ID(), 'dropped')")
        assert a.in_transaction
        a.rollback()
        assert not a.in_transaction
        assert a.execute(f"SELECT name FROM {table}").fetchall() == [("kept",)]


def test_a_failed_statement_aborts_the_transaction(conn, table):
    conn.execute(f"INSERT INTO {table} (id, name) VALUES (GEN_ID(), 'one')")
    with pytest.raises(camusdb.ProgrammingError) as info:
        conn.execute("SELECT * FROM table_that_does_not_exist")
    assert info.value.code == codes.TABLE_DOESNT_EXIST
    with pytest.raises(camusdb.InternalError):
        conn.execute("SELECT 1")
    conn.rollback()
    # The insert was part of the failed transaction.
    assert conn.execute(f"SELECT name FROM {table}").fetchall() == []


def test_duplicate_primary_key_is_an_integrity_error(conn, table):
    oid = ObjectId.generate()
    conn.autocommit = True
    conn.execute(f"INSERT INTO {table} (id, name) VALUES (@id, 'a')", {"id": oid})
    with pytest.raises(camusdb.IntegrityError):
        conn.execute(f"INSERT INTO {table} (id, name) VALUES (@id, 'b')", {"id": oid})


def test_value_too_long_is_a_data_error(conn, table):
    conn.autocommit = True
    with pytest.raises(camusdb.DataError) as info:
        conn.execute(f"INSERT INTO {table} (id, name) VALUES (GEN_ID(), @name)", {"name": "x" * 65})
    assert info.value.code == codes.VALUE_TOO_LONG


def test_syntax_error(conn):
    with pytest.raises(camusdb.ProgrammingError):
        conn.execute("SELEC 1")


def test_transaction_block_with_options(conn, table):
    conn.autocommit = True
    with conn.transaction(isolation_level="read committed", locking="optimistic"):
        conn.execute(f"INSERT INTO {table} (id, name) VALUES (GEN_ID(), 'opt')")
        assert conn.in_transaction
    assert not conn.in_transaction
    assert conn.execute(f"SELECT name FROM {table}").fetchall() == [("opt",)]


def test_run_transaction(conn, table):
    conn.autocommit = True

    def transfer(c):
        c.execute(f"INSERT INTO {table} (id, name, year) VALUES (GEN_ID(), 'acct', 100)")
        return c.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]

    assert camusdb.run_transaction(conn, transfer) == 1


def test_executemany(conn, table):
    cur = conn.cursor()
    cur.executemany(
        f"INSERT INTO {table} (id, name, year) VALUES (GEN_ID(), @name, @year)",
        [{"name": f"r{i}", "year": 1900 + i} for i in range(5)],
    )
    assert cur.rowcount == 5
    conn.commit()
    assert conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone() == (5,)


def test_stream(conn, table):
    conn.autocommit = True
    cur = conn.cursor()
    cur.executemany(
        f"INSERT INTO {table} (id, name, year) VALUES (GEN_ID(), @name, @year)",
        [{"name": f"r{i}", "year": i} for i in range(50)],
    )
    rows = list(
        cur.stream(f"SELECT name, year FROM {table} WHERE year >= @min ORDER BY year", {"min": 10})
    )
    assert len(rows) == 40
    assert rows[0] == ("r10", 10)
    assert [d[0] for d in cur.description] == ["name", "year"]
    assert cur.rowcount == 40


def test_stream_returning(conn, table):
    conn.autocommit = True
    rows = list(
        conn.cursor().stream(
            f"INSERT INTO {table} (id, name) VALUES (GEN_ID(), 'a'), (GEN_ID(), 'b') RETURNING name"
        )
    )
    assert sorted(rows) == [("a",), ("b",)]


def test_insert_route(conn, table):
    cur = conn.cursor()
    assert (
        cur.insert(table, {"id": ObjectId.generate(), "name": "typed", "year": 2001, "tags": [7]})
        == 1
    )
    conn.commit()
    assert conn.execute(f"SELECT name, year, tags FROM {table}").fetchone() == ("typed", 2001, [7])


def test_show_and_explain_use_the_query_route(conn, table):
    tables = [row[0] for row in conn.execute("SHOW TABLES").fetchall()]
    assert table in tables
    assert conn.execute(f"EXPLAIN SELECT * FROM {table}").description is not None
    assert conn.execute(f"DESCRIBE {table}").fetchall()


def test_ddl_inside_a_transaction_does_not_open_one(conn, table):
    conn.execute(f"CREATE INDEX idx_{table}_year ON {table} (year)")
    assert not conn.in_transaction


def test_truncate(conn, table):
    conn.autocommit = True
    conn.execute(f"INSERT INTO {table} (id, name) VALUES (GEN_ID(), 'gone')")
    conn.execute(f"TRUNCATE {table}")
    assert conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone() == (0,)


def test_branches(endpoint, database, table):
    branch = f"{database}_branch"
    with camusdb.connect(endpoint=endpoint, database=database, autocommit=True) as conn:
        conn.execute(f"INSERT INTO {table} (id, name) VALUES (GEN_ID(), 'parent row')")
        conn.create_branch(branch, if_not_exists=True)
        try:
            assert branch in [b.database for b in conn.show_branches()]
            conn.database = branch
            assert conn.execute(f"SELECT name FROM {table}").fetchall() == [("parent row",)]
            ancestors = conn.show_ancestors()
            assert database in [b.database for b in ancestors]
        finally:
            conn.drop_database(branch)


def test_create_database_if_not_exists_is_idempotent(conn):
    conn.create_database(if_not_exists=True)
    with pytest.raises(camusdb.ProgrammingError) as info:
        conn.create_database()
    assert info.value.code == codes.DATABASE_ALREADY_EXISTS


@pytest.mark.anyio
async def test_async_round_trip(endpoint, database, table):
    import camusdb.aio

    async with await camusdb.aio.connect(endpoint=endpoint, database=database) as conn:
        cur = conn.cursor()
        await cur.executemany(
            f"INSERT INTO {table} (id, name, year) VALUES (GEN_ID(), @name, @year)",
            [{"name": "a", "year": 1}, {"name": "b", "year": 2}],
        )
        await conn.commit()
        cur = await conn.execute(f"SELECT name, year FROM {table} ORDER BY year")
        assert [row async for row in cur] == [("a", 1), ("b", 2)]

        streamed = [
            row async for row in conn.cursor().stream(f"SELECT name FROM {table} ORDER BY year")
        ]
        assert streamed == [("a",), ("b",)]

        with pytest.raises(camusdb.ProgrammingError):
            await conn.execute("SELECT * FROM missing_table")
        with pytest.raises(camusdb.InternalError):
            await conn.execute("SELECT 1")
        await conn.rollback()

        async def count(c):
            return (await (await c.execute(f"SELECT COUNT(*) FROM {table}")).fetchone())[0]

        conn.autocommit = True
        assert await camusdb.aio.run_transaction(conn, count) == 2
        assert await conn.ping()


def test_node_stats(conn):
    stats = conn.node_stats(conn.endpoints[0])
    assert stats.address == conn.endpoints[0]
    assert stats.node.version
    assert stats.data_volume is not None
    assert stats.data_volume.total_bytes > 0
    assert stats.cpu.processor_count > 0


def test_backups(conn):
    """A full backup, an incremental on it, and the chain. Needs a server with kahuna.backup_dir."""
    try:
        full = conn.backups.take_full()
    except camusdb.Error as exc:
        if exc.code == codes.BACKUP_NOT_CONFIGURED:
            pytest.skip("the server has no kahuna.backup_dir")
        # With authentication off, the server accepts backup calls from loopback only. Under Docker
        # port mapping, the server does not see the client as loopback.
        if exc.code == codes.INSUFFICIENT_PRIVILEGE:
            pytest.skip(f"the server refuses backup calls from this client: {exc.message}")
        raise
    assert full.type == "Full"
    assert full.created_at is not None

    incremental = conn.backups.take_incremental(full.backup_id)
    assert incremental.parent_backup_id == full.backup_id or incremental.was_substituted

    ids = [backup.backup_id for backup in conn.backups.list()]
    assert full.backup_id in ids
    assert incremental.backup_id in ids

    chain = conn.backups.chain(incremental.backup_id)
    assert chain[-1].backup_id == incremental.backup_id
    assert chain[0].min_recoverable_physical_ms is not None

    preview = conn.backups.preview_garbage_collection()
    assert preview.applied is False
