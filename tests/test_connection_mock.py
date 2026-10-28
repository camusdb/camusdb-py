"""Connection behavior against a fake server: what the driver sends, and how it reacts."""

import json

import httpx
import pytest

import camusdb
from camusdb import codes
from camusdb._auth import TOKENS
from camusdb._endpoints import EndpointPool


class FakeServer:
    def __init__(self):
        self.requests: list[tuple[str, dict, dict]] = []
        self.handlers = {}
        self.txn = 0

    def __call__(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content) if request.content else {}
        self.requests.append((request.url.path, body, dict(request.headers)))
        handler = self.handlers.get(request.url.path)
        if handler is not None:
            result = handler(body, request)
            if isinstance(result, httpx.Response):
                return result
            return httpx.Response(200, json=result)
        return httpx.Response(200, json=self.default(request.url.path))

    def default(self, path):
        if path == "/start-transaction":
            self.txn += 1
            return {"status": "ok", "txnIdPT": 1000 + self.txn, "txnIdCounter": 0}
        if path == "/execute-sql-query":
            return {"status": "ok", "columns": [{"name": "x", "type": 2}], "rows": [[1], [2]]}
        if path == "/execute-sql-non-query":
            return {"status": "ok", "rows": 3}
        return {"status": "ok"}

    def paths(self):
        return [path for path, _, _ in self.requests]


def failure(code, message="failed", status=500):
    return httpx.Response(status, json={"status": "failed", "code": code, "message": message})


@pytest.fixture
def server():
    return FakeServer()


@pytest.fixture
def connect(server):
    created = []

    def factory(**kwargs):
        kwargs.setdefault("endpoint", f"http://localhost:{9000 + len(created)}")
        kwargs.setdefault("database", "test")
        conn = camusdb.connect(**kwargs)
        conn._transport._client = httpx.Client(transport=httpx.MockTransport(server))
        created.append(conn)
        return conn

    yield factory
    for conn in created:
        conn.close()


def test_the_first_statement_begins_a_transaction(server, connect):
    conn = connect()
    rows = conn.execute("SELECT x FROM t WHERE y = @y", {"y": 5}).fetchall()
    assert rows == [(1,), (2,)]
    assert server.paths() == ["/start-transaction", "/execute-sql-query"]
    _, body, _ = server.requests[1]
    assert body == {
        "databaseName": "test",
        "sql": "SELECT x FROM t WHERE y = @y",
        "parameters": {"@y": {"type": 2, "longValue": 5}},
        "txnIdPT": 1001,
        "txnIdCounter": 0,
    }
    conn.commit()
    assert server.paths()[-1] == "/commit-transaction"
    assert not conn.in_transaction


def test_autocommit_sends_the_options_with_each_write(server, connect):
    conn = connect(autocommit=True, isolation_level="ReadCommitted", locking="optimistic")
    cur = conn.execute("UPDATE t SET x = 1")
    assert cur.rowcount == 3
    assert cur.description is None
    _, body, _ = server.requests[0]
    assert body["isolationLevel"] == "ReadCommitted"
    assert body["locking"] == "Optimistic"
    assert "txnIdPT" not in body
    # A read carries no autocommit options.
    conn.execute("SELECT 1")
    _, body, _ = server.requests[1]
    assert "isolationLevel" not in body


def test_ddl_goes_to_its_route_and_opens_no_transaction(server, connect):
    conn = connect()
    cur = conn.execute("CREATE TABLE t (id OID PRIMARY KEY NOT NULL)")
    assert server.paths() == ["/execute-sql-ddl"]
    assert cur.rowcount == -1
    assert not conn.in_transaction


def test_truncate_is_refused_in_a_transaction(server, connect):
    conn = connect()
    conn.execute("SELECT 1")
    with pytest.raises(camusdb.ProgrammingError) as info:
        conn.execute("TRUNCATE t")
    assert info.value.code == codes.STATEMENT_NOT_ALLOWED_IN_TRANSACTION
    # The refusal happened before anything was sent, so the transaction is still good.
    conn.execute("SELECT 1")


def test_a_failed_statement_aborts_the_transaction(server, connect):
    conn = connect()
    server.handlers["/execute-sql-query"] = lambda body, req: failure(
        "CADB0011", "Table 'x' doesn't exist"
    )
    with pytest.raises(camusdb.ProgrammingError):
        conn.execute("SELECT * FROM x")
    with pytest.raises(camusdb.InternalError, match="rollback"):
        conn.execute("SELECT 1")
    # The server discarded the transaction, so a failed rollback is not an error.
    server.handlers["/rollback-transaction"] = lambda body, req: failure(
        "CADB0400", "Unknown transaction"
    )
    conn.rollback()
    del server.handlers["/execute-sql-query"]
    assert conn.execute("SELECT 1").fetchone() == (1,)


