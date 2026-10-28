"""The asyncio interface of the CamusDB driver.

::

    import camusdb.aio

    async with await camusdb.aio.connect("Endpoint=http://localhost:5095;Database=test") as conn:
        cur = await conn.execute("SELECT name FROM robots WHERE year > @year", {"year": 1980})
        async for row in cur:
            print(row)
"""

from .connection import AsyncConnection, AsyncCursor, connect, run_transaction

__all__ = ["AsyncConnection", "AsyncCursor", "connect", "run_transaction"]
