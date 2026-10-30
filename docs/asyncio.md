# asyncio and threads

## asyncio

`camusdb.aio` has the same interface. Use `await` on the calls:

```python
import camusdb.aio

async with await camusdb.aio.connect("Endpoint=http://localhost:5095;Database=test") as conn:
    cur = await conn.execute("SELECT name FROM robots WHERE year > @year", {"year": 1980})
    async for (name,) in cur:
        print(name)

    async with conn.transaction():
        await conn.execute(...)

    await camusdb.aio.run_transaction(conn, transfer)  # transfer is an async function
```

## Threads

Do not share a connection between threads. Give each thread its own connection.
