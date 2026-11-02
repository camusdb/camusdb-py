"""Connection behavior against a fake server: what the driver sends, and how it reacts."""

import hashlib
import json
import threading
import time

import httpx
import pytest

import camusdb
import camusdb._auth
from camusdb import codes
from camusdb._auth import TOKENS, Token
from camusdb._config import build_config
from camusdb._endpoints import EndpointPool
from camusdb._transport import LineSplitter


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
        "jsonbAsText": True,
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


def test_ddl_does_not_ask_for_jsonb_text(server, connect):
    conn = connect(autocommit=True)
    conn.execute("CREATE TABLE t (id OID PRIMARY KEY NOT NULL)")
    _, body, _ = server.requests[0]
    assert "jsonbAsText" not in body


def test_a_streamed_upsert_without_returning_runs_as_a_write(server, connect):
    conn = connect(autocommit=True, isolation_level="ReadCommitted")
    cur = conn.cursor()
    assert list(cur.stream("UPSERT INTO counters (k, n) VALUES ('a', 1)")) == []
    assert cur.rowcount == 3
    assert server.paths() == ["/execute-sql-non-query"]
    _, body, _ = server.requests[0]
    assert body["isolationLevel"] == "ReadCommitted"


def test_a_streamed_upsert_with_returning_carries_the_autocommit_options(server, connect):
    lines = [
        {"status": "ok", "columns": [{"name": "n", "type": 2}]},
        [7],
        {"status": "ok", "total": 1},
    ]
    server.handlers["/execute-sql-query-stream"] = lambda body, req: httpx.Response(
        200, content="\n".join(json.dumps(line) for line in lines)
    )
    conn = connect(autocommit=True, isolation_level="ReadCommitted")
    rows = list(conn.cursor().stream("UPSERT INTO counters (k, n) VALUES ('a', 7) RETURNING n"))
    assert rows == [(7,)]
    _, body, _ = server.requests[0]
    assert body["isolationLevel"] == "ReadCommitted"
    assert body["jsonbAsText"] is True