def test_commit_of_a_failed_transaction_is_an_error(server, connect):
    conn = connect()
    server.handlers["/execute-sql-non-query"] = lambda body, req: failure("CADB0300")
    with pytest.raises(camusdb.IntegrityError):
        conn.execute("INSERT INTO t (a) VALUES (1)")
    with pytest.raises(camusdb.InternalError, match="nothing was committed"):
        conn.commit()
    assert not conn.in_transaction


def test_an_unresolved_commit_is_sent_again(server, connect, monkeypatch):
    monkeypatch.setattr("camusdb.connection.finalize_delay", lambda attempt: 0)
    answers = iter([failure("CADB0509"), failure("CADB0509"), {"status": "ok"}])
    server.handlers["/commit-transaction"] = lambda body, req: next(answers)
    conn = connect()
    conn.execute("SELECT 1")
    conn.commit()
    assert server.paths().count("/commit-transaction") == 3


def test_the_transaction_block(server, connect):
    conn = connect(autocommit=True)
    with conn.transaction(read_only=True):
        conn.execute("SELECT 1")
    assert server.paths() == ["/start-transaction", "/execute-sql-query", "/commit-transaction"]
    assert server.requests[0][1]["transactionMode"] == "ReadOnly"

    with pytest.raises(ZeroDivisionError), conn.transaction():
        conn.execute("SELECT 1")
        raise ZeroDivisionError
    assert server.paths()[-1] == "/rollback-transaction"


def test_run_transaction_replays_a_conflict(server, connect, monkeypatch):
    monkeypatch.setattr("camusdb.connection.retry_delay", lambda attempt: 0)
    answers = iter([failure("CADB0502"), {"status": "ok", "rows": 1}])
    server.handlers["/execute-sql-non-query"] = lambda body, req: next(answers)
    conn = connect(autocommit=True)

    calls = []

    def work(c):
        calls.append(1)
        return c.execute("UPDATE t SET a = 1").rowcount

    assert camusdb.run_transaction(conn, work) == 1
    assert len(calls) == 2


def test_the_connection_context_manager_commits_and_closes(server, connect):
    conn = connect()
    with conn:
        conn.execute("INSERT INTO t (a) VALUES (1)")
    assert server.paths()[-1] == "/commit-transaction"
    assert conn.closed
    with pytest.raises(camusdb.InterfaceError):
        conn.cursor()


def test_returning_rows_come_back_from_a_write(server, connect):
    server.handlers["/execute-sql-non-query"] = lambda body, req: {
        "status": "ok",
        "rows": 1,
        "columns": [{"name": "id", "type": 1}],
        "returningRows": [["6ae24c3a5c7d131d88e2ed42"]],
    }
    conn = connect(autocommit=True)
    cur = conn.execute("INSERT INTO t (id) VALUES (GEN_ID()) RETURNING id")
    assert cur.rowcount == 1
    assert cur.description[0][:2] == ("id", camusdb.ColumnType.ID)
    assert cur.description[0][1] == camusdb.ROWID
    assert cur.fetchone() == ("6ae24c3a5c7d131d88e2ed42",)


def test_fetch_without_rows_is_an_error(server, connect):
    conn = connect(autocommit=True)
    cur = conn.execute("UPDATE t SET a = 1")
    with pytest.raises(camusdb.ProgrammingError):
        cur.fetchone()


def test_row_factories(server, connect):
    conn = connect(autocommit=True, row_factory=camusdb.rows.dict_row)
    assert conn.execute("SELECT x FROM t").fetchall() == [{"x": 1}, {"x": 2}]
    cur = conn.cursor(row_factory=camusdb.rows.namedtuple_row)
    assert cur.execute("SELECT x FROM t").fetchone().x == 1


def test_fetchmany_and_iteration(server, connect):
    conn = connect(autocommit=True)
    cur = conn.execute("SELECT x FROM t")
    assert cur.fetchmany(1) == [(1,)]
    assert list(cur) == [(2,)]
    assert cur.fetchone() is None
    cur.scroll(0, "absolute")
    assert cur.fetchall() == [(1,), (2,)]


