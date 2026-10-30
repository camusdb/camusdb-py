# Errors

The driver raises the standard PEP 249 exceptions. Each exception has:

- `code`: the server's error code, such as `CADB0402`.
- `message`: the text of the error.

```python
try:
    conn.execute("INSERT INTO robots (id, name) VALUES (@id, @name)", row)
except camusdb.IntegrityError as exc:
    if exc.code == camusdb.codes.DUPLICATE_PRIMARY_KEY:
        ...
```

`camusdb.codes` has names for the codes that you are likely to handle.

| Exception | When |
| --- | --- |
| `IntegrityError` | A duplicate key, or a `NOT NULL`, `CHECK` or foreign key failure. |
| `DataError` | A value that is too long, out of range, or of the wrong type. |
| `ProgrammingError` | A syntax error, an unknown table or column, or a missing privilege. |
| `OperationalError` | A node that does not answer, a failed login, or a failed transaction. |
| `SerializationFailure` | A conflict between transactions. Run the transaction again. See [Retries](transactions.md#retries). |
| `NotSupportedError` | A feature that the server or the driver does not have. |
| `InternalError` | A statement after a failed statement in the same transaction, or a server fault. |
| `InterfaceError` | A wrong use of the driver, such as a closed connection. |