def test_jsonb_round_trip(server, connect):
    server.handlers["/execute-sql-query"] = lambda body, req: {
        "status": "ok",
        "columns": [{"name": "doc", "type": 13, "jsonbAsText": True}],
        "rows": [['{"kind": "login"}'], ["null"], [None]],
    }
    conn = connect(autocommit=True)
    cur = conn.execute("SELECT doc FROM events WHERE doc @> @filter", {"filter": {"kind": "login"}})
    assert cur.fetchall() == [('{"kind": "login"}',), ("null",), (None,)]
    assert cur.description[0][1] == camusdb.ColumnType.JSONB
    assert cur.description[0][1] == camusdb.STRING
    _, body, _ = server.requests[0]
    assert body["parameters"]["@filter"] == {"type": 13, "strValue": '{"kind": "login"}'}


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
        TOKENS.clear()

    @staticmethod
    def _age_token(conn, value, fraction, lifetime=100.0):
        """Cache ``value`` for the credentials of ``conn`` as a token at ``fraction`` of its life."""
        auth = conn._transport.auth
        TOKENS.put(auth._key, Token(value, time.monotonic() - fraction * lifetime, lifetime))

    @staticmethod
    def _wait_for_renewals():
        for thread in threading.enumerate():
            if thread.name == "camusdb-token-renewal":
                thread.join(5)

    def test_an_old_token_is_sent_while_a_background_login_replaces_it(self, server, connect):
        server.handlers["/login"] = lambda body, req: {
            "status": "ok",
            "token": "new",
            "expiresInSeconds": 900,
        }
        conn = connect(
            endpoint="http://localhost:9103", user="app", password="secret", autocommit=True
        )
        self._age_token(conn, "old", 0.85)

        conn.execute("SELECT 1")
        self._wait_for_renewals()
        conn.execute("SELECT 1")

        sent = [(path, headers.get("authorization")) for path, _, headers in server.requests]
        assert sent.count(("/login", None)) == 1
        queries = [auth for path, auth in sent if path == "/execute-sql-query"]
        assert queries == ["Bearer old", "Bearer new"]
        assert conn.access_token == "new"

    def test_a_failed_background_renewal_keeps_the_old_token(self, server, connect):
        server.handlers["/login"] = lambda body, req: failure("CADB0516", status=401)
        conn = connect(
            endpoint="http://localhost:9104", user="app", password="secret", autocommit=True
        )
        self._age_token(conn, "old", 0.85)

        conn.execute("SELECT 1")
        self._wait_for_renewals()
        conn.execute("SELECT 1")
        self._wait_for_renewals()

        # The second statement does not start a new login: the retry waits 5 seconds.
        assert server.paths().count("/login") == 1
        queries = [
            headers["authorization"]
            for path, _, headers in server.requests
            if path == "/execute-sql-query"
        ]
        assert queries == ["Bearer old", "Bearer old"]

    def test_an_expired_token_waits_for_the_login(self, server, connect):
        server.handlers["/login"] = lambda body, req: {"status": "ok", "token": "new"}
        conn = connect(
            endpoint="http://localhost:9105", user="app", password="secret", autocommit=True
        )
        self._age_token(conn, "old", 0.96)
        conn.execute("SELECT 1")
        assert server.paths() == ["/login", "/execute-sql-query"]
        assert server.requests[1][2]["authorization"] == "Bearer new"

    def test_a_busy_login_is_sent_again(self, server, connect, monkeypatch):
        monkeypatch.setattr(camusdb._auth, "BUSY_LOGIN_RETRY_DELAYS", (0, 0, 0))
        replies = iter([failure("CADB0518", status=429), failure("CADB0518", status=429)])
        server.handlers["/login"] = lambda body, req: next(
            replies, {"status": "ok", "token": "tok"}
        )
        conn = connect(
            endpoint="http://localhost:9106", user="app", password="secret", autocommit=True
        )
        conn.execute("SELECT 1")
        assert server.paths() == ["/login", "/login", "/login", "/execute-sql-query"]

    def test_a_login_busy_four_times_fails(self, server, connect, monkeypatch):
        monkeypatch.setattr(camusdb._auth, "BUSY_LOGIN_RETRY_DELAYS", (0, 0, 0))
        server.handlers["/login"] = lambda body, req: failure("CADB0518", status=429)
        conn = connect(
            endpoint="http://localhost:9107", user="app", password="secret", autocommit=True
        )
        with pytest.raises(camusdb.Error) as info:
            conn.execute("SELECT 1")
        assert info.value.code == codes.TOO_MANY_AUTH_ATTEMPTS
        assert server.paths() == ["/login"] * 4

    def test_another_refusal_is_not_sent_again(self, server, connect, monkeypatch):
        monkeypatch.setattr(camusdb._auth, "BUSY_LOGIN_RETRY_DELAYS", (0, 0, 0))
        server.handlers["/login"] = lambda body, req: failure("CADB0516", status=401)
        conn = connect(
            endpoint="http://localhost:9108", user="app", password="wrong", autocommit=True
        )
        with pytest.raises(camusdb.Error):
            conn.execute("SELECT 1")
        assert server.paths() == ["/login"]

    @pytest.mark.anyio
    async def test_async_background_renewal_and_busy_login(self, server, monkeypatch):
        import camusdb.aio

        monkeypatch.setattr(camusdb._auth, "BUSY_LOGIN_RETRY_DELAYS", (0, 0, 0))
        replies = iter([failure("CADB0518", status=429)])
        server.handlers["/login"] = lambda body, req: next(
            replies, {"status": "ok", "token": "new", "expiresInSeconds": 900}
        )
        conn = await camusdb.aio.connect(
            endpoint="http://localhost:9109",
            database="test",
            user="app",
            password="secret",
            autocommit=True,
        )
        conn._transport._client = httpx.AsyncClient(transport=httpx.MockTransport(server))
        try:
            self._age_token(conn, "old", 0.85)
            await conn.execute("SELECT 1")
            renewals = list(conn._transport._renewals)
            assert len(renewals) == 1
            await renewals[0]
            await conn.execute("SELECT 1")
        finally:
            await conn.close()

        queries = [
            headers["authorization"]
            for path, _, headers in server.requests
            if path == "/execute-sql-query"
        ]
        assert queries == ["Bearer old", "Bearer new"]
        assert server.paths().count("/login") == 2

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


