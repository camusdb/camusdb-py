# Administration

## Databases and branches

```python
conn.ping()  # True when the server answers
conn.create_database("shop", if_not_exists=True)
conn.drop_database("shop")

conn.create_branch("shop_test", source="shop")  # a copy of "shop" that shares its storage
conn.show_branches("shop")
conn.show_ancestors("shop_test")

conn.database = "shop_test"  # use another database on this connection
```

Without a name, these methods use the connection's database.

## Node statistics

`node_stats()` gives the disk, CPU and memory of one node:

```python
stats = conn.node_stats()
print(stats.data_volume.available_bytes, stats.cpu.process_cpu_percent)
```

Each node gives only its own statistics. To see all the nodes, call `conn.node_stats(endpoint)` for
each one in `conn.endpoints`.

A value that the node could not read is `-1`. `stats.errors` tells you why.

With authentication on, the user must be a superuser or have the `MONITOR` privilege.

## Backups

`conn.backups` takes and manages backups while the server runs:

```python
full = conn.backups.take_full()
incremental = conn.backups.take_incremental(full.backup_id)  # only the changes since `full`

conn.backups.list()  # all the backups
conn.backups.chain(incremental.backup_id)  # the backups that a restore needs

conn.backups.preview_garbage_collection()  # what the cleanup would delete
conn.backups.collect_garbage()  # delete old backups now
```

- A backup has all the databases on the server, not only the connection's database.
- The server must have a backup directory (`kahuna.backup_dir`).
- With authentication on, the user must be a superuser. With authentication off, the server accepts
  backup calls from the same machine only.
- If an incremental backup is not possible, the server takes a full backup. Then
  `backup.was_substituted` is `True`.
- In a cluster, `take_coordinated()` takes one consistent backup of all the nodes. Set
  `backup_endpoint` to the coordinator node.

The driver cannot restore a backup. See the server's documentation for restores.
