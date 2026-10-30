# Connecting

`connect()` takes a connection string, keyword arguments, or both. When both set a value, the keyword
argument wins.

```python
import camusdb

conn = camusdb.connect("Endpoint=http://localhost:5095;Database=test")
conn = camusdb.connect(endpoint="http://localhost:5095", database="test", timeout=30)
```

`connect()` does not contact the server. The first statement sends the first request.

## The connection string

The string has the same format as in the .NET driver, so one string works for both.

- Keys are not case-sensitive.
- Put a value in quotes when it has a `;` or spaces: `Password='pa;ss'`.

To log a connection string without its password, use `camusdb.redact_connection_string(text)`.

## Settings

| Keyword | Connection-string key | Default | What it does |
| --- | --- | --- | --- |
| `endpoint` | `Endpoint` | required | The URL of a node. A list, or a comma-separated string, names several nodes. |
| `database` | `Database` | required | The database to use. |
| `user`, `password` | `User`, `Password` | none | See [Authentication](authentication.md). |
| `access_token` | `AccessToken` | none | A token that you got elsewhere. See [Authentication](authentication.md). |
| `timeout` | `Timeout` | `10` | The time limit of each request, in seconds. |
| `autocommit` | `Autocommit` | `False` | See [Transactions](transactions.md). |
| `isolation_level` | `IsolationLevel` | server default | `"Serializable"` or `"ReadCommitted"`. |
| `read_only` | `TransactionMode=ReadOnly` | server default | Make transactions read-only. |
| `locking` | `Locking` | server default | `"Pessimistic"` or `"Optimistic"`. |
| `http2` | `HttpVersion=2` | `False` | Use HTTP/2. Install `camusdb-py[http2]` first. |
| `backup_endpoint` | `BackupEndpoint` | none | The node for backup calls. See [Administration](administration.md#backups). |
| `backup_timeout` | `BackupTimeout` | `300` | The time limit of each backup call, in seconds. |
| `row_factory` | | tuples | See [Queries](queries.md#rows). |

The driver accepts the other .NET keys, but they have no effect.

## Several nodes

```python
conn = camusdb.connect(endpoint="http://a:5095,http://b:5095,http://c:5095", database="test")
```

- The driver sends each statement to the next node in turn.
- A transaction stays on the node where it began.
- When a node does not answer, the driver raises an `OperationalError` and skips that node for 30
  seconds. The driver does not send the request again, because the server can already have run it.
