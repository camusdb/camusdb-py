# Transactions

## The default

The first statement begins a transaction. `commit()` or `rollback()` ends it.

```python
conn.execute("UPDATE accounts SET balance = balance - 30 WHERE id = @id", {"id": a})
conn.execute("UPDATE accounts SET balance = balance + 30 WHERE id = @id", {"id": b})
conn.commit()
```

When you use the connection in a `with` block, the driver commits at the end of the block. If the
block raises an error, the driver rolls back. Then it closes the connection.

## Autocommit

With `autocommit=True`, each statement commits on its own. For a group of statements that must
commit together, use `transaction()`:

```python
conn.autocommit = True
with conn.transaction():
    conn.execute(...)
    conn.execute(...)
```

`transaction()` also works without autocommit. You cannot open a transaction inside another one.

## Rules from the server

- **A failed statement ends the transaction.** Call `rollback()` before you run the next statement.
- **`TRUNCATE` cannot run in a transaction.** Run it with autocommit.
- **A schema change can be part of a transaction or commit at once.** See
  [Schema changes](#schema-changes).

## Schema changes

A schema statement, such as `CREATE TABLE`, does not begin a transaction. With no transaction open,
it commits at once. In an open transaction, the server setting `transactional_ddl_enabled` decides
what the statement does. The setting is off by default, and the server got it on 2026-11-22.

- **Setting off, or an older server.** The statement commits at once. A later `rollback()` does not
  undo it.
- **Setting on.** The statement becomes part of the transaction. The later statements of the
  transaction see the change. `commit()` makes the change visible together with the rows.
  `rollback()` undoes both.

To put schema statements in a transaction, use `transaction()`, which begins at once:

```python
with conn.transaction():
    conn.execute("CREATE TABLE audit (id INT64 PRIMARY KEY NOT NULL, note STRING)")
    conn.execute("INSERT INTO audit (id, note) VALUES (1, 'created')")
```

With the setting on, the server refuses these statements in a transaction with `CADB0538`
(`ProgrammingError`), and the transaction ends:

- `TRUNCATE`, `ALTER TABLE ... REWRITE STORAGE`, `ALTER SEQUENCE ... RESTART`,
  `CREATE MATERIALIZED VIEW`, `REFRESH MATERIALIZED VIEW` and `CREATE TABLE ... RELINK`.
- A statement that reads every row of a table, such as `CREATE INDEX` or `ADD CONSTRAINT`, when all
  of these conditions are true:
  - The transaction did not create the table.
  - The transaction wrote rows to the table, or changed the table before.

An index or a column fill is also refused when the transaction holds locks on rows of the table from
a read. A foreign key that needs a new index has the same rule. Run such a statement first in the
transaction.

A `CHECK`, a `NOT NULL` or a foreign key on an existing table holds for every transaction from the
statement on. A rollback removes it. The values that `nextval` gave to a rolled-back transaction are
lost.

Other errors to expect with the setting on:

- `CADB0504`: another transaction held the DDL lock of the database for more than 30 seconds.
- `CADB0502` from `commit()`: the schema changed after the first schema statement of the transaction.
  The transaction rolls back.
- `CADB0509` from `commit()`: the server did not confirm the schema change. The driver sends the same
  commit again before it raises this error.

The first two are `SerializationFailure` errors, and [`run_transaction()`](#retries) runs the
transaction again after them.

`REFRESH MATERIALIZED VIEW` does not begin a transaction either. With no transaction open, it commits
at once.

## Isolation and locking

Set these in `connect()`, as attributes of the connection, or for one `transaction()` block:

```python
with conn.transaction(isolation_level="ReadCommitted", locking="optimistic", read_only=False):
    ...
```

| Setting | Values | Server default |
| --- | --- | --- |
| `isolation_level` | `"Serializable"`, `"ReadCommitted"` | `"Serializable"` |
| `locking` | `"Pessimistic"`, `"Optimistic"` | `"Pessimistic"` |
| `read_only` | `True`, `False` | `False` |

With optimistic locking, the server checks for conflicts only at commit.

## Retries

When two transactions conflict, the server stops one of them, and the driver raises
`camusdb.SerializationFailure`. Run the whole transaction again, not only the last statement.

`run_transaction()` does this for you:

```python
def transfer(conn):
    (balance,) = conn.execute("SELECT balance FROM accounts WHERE id = @id", {"id": a}).fetchone()
    conn.execute("UPDATE accounts SET balance = @b WHERE id = @id", {"b": balance - 30, "id": a})


camusdb.run_transaction(conn, transfer, max_attempts=5)
```

- The function must do all of its work again on each attempt.
- An error that is not a conflict stops the retries at once.
- With autocommit off, call `run_transaction()` when no transaction is open.

To write your own retry loop, use `camusdb.is_retryable(exc)`.

## Commit timestamp

After a successful commit, `conn.commit_timestamp` holds the commit timestamp of the transaction. It
is the hybrid logical clock (HLC) timestamp that the server gives to every write of the transaction:
a wall-clock time `l`, a counter `c` and a node id `n`.

```python
with conn.transaction():
    conn.execute("UPDATE accounts SET balance = 0 WHERE id = @id", {"id": a})

committed_at = conn.commit_timestamp  # a camusdb.HlcTimestamp; str() gives HLC(2:1795124033385:7)
```

The value is `None` in these cases:

- Before the first commit of the connection, and while a transaction is open.
- After a rollback or a failed commit.
- The last transaction wrote nothing, for example a read-only transaction.
- The server is older than CamusDB 0.18.4.

A statement with autocommit does not report a commit timestamp, and it does not change the value. A
commit that the driver sends again after `CADB0509` reports the commit that landed.

`HlcTimestamp` values compare by `l`, then `c`, then `n`, as the server compares them. These rules
say which commits are in that order:

- **Two transactions that write the same row:** always, on every node.
- **A transaction that reads a value and then commits:** it is above the commit of that value when
  the transaction is optimistic, or when it read a row that it already wrote. Other reads of a
  pessimistic transaction (the default) give no such promise.
- **Other cases:** no order is promised.

To put the requests of one session in order across processes, make each request write the same row.