def test_stream(server, connect):
    lines = [
        {"status": "ok", "columns": [{"name": "x", "type": 2}]},
        [1],
        [2],
        {"status": "ok", "total": 2},
    ]
    server.handlers["/execute-sql-query-stream"] = lambda body, req: httpx.Response(
        200, content="\n".join(json.dumps(line) for line in lines) + "\n"
    )
    conn = connect(autocommit=True)
    cur = conn.cursor()
    assert list(cur.stream("SELECT x FROM t")) == [(1,), (2,)]
    assert cur.description[0][0] == "x"
    assert cur.rowcount == 2
    assert server.requests[0][2]["accept"] == "application/x-ndjson"


def test_a_failure_after_rows_began_is_raised_from_the_iteration(server, connect):
    lines = [
        {"status": "ok", "columns": [{"name": "x", "type": 2}]},
        [1],
        {"status": "failed", "code": "CADB0502", "message": "conflict"},
    ]
    server.handlers["/execute-sql-query-stream"] = lambda body, req: httpx.Response(
        200, content="\n".join(json.dumps(line) for line in lines)
    )
    conn = connect(autocommit=True)
    rows = conn.cursor().stream("SELECT x FROM t")
    assert next(rows) == (1,)
    with pytest.raises(camusdb.SerializationFailure):
        next(rows)


def test_insert_route(server, connect):
    conn = connect(autocommit=True)
    server.handlers["/insert"] = lambda body, req: {"status": "ok", "rows": 1}
    assert conn.cursor().insert("robots", {"name": "T-800", "year": 1984}) == 1
    _, body, _ = server.requests[0]
    assert body == {
        "databaseName": "test",
        "tableName": "robots",
        "values": {
            "name": {"type": 3, "strValue": "T-800"},
            "year": {"type": 2, "longValue": 1984},
        },
    }


class TestAuthentication:
    def setup_method(self):
        TOKENS._tokens.clear()

    def test_one_login_is_shared_by_connections(self, server, connect):
        server.handlers["/login"] = lambda body, req: {
            "status": "ok",
            "token": "tok-1",
            "expiresInSeconds": 900,
        }
        endpoint = "http://localhost:9100"
        a = connect(endpoint=endpoint, user="app", password="secret", autocommit=True)
        b = connect(endpoint=endpoint, user="app", password="secret", autocommit=True)
        a.execute("SELECT 1")
        b.execute("SELECT 1")
        assert server.paths().count("/login") == 1
        assert server.requests[0][1] == {"user": "app", "password": "secret"}
        assert "authorization" not in server.requests[0][2]
        assert all(
            headers["authorization"] == "Bearer tok-1" for _, _, headers in server.requests[1:]
        )
        assert a.access_token == "tok-1"

    def test_a_rejected_token_is_renewed_and_the_request_replayed(self, server, connect):
        tokens = iter(["old", "new"])
        server.handlers["/login"] = lambda body, req: {"status": "ok", "token": next(tokens)}
        server.handlers["/execute-sql-query"] = lambda body, req: (
            failure("CADB0516", status=401)
            if req.headers["authorization"] == "Bearer old"
            else server.default("/execute-sql-query")
        )
        conn = connect(
            endpoint="http://localhost:9101", user="app", password="secret", autocommit=True
        )
        assert conn.execute("SELECT 1").fetchall() == [(1,), (2,)]
        assert server.paths() == ["/login", "/execute-sql-query", "/login", "/execute-sql-query"]

    def test_a_static_token_is_not_renewed(self, server, connect):
        server.handlers["/execute-sql-query"] = lambda body, req: failure("CADB0516", status=401)
        conn = connect(endpoint="http://localhost:9102", access_token="static", autocommit=True)
        with pytest.raises(camusdb.OperationalError) as info:
            conn.execute("SELECT 1")
        assert info.value.code == codes.AUTHENTICATION_FAILED
        assert "/login" not in server.paths()

    def test_no_credentials_send_no_header(self, server, connect):
        conn = connect(autocommit=True)
        conn.execute("SELECT 1")
        assert "authorization" not in server.requests[0][2]


def test_an_unreachable_endpoint_is_paused(server, connect):
    conn = connect(endpoint="http://localhost:9200,http://localhost:9201", autocommit=True)

    def refuse(request):
        if request.url.port == 9200:
            raise httpx.ConnectError("refused", request=request)
        return server(request)

    conn._transport._client = httpx.Client(transport=httpx.MockTransport(refuse))
    pool = EndpointPool.for_endpoints(("http://localhost:9200", "http://localhost:9201"))
    pool._rotation = iter(range(100))  # start the rotation at the first endpoint
    with pytest.raises(camusdb.OperationalError) as info:
        conn.execute("SELECT 1")
    assert info.value.code == codes.ENDPOINT_UNREACHABLE
    assert pool.is_quarantined("http://localhost:9200")
    for _ in range(3):
        conn.execute("SELECT 1")
