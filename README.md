# CamusDB driver for Python

A Python driver for [CamusDB](https://github.com/camusdb/camusdb). It follows
[PEP 249](https://peps.python.org/pep-0249/) (DB-API 2.0), and it has an asyncio interface.

```shell
pip install camusdb-py
```

You import it as `camusdb`. It needs Python 3.10 or later.

## Quick start

```python
import camusdb

with camusdb.connect("Endpoint=http://localhost:5095;Database=test") as conn:
    conn.create_database(if_not_exists=True)
    conn.execute(
        "CREATE TABLE robots (id OID PRIMARY KEY NOT NULL, name STRING NOT NULL, year INT64)"
    )

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

## Documentation

- [Connecting](https://github.com/camusdb/camusdb-py/blob/main/docs/connecting.md): the connection
  string, the settings, and several nodes.
- [Queries](https://github.com/camusdb/camusdb-py/blob/main/docs/queries.md): parameters, types,
  results and streaming.
- [Transactions](https://github.com/camusdb/camusdb-py/blob/main/docs/transactions.md): commit,
  autocommit, isolation and retries.
- [Authentication](https://github.com/camusdb/camusdb-py/blob/main/docs/authentication.md): users,
  passwords, tokens and TLS.
- [Errors](https://github.com/camusdb/camusdb-py/blob/main/docs/errors.md): the exception classes and
  the error codes.
- [Administration](https://github.com/camusdb/camusdb-py/blob/main/docs/administration.md):
  databases, branches, node statistics and backups.
- [asyncio and threads](https://github.com/camusdb/camusdb-py/blob/main/docs/asyncio.md).

## Not in this version

The .NET driver has these features, and this driver does not have them yet: the gRPC protocol,
prepared statements, statement routing, pipelined transactions, OpenTelemetry, and HTTP/3.

## Contributing

See [CONTRIBUTING.md](https://github.com/camusdb/camusdb-py/blob/main/CONTRIBUTING.md) and
[RELEASING.md](https://github.com/camusdb/camusdb-py/blob/main/RELEASING.md).

## License

MIT. See [LICENSE](https://github.com/camusdb/camusdb-py/blob/main/LICENSE).