def test_a_writable_cte_runs_as_a_write_with_the_autocommit_options(server, connect):
    conn = connect(autocommit=True, isolation_level="ReadCommitted")
    conn.execute("WITH x AS (SELECT 1) DELETE FROM t")
    path, body, _ = server.requests[0]
    assert path == "/execute-sql-non-query"
    assert body["isolationLevel"] == "ReadCommitted"


def test_a_malformed_stream_row_is_an_interface_error(server, connect):
    lines = [{"status": "ok", "columns": [{"name": "x", "type": 12}]}, ["12.34abc"]]
    server.handlers["/execute-sql-query-stream"] = lambda body, req: httpx.Response(
        200, content="\n".join(json.dumps(line) for line in lines)
    )
    conn = connect(autocommit=True)
    with pytest.raises(camusdb.InterfaceError, match="malformed"):
        list(conn.cursor().stream("SELECT x FROM t"))


def test_a_malformed_insert_count_is_an_interface_error(server, connect):
    server.handlers["/insert"] = lambda body, req: {"status": "ok", "rows": "many"}
    conn = connect(autocommit=True)
    with pytest.raises(camusdb.InterfaceError, match="malformed"):
        conn.cursor().insert("robots", {"name": "T-800"})


class TestLineSplitter:
    def test_lines_across_chunks(self):
        splitter = LineSplitter()
        assert splitter.feed(b'{"a"') == []
        assert splitter.feed(b": 1}\n\n [1") == [b'{"a": 1}']
        assert splitter.feed(b"]\n[2]\r\n[3") == [b"[1]", b"[2]"]
        assert splitter.finish() == [b"[3"]
        assert splitter.finish() == []

    def test_a_long_line_in_small_chunks_takes_linear_time(self):
        # 8 MB with no newline in 1 KB chunks. The old buffer copied all of it for each chunk, which
        # took about 9 s; a linear buffer takes milliseconds.
        splitter = LineSplitter()
        chunk = b"x" * 1024
        start = time.perf_counter()
        for _ in range(8 * 1024):
            assert splitter.feed(chunk) == []
        assert splitter.finish() == [chunk * 8 * 1024]
        assert time.perf_counter() - start < 2.0


class TestTokenStore:
    def setup_method(self):
        TOKENS.clear()

    def test_the_key_holds_no_plain_hash_of_the_password(self):
        auth = camusdb._auth.AuthState(build_config(None, _CREDENTIALS))
        plain = hashlib.sha256(b"hunter2").hexdigest()
        assert plain not in repr(auth._key)
        assert auth._key == camusdb._auth.AuthState(build_config(None, _CREDENTIALS))._key

    def test_old_credentials_are_evicted(self, monkeypatch):
        monkeypatch.setattr(camusdb._auth, "MAX_CACHED_CREDENTIALS", 3)
        auth = camusdb._auth.AuthState(build_config(None, _CREDENTIALS))
        keys = []
        for i in range(5):
            auth.set_credentials("app", f"password-{i}")
            auth.login_lock()
            auth.store(f"token-{i}", 600)
            keys.append(auth._key)
        assert [TOKENS.get(key) is not None for key in keys] == [False, False, True, True, True]
        assert len(TOKENS._login_locks) <= 3 + 1


_CREDENTIALS = {
    "endpoint": "http://localhost:1",
    "database": "d",
    "user": "app",
    "password": "hunter2",
}
