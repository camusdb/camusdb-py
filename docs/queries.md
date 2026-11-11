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
| `dict` | `JSONB`. See [JSONB](#jsonb). |

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

`inf`, `-inf` and `nan` are float values too. JSON has no number for them, so the driver sends a
`FLOAT64` or `FLOAT32` parameter with one of these values as the string `"Infinity"`, `"-Infinity"` or
`"NaN"`. In a `FLOAT64` or `FLOAT32` cell or array element, the driver reads these three strings as
the float value. Any other string stays a `str`. A server that is older than the PostgreSQL float
rule cannot return these values, and it refuses them in a parameter.

## JSONB

A `JSONB` column (alias `JSON`) holds one JSON document. The SQL follows PostgreSQL `jsonb`: the
operators `->`, `->>`, `@>`, `?` and the others, the `jsonb_*` functions, and GIN indexes.

- A `dict` parameter binds as `JSONB`. A `list` binds as an `ARRAY`. To bind a list as one JSON
  array document, use `Typed(value, ColumnType.JSONB)`.
- A `str` typed `ColumnType.JSONB` goes to the server as it is, and the server parses it. A plain
  `str` binds as a `STRING`.
- The server has no `ARRAY(JSONB)`, so a list of `dict` values is refused.
- Text that is not valid JSON fails with `CADB0418`. A document nested deeper than 512 levels fails
  with `CADB0419`. The driver refuses a `dict` or a `list` nested that deep with a `DataError`, before
  it sends the statement.

A `JSONB` cell comes back as a `str` in the PostgreSQL text form: one space after each `:` and each
`,`, and the keys in the stored order. Numbers keep all their digits. Use `json.loads()` to get a
`dict`, with `parse_float=decimal.Decimal` if the numbers must stay exact.

The driver does not give a `dict`, because JSON `null` and SQL `NULL` are different values. A JSON
`null` document comes back as the text `"null"`, and SQL `NULL` comes back as `None`. A server older
than this feature sends both as `None`.

`camusdb.format_jsonb(text)` gives the PostgreSQL text form of any JSON text.

```python
from camusdb import ColumnType, Typed

conn.execute("CREATE TABLE events (id OID PRIMARY KEY NOT NULL, payload JSONB NOT NULL)")
conn.execute("CREATE INDEX events_payload_gin ON events USING GIN (payload)")

conn.execute(
    "INSERT INTO events (id, payload) VALUES (GEN_ID(), @payload)",
    {"payload": {"kind": "login", "user": {"id": 7}}},
)

cur = conn.execute(
    "SELECT payload, payload -> 'user' ->> 'id' FROM events WHERE payload @> @filter",
    {"filter": Typed('{"kind": "login"}', ColumnType.JSONB)},
)
payload, user_id = cur.fetchone()  # '{"kind": "login", "user": {"id": 7}}', '7'
```

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

`UPSERT INTO` and `INSERT ... ON CONFLICT` are writes like `INSERT`. They take a `RETURNING` list
too.

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

## Inline functions

An inline function is a scalar function. Its C# body is dollar-quoted text in `CREATE FUNCTION`.
The operator deploys no DLL. The user needs the `CREATE` privilege on the database, not superuser.
The server setting `inline_functions_enabled` must be `true`.

```python
conn.execute("""
    CREATE OR REPLACE FUNCTION slugify(s STRING) RETURNS STRING LANGUAGE csharp AS $$
        return s.Trim().ToLowerInvariant().Replace(' ', '-');
    $$
""")
conn.execute("SELECT slugify(title) FROM posts").fetchall()
```

- `CREATE [OR REPLACE] FUNCTION`, `DROP FUNCTION` and `ALTER FUNCTION ... OWNER TO` are schema
  changes. `CREATE EXTENSION`, `ALTER EXTENSION` and `DROP EXTENSION` are schema changes too.
- The driver sends the body as you write it. If the body holds `$$`, use a tagged quote:
  `$fn$ ... $fn$`.
- The driver reads dollar-quoted text as one literal, so a word in the body is not a keyword.
- `SHOW FUNCTIONS` and `SHOW CREATE FUNCTION name` return rows.
- The types are `INT64`, `FLOAT64`, `FLOAT32`, `BOOL`, `STRING`, `UUID`, `BYTES`, `DATETIME`,
  `NUMERIC` and `ARRAY(E)`.

`camusdb.codes` names the error codes:

| Code | Name | When |
| --- | --- | --- |
| `CADB0838` | `INLINE_FUNCTION_COMPILE_FAILED` | The body does not compile. |
| `CADB0839` | `INLINE_FUNCTION_REFUSED` | The body uses a type or a member that is not allowed. |
| `CADB0840` | `INLINE_FUNCTION_BUDGET_EXCEEDED` | A call went over its time, memory, array or stack budget. |
| `CADB0841` | `INLINE_FUNCTIONS_DISABLED` | The feature is off. |
| `CADB0842` | `FUNCTION_ALREADY_EXISTS` | The name is taken. |
| `CADB0843` | `FUNCTION_NOT_FOUND` | No function has that name. |
| `CADB0844` | `FUNCTION_HAS_DEPENDENTS` | A view or a `CHECK` uses the function. |
| `CADB0845` | `INLINE_FUNCTION_UNAVAILABLE_ON_NODE` | This node cannot compile the function. |
| `CADB0809` | `EXTENSION_FUNCTION_FAILED` | The body threw an exception. |

The driver retries none of these errors.
