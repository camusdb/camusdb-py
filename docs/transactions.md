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
- **Schema changes commit at once.** A `ROLLBACK` does not undo `CREATE TABLE` or another DDL
  statement.
- **`TRUNCATE` cannot run in a transaction.** Run it with autocommit.

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
