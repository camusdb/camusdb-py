# Queries

## Parameters

Write a placeholder as `@name`, and give the values in a dictionary:

```python
conn.execute(
    "SELECT * FROM robots WHERE year = @year AND name = @name", {"year": 1984, "name": "T-800"}
)
```

The driver never puts a value into the SQL text. The server receives each value with its type.

## Types

| Python | CamusDB |
| --- | --- |
| `None` | `NULL` |
| `bool` | `BOOL` |
| `int` | `INT64` |
| `float` | `FLOAT64` |
| `decimal.Decimal` | `NUMERIC` |
| `str` | `STRING` or `OID` |
| `bytes` | `BYTES` |
| `datetime.date` | `DATE` |
| `datetime.datetime` | `DATETIME`, in UTC. A datetime with no time zone is taken as UTC. |
| `uuid.UUID` | `UUID` |
| `list`, `tuple` | `ARRAY` |

Results come back with the same Python types. An `OID` comes back as a `str`, and a `FLOAT32` comes
back as a `float`.

When the Python type is not enough, wrap the value in `Typed`:

```python
from camusdb import ColumnType, Typed

conn.execute(
    "INSERT INTO m (id, score, tags) VALUES (GEN_ID(), @score, @tags)",
    {
        "score": Typed(9.5, ColumnType.FLOAT32),
        # An empty array needs a type.
        "tags": Typed([], ColumnType.ARRAY, element_type=ColumnType.INT64),
    },
)
```

To make a new object id in Python, use `camusdb.ObjectId.generate()`.

## Results

`execute()` reads all the rows into memory, and it returns the cursor:

```python
cur = conn.execute("SELECT id, name FROM robots")
cur.fetchone()
cur.fetchall()
```

- `cur.description` gives the name and the type of each column.
- `cur.rowcount` is the number of rows that a query returned or that a write changed.

To get a generated key, add `RETURNING` to the write. `cur.lastrowid` is always `None`.

```python
(new_id,) = conn.execute(
    "INSERT INTO robots (id, name) VALUES (GEN_ID(), @name) RETURNING id", {"name": "R2-D2"}
).fetchone()
```

## Rows

A row is a tuple. To get a dictionary or a named tuple, set a row factory on the connection or on
one cursor:

```python
from camusdb.rows import dict_row, namedtuple_row

conn = camusdb.connect(..., row_factory=dict_row)
conn.execute("SELECT id, name FROM robots").fetchone()  # {'id': '6ae2...', 'name': 'T-800'}

cur = conn.cursor(row_factory=namedtuple_row)
```

## Large results

`stream()` gives the rows while they arrive, so a large result does not fill the memory:

```python
for row in conn.cursor().stream("SELECT * FROM events"):
    handle(row)
```

Read all the rows, or close the iterator, to free the connection.

## Insert without SQL

```python
conn.cursor().insert(
    "robots", {"id": camusdb.ObjectId.generate(), "name": "HAL 9000", "year": 1968}
)
```
