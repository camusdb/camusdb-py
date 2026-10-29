# CamusDB driver for Python

A Python driver for [CamusDB](https://github.com/camusdb/camusdb). It implements
[PEP 249](https://peps.python.org/pep-0249/) (DB-API 2.0), and it has an asyncio interface in
`camusdb.aio`. It speaks the server's REST protocol through [httpx](https://www.python-httpx.org/).

```shell
pip install camusdb-py
pip install 'camusdb-py[http2]'   # to use HTTP/2
```

The package on PyPI is `camusdb-py`, and the module that you import is `camusdb`. Python 3.10 or
later is necessary.

## Quick start

```python
import camusdb

with camusdb.connect("Endpoint=http://localhost:5095;Database=test") as conn:
    conn.execute("""
        CREATE TABLE robots (
            id OID PRIMARY KEY NOT NULL,
            name STRING NOT NULL,
            year INT64
        )
    """)
    conn.execute(
        "INSERT INTO robots (id, name, year) VALUES (GEN_ID(), @name, @year)",
        {"name": "T-800", "year": 1984},
    )
    conn.commit()

    for name, year in conn.execute(
        "SELECT name, year FROM robots WHERE year > @year", {"year": 1980}
    ):
        print(name, year)
```

The database must exist before you use it. Create it one time, for example when the application
starts:

```python
conn.create_database(if_not_exists=True)
```

## Connecting

`connect()` takes a connection string, keyword arguments, or both. A keyword argument wins over the
same key in the string.

```python
conn = camusdb.connect(endpoint="http://localhost:5095", database="test", timeout=30)

conn = camusdb.connect("Endpoint=http://localhost:5095;Database=test;User=app;Password=secret")
```

The connection string is the one that the .NET driver uses, so one string can serve both drivers.
Keys are not case-sensitive. A value ends at the next `;`. To keep a `;` or spaces in a value, put
the value in quotes. To include the quote character, write it two times:
`Password='pa;ss''word'` gives `pa;ss'word`. A repeated key is an error. Use
`camusdb.redact_connection_string(text)` to log a string without its secrets.

| Keyword | Connection-string key | Default | Description |
| --- | --- | --- | --- |
| `endpoint` | `Endpoint` | (required) | The REST URL of a node. A list, or a comma-separated string, names several nodes. |
| `database` | `Database` | (required) | The database that statements use. |
| `user`, `password` | `User` (`UserId`, `Uid`, `Username`), `Password` (`Pwd`) | none | The credentials. See [Authentication](#authentication). |
| `access_token` | `AccessToken` | none | A bearer token from elsewhere. The driver sends it as it is and never renews it. |
| `timeout` | `Timeout` | `10` | The time limit of each request, in seconds. |
| `autocommit` | `Autocommit` | `False` | See [Transactions](#transactions). |
| `isolation_level` | `IsolationLevel` | server default | `"Serializable"` or `"ReadCommitted"`. |
| `read_only` | `TransactionMode` (`ReadWrite`, `ReadOnly`) | server default | Whether transactions are read-only. |
| `locking` | `Locking` | server default | `"Pessimistic"` or `"Optimistic"`. |
| `http2` | `HttpVersion` (`1.1`, `2`, `3`) | `False` | Use HTTP/2 on an `https://` endpoint. `HttpVersion=3` also gives HTTP/2, because httpx has no HTTP/3. |
| `token_lifetime` | `TokenLifetime` | `600` | The lifetime of a token, in seconds, when the server reports none. |
| `allow_insecure_credentials` | `AllowInsecureCredentials` | `False` | See [TLS](#tls). |
| `backup_endpoint` | `BackupEndpoint` | the next node in rotation | The node that gets the backup calls. See [Backups](#backups). |
| `backup_timeout` | `BackupTimeout` | `300` | The time limit of each backup call, in seconds. |
| `row_factory` | | `tuple_row` | See [Rows](#rows). |

The driver accepts the .NET keys that tune features it does not have (`MaxAutoPrepare`,
`ChannelPoolSize`, `RoutingMode` and the others), and these keys have no effect. `Protocol=grpc`
is an error, because this driver speaks REST only.

`connect()` sends nothing to the server. The first statement sends the first request.

### Several nodes

```python
conn = camusdb.connect(endpoint="http://a:5095,http://b:5095,http://c:5095", database="test")
```

The driver sends statements to the nodes in rotation. A transaction stays on the node where it
began. When a node does not answer, the driver skips it for 30 seconds and raises an
`OperationalError` with the code `CADB0001`. The driver does not send that request again, because
it cannot know whether the server ran it. The rotation and the node health are shared by every
connection in the process that names the same nodes.

## Parameters

Placeholders are written `@name`, as the server reads them. Give the values in a mapping. A key can
be written with or without the `@`:

```python
cur.execute(
    "SELECT * FROM robots WHERE year = @year AND name = @name", {"year": 1984, "@name": "T-800"}
)
```

The driver never puts a value into the SQL text. Each value travels as a typed wire object, and the
server binds it. `camusdb.paramstyle` is `"named"`.

The type of a value comes from its Python type:

| Python | CamusDB | Notes |
| --- | --- | --- |
| `None` | `NULL` | |
| `bool` | `BOOL` | |
| `int` | `INT64` | A value outside the 64-bit range is a `DataError`. |
| `float` | `FLOAT64` | NaN and infinity are a `DataError`, because JSON cannot hold them. |
| `decimal.Decimal` | `NUMERIC` | Every digit travels as text. |
| `str` | `STRING` | A string also binds to an `OID` column. |
| `bytes`, `bytearray`, `memoryview` | `BYTES` | |
| `datetime.datetime` | `DATETIME` | Sent as UTC. A naive datetime is taken as UTC. |
| `datetime.date` | `DATE` | |
| `uuid.UUID` | `UUID` | |
| `camusdb.ObjectId` | `OID` | |
| `list`, `tuple` | `ARRAY(T)` | `T` comes from the first element that is not `None`. A `Decimal` element travels as `FLOAT64`, because the server has no `ARRAY(NUMERIC)`. |

For a type that Python cannot express, wrap the value in `camusdb.Typed`:

```python
from camusdb import ColumnType, Typed

cur.execute(
    "INSERT INTO m (id, score, tags) VALUES (GEN_ID(), @score, @tags)",
    {
        "score": Typed(9.5, ColumnType.FLOAT32),
        "tags": Typed(
            [], ColumnType.ARRAY, element_type=ColumnType.INT64
        ),  # an empty array needs a type
    },
)
cur.execute(
    "INSERT INTO m (total) VALUES (@total)",
    {"total": Typed("12345678901234567890.123456789", ColumnType.NUMERIC)},
)
```

`camusdb.ObjectId.generate()` makes a new object id on the client, as `GEN_ID()` does on the server.

## Results

Results are buffered: every row is in memory when `execute()` returns. `execute()` returns the
cursor, so calls can be chained.

| CamusDB | Python |
| --- | --- |
| `OID` | `str` |
| `STRING` | `str` |
| `INT64` | `int` |
| `FLOAT64` | `float` |
| `FLOAT32` | `float`: the stored single-precision value, widened exactly |
| `NUMERIC` | `decimal.Decimal` |
| `BOOL` | `bool` |
| `BYTES` | `bytes` |
| `DATE` | `datetime.date` |
| `DATETIME` | `datetime.datetime` in UTC, with microsecond precision |
| `UUID` | `uuid.UUID` |
| `ARRAY(T)` | `list` |

`cursor.description` has one 7-item tuple for each column. The second item is the
`camusdb.ColumnType` of the column. It compares equal to the PEP 249 type objects
(`camusdb.STRING`, `NUMBER`, `DATETIME`, `BINARY` and `ROWID`). `cursor.rowcount` is the number of
rows that a query returned or that a write changed. It is `-1` for DDL.

### RETURNING

A write with a `RETURNING` list returns its rows, as a query does:

```python
cur = conn.execute(
    "INSERT INTO robots (id, name) VALUES (GEN_ID(), @name) RETURNING id", {"name": "R2-D2"}
)
(new_id,) = cur.fetchone()
```

`cursor.lastrowid` is always `None`. Use `RETURNING` to read a generated key.

### Rows

A row is a tuple by default. A row factory changes that, for a connection or for one cursor:

```python
from camusdb.rows import dict_row, namedtuple_row

conn = camusdb.connect(..., row_factory=dict_row)
conn.execute("SELECT id, name FROM robots").fetchone()  # {'id': '6ae2...', 'name': 'T-800'}

cur = conn.cursor(row_factory=namedtuple_row)
```

### Streaming large results

`cursor.stream()` gives the rows while they arrive from the network, so a large result never sits in
memory all at once:

```python
cur = conn.cursor()
for row in cur.stream("SELECT * FROM events WHERE day > @day", {"day": datetime.date(2026, 1, 1)}):
    handle(row)
```

`cursor.description` is set when the first row arrives. Read the iterator to its end, or close it,
to release the HTTP connection. Two differences from `execute()`:

- The server cannot retry a serialization conflict that occurs after rows began to arrive. The
  conflict is raised from the iteration instead.
- The time limit applies to each wait for data, not to the whole read.

A write with `RETURNING` streams its rows too. A write without `RETURNING` runs as `execute()` does.

### Typed insert

`cursor.insert()` inserts one row through the server's typed insert route, with no SQL text:

```python
conn.cursor().insert(
    "robots", {"id": camusdb.ObjectId.generate(), "name": "HAL 9000", "year": 1968}
)
```

## Transactions

The driver follows PEP 249. The first statement begins a transaction, and `commit()` or
`rollback()` ends it. The next statement begins a new one. `close()` rolls back an open
transaction.

```python
conn.execute("UPDATE accounts SET balance = balance - 30 WHERE id = @id", {"id": a})
conn.execute("UPDATE accounts SET balance = balance + 30 WHERE id = @id", {"id": b})
conn.commit()
```

As a context manager, a connection commits when the block ends, rolls back when the block raises,
and then closes.

With `autocommit=True`, each statement runs in its own short transaction, which the server begins
and commits. Use `transaction()` for a block of statements in one transaction. The block begins the
transaction at once, commits when it ends and rolls back when it raises:

```python
conn.autocommit = True
with conn.transaction(isolation_level="ReadCommitted", locking="optimistic"):
    conn.execute(...)
    conn.execute(...)
```

`transaction()` works in both modes. Transactions do not nest: with a transaction open,
`transaction()` raises `ProgrammingError`.

Some rules come from the server:

- **A failed statement ends the transaction.** The server discards the transaction when one of its
  statements fails. The driver then refuses the next statement with an `InternalError` until you
  call `rollback()`. A `commit()` of such a transaction raises an `InternalError`, because nothing
  was committed.
- **Schema changes commit on their own.** `CREATE TABLE`, `CREATE INDEX` and the other DDL
  statements do not begin a transaction. A `ROLLBACK` does not undo them.
- **`TRUNCATE` is refused in a transaction**, with the code `CADB0538`. Run it with
  `autocommit=True`.
- **A commit whose outcome is not known yet** (`CADB0509`) is sent again by `commit()`, up to 10
  times with a back-off, on the same transaction. It is never replayed from the start, because the
  commit can already be durable.

### Isolation and locking

| Setting | Values | Default |
| --- | --- | --- |
| `isolation_level` | `IsolationLevel.SERIALIZABLE`, `IsolationLevel.READ_COMMITTED` | server default (serializable) |
| `read_only` | `True`, `False` | server default (read-write) |
| `locking` | `Locking.PESSIMISTIC`, `Locking.OPTIMISTIC` | server default (pessimistic) |

Set them in `connect()`, as connection attributes, or for one `transaction()` block. A string such
as `"read committed"` also works. A connection attribute applies to the transactions that begin
after you set it, and to autocommit writes. You cannot change it while a transaction is open.

Optimistic locking takes no write locks. The server checks for conflicts at commit, and the loser
gets a `SerializationFailure`. Serializable plus optimistic still takes the predicate locks, so it
is free of phantoms. Read committed plus optimistic takes no locks, and phantoms can occur.

### Retries

When two serializable transactions conflict, the server aborts one of them. That transaction must
be replayed from its start, with all of its reads and writes. One statement must not be retried by
itself. These codes are conflicts, and the driver raises them as `camusdb.SerializationFailure`:

| Code | Meaning |
| --- | --- |
| `CADB0502` | A lock conflict. The server aborted the transaction when it asked for the lock. |
| `CADB0504` | The retry budget is exhausted. Nothing was written. |
| `CADB0505` | The transaction was open for longer than the server permits. |

`camusdb.run_transaction()` runs a function in a transaction, and replays it after a conflict:

```python
def transfer(conn):
    (balance,) = conn.execute("SELECT balance FROM accounts WHERE id = @id", {"id": a}).fetchone()
    if balance < 30:
        raise ValueError("insufficient funds")
    conn.execute("UPDATE accounts SET balance = @b WHERE id = @id", {"b": balance - 30, "id": a})


camusdb.run_transaction(conn, transfer, max_attempts=5)
```

The function must do all of its work again on each attempt. Any error that is not a conflict is
raised at once. The back-off between attempts is min(20 ms × 2^attempt, 400 ms), with 25% jitter.
`run_transaction()` begins its own transaction, so with `autocommit=False` call it when no
transaction is open.

To write your own loop, use `camusdb.is_retryable(exc)`. It also knows the conflicts that the server
reports in the message text only.

## Authentication

Authentication is off on the server by default. Then a connection without credentials sends no
`Authorization` header. Against a server with `CAMUSDB_AUTH_ENABLED=true`, give credentials:

```python
conn = camusdb.connect(
    endpoint="https://db.example:5095", database="shop", user="app", password=secret
)
```

The driver exchanges the password for a short-lived bearer token (`POST /login`). Each statement then
sends the token, not the password.

- **One login for each set of credentials.** The token is cached for the process, for each set of
  nodes, user and password. All connections with the same credentials use one login. This is
  important: the server checks a password slowly on purpose, and it permits 20 logins for each
  account each minute.
- **Renewal.** At 80% of the token's lifetime, the next statement logs in again before it is sent.
  If that login fails, the driver continues to send the old token until 95% of its lifetime.
- **A rejected token** (`CADB0516`), for example after a password change or a server restart, is
  discarded. The driver logs in again and sends the statement one more time. The rejected request did
  not run, so this is safe.
- A token given with `access_token` is never renewed. A rejection of it raises `OperationalError`
  with the code `CADB0516`.

To keep the password out of the settings, log in on the open connection:

```python
conn = camusdb.connect(endpoint="https://db.example:5095", database="shop")
token = conn.login("app", secrets.get("camus-password"))
...
conn.logout()  # revokes the token on the server
```

`conn.access_token` is the token that statements send now.

Users and grants are SQL statements. Bind a password as a parameter, so that it does not get into
the query logs:

```python
conn.execute("CREATE USER app IDENTIFIED BY @password", {"password": "app-secret"})
conn.execute("GRANT SELECT ON shop.* TO app")
```

| Code | Class | Meaning |
| --- | --- | --- |
| `CADB0516` | `OperationalError` | Authentication failed: a missing, invalid or expired token, an unknown user, or a wrong password. |
| `CADB0517` | `ProgrammingError` | The user does not have the privilege that the statement needs. |
| `CADB0518` | `OperationalError` | Too many logins for that account. |
| `CADB0519` | `InterfaceError` | Credentials over clear text. See [TLS](#tls). |

### TLS

The driver refuses credentials (`password` or `access_token`) for an endpoint or a `backup_endpoint`
that is neither `https://` nor this machine (`localhost`, `127.0.0.1`, `::1`). It raises an `InterfaceError` with the
code `CADB0519` in `connect()`, before anything goes on the network. When TLS ends at a proxy inside
your trust boundary, set `allow_insecure_credentials=True`.

## Errors

The driver raises the PEP 249 exception classes. Each exception has `code`, the server's `CADBxxxx`
code, and `message`, the text without the code.

```python
try:
    conn.execute("INSERT INTO robots (id, name) VALUES (@id, @name)", row)
except camusdb.IntegrityError as exc:
    if exc.code == camusdb.codes.DUPLICATE_PRIMARY_KEY:
        ...
```

| Class | Examples |
| --- | --- |
| `IntegrityError` | A duplicate key, a `NOT NULL`, `CHECK` or foreign key failure (`CADB03xx`, `CADB0402`). |
| `DataError` | A value that is too long, out of range, or that cannot be converted. |
| `ProgrammingError` | A syntax error, an unknown table or column, a missing privilege (`CADB04xx`, `CADB00xx`). |
| `OperationalError` | An unreachable node, a failed login, a transaction failure (`CADB05xx`). |
| `SerializationFailure` | A conflict that a replay can resolve. A subclass of `OperationalError`. |
| `NotSupportedError` | A feature that the server or the driver does not have. |
| `InternalError` | Storage that the server cannot trust, or a statement in a transaction that failed. |
| `InterfaceError` | A misuse of the driver, such as a closed connection. |

`camusdb.codes` has names for the codes that an application is likely to handle. The driver removes
secrets and control characters from the server's messages before it raises them.

## Administration

```python
conn.ping()  # True when the next node answers
conn.create_database("shop", if_not_exists=True)
conn.drop_database("shop")

conn.create_branch("shop_test", source="shop")  # a copy-on-write branch
conn.show_branches("shop")  # [Branch(database='shop_test', depth=1, ...)]
conn.show_ancestors("shop_test")
conn.database = "shop_test"  # use another database on this connection
```

Without a name, these methods use the connection's database. A create that collides with another
create at the same time is retried. A create with `if_not_exists=True` that loses a race succeeds.

### Node statistics

`conn.node_stats()` gives the disk, CPU and memory of one node:

```python
stats = conn.node_stats()  # the next node in rotation
for endpoint in conn.endpoints:  # each node answers only for itself
    stats = conn.node_stats(endpoint)
    disk = stats.data_volume
    print(stats.address, disk.available_bytes, disk.below_watermark, stats.cpu.process_cpu_percent)
```

Every size is in bytes. A value that the node could not read is `-1`, never `0`, and `stats.errors`
gives the field and the reason. A value that the server did not send is `-1` too. The endpoint must be
one of `conn.endpoints`, because the driver sends its token only to those nodes.

With authentication on, the user must be a superuser or have the `MONITOR` privilege
(`GRANT MONITOR ON *.* TO app`). Another user gets a `ProgrammingError` with the code `CADB0517`.

### Backups

`conn.backups` takes, lists and checks backups while the server runs:

```python
full = conn.backups.take_full()
incremental = conn.backups.take_incremental(full.backup_id)  # the changes since the parent
if incremental.was_substituted:
    log.warning("full backup taken instead: %s", incremental.substitution_reason)

conn.backups.list()  # every backup in the catalog
chain = conn.backups.chain(incremental.backup_id)  # root first; the server checks the chain
window = (chain[0].min_recoverable_physical_ms, chain[0].max_recoverable_physical_ms)

conn.backups.preview_garbage_collection()  # what retention would delete; nothing is deleted
conn.backups.collect_garbage()  # run retention now
```

- **A backup holds the whole node**, every database on the server, not only the connection's
  database.
- **The server must have a backup directory** (`kahuna.backup_dir`). Without one, each call raises an
  `OperationalError` with the code `CADB0700`.
- **Authorization.** With authentication on, the connection must log in as a superuser. With
  authentication off, the server accepts these calls from this machine only.
- **The node.** The calls go to `backup_endpoint` when you set it, else to the next node in rotation.
  `conn.backups.take_coordinated()` takes one consistent backup of the cluster, and only the
  coordinator node accepts it. Another node refuses with `CADB070E`. Set `backup_endpoint` to the
  coordinator. `backup_endpoint` must be a node of the same deployment, because it gets the
  connection's token.
- **The time limit** is `backup_timeout`, 300 seconds by default, because a full backup copies the
  whole node.
- **A restore is not in the driver.** A restore writes a new data directory, and an operator must
  then start a new server on it. See the server's guide to backups and point-in-time recovery.

The backup errors are `OperationalError` with a `CADB07xx` code. `camusdb.codes` names the frequent
ones.

## asyncio

`camusdb.aio` has the same interface, with coroutines:

```python
import camusdb.aio

async with await camusdb.aio.connect("Endpoint=http://localhost:5095;Database=test") as conn:
    cur = await conn.execute("SELECT name FROM robots WHERE year > @year", {"year": 1980})
    async for (name,) in cur:
        print(name)

    async for row in conn.cursor().stream("SELECT * FROM events"):
        ...

    async with conn.transaction():
        await conn.execute(...)

    await camusdb.aio.run_transaction(conn, transfer)  # transfer is an async function

    stats = await conn.node_stats()
    backup = await conn.backups.take_full()
```

## Thread safety

`camusdb.threadsafety` is `1`. Threads can share the module, but not a connection. Give each thread
its own connection. The token cache and the node health are shared by all connections, and they are
safe to use from many threads.

## Not in this version

The .NET driver has these features, and this driver does not have them yet:

- the gRPC protocol,
- prepared statements, explicit or automatic,
- learned statement routing,
- pipelined transactions,
- OpenTelemetry traces and metrics,
- HTTP/3.

## Contributing

- [CONTRIBUTING.md](https://github.com/camusdb/camusdb-py/blob/main/CONTRIBUTING.md): the
  environment, the checks, and how to run the integration tests.
- [RELEASING.md](https://github.com/camusdb/camusdb-py/blob/main/RELEASING.md): the CI jobs and how
  to publish a version.

## License

MIT. See [LICENSE](https://github.com/camusdb/camusdb-py/blob/main/LICENSE).
