"""Node statistics and the backup administration API, against a fake server."""

import datetime as dt
import json
import uuid

import httpx
import pytest

import camusdb
import camusdb.aio
from camusdb import codes
from camusdb._admin import _datetime
from camusdb._auth import TOKENS
from camusdb._config import build_config

UTC = dt.timezone.utc

STATS = {
    "status": "ok",
    "node": {
        "nodeId": "n1",
        "endpoint": "10.0.0.1:2070",
        "version": "1.2.3",
        "clusterMode": True,
        "uptimeSeconds": 3600,
        "sampledAtUnixMs": 1767225600000,
    },
    "disks": [
        {"role": "wal", "path": "/wal", "totalBytes": 100},
        {
            "role": "data",
            "path": "/data",
            "mountPoint": "/",
            "totalBytes": 1000,
            "freeBytes": 400,
            "availableBytes": 300,
            "usedBytes": 600,
            "minFreeDiskBytes": 50,
            "belowWatermark": False,
            "engineBytes": 200,
        },
    ],
    "cpu": {"processCpuPercent": 12.5, "processorCount": 8, "loadAverage1m": -1},
    "memory": {"processWorkingSetBytes": 1024, "gcMemoryLimitBytes": 0},
    "errors": [{"field": "cpu.load_average_1m", "message": "not available"}],
}

BACKUP = {
    "backupId": "3f2b7c8e-0d6a-4f7e-9a51-6c1e2d3f4a5b",
    "formatVersion": 2,
    "type": "Full",
    "createdAtUtc": "2026-10-29T12:34:56.1234567Z",
    "parentBackupId": None,
    "partitionCount": 4,
    "requestedKind": "Full",
    "actualKind": "Full",
    "isInvalid": False,
}


class FakeServer:
    def __init__(self):
        self.requests: list[tuple[str, str, str, dict, dict]] = []
        self.handlers = {}

    def __call__(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content) if request.content else None
        self.requests.append(
            (
                request.method,
                f"{request.url.scheme}://{request.url.netloc.decode()}",
                request.url.raw_path.decode(),
                body,
                dict(request.headers),
            )
        )
        handler = self.handlers.get(request.url.path)
        result = handler(body, request) if handler is not None else {"status": "ok"}
        return result if isinstance(result, httpx.Response) else httpx.Response(200, json=result)


@pytest.fixture
def server():
    return FakeServer()


@pytest.fixture
def connect(server):
    created = []

    def factory(**kwargs):
        kwargs.setdefault("endpoint", f"http://localhost:{9100 + len(created)}")
        kwargs.setdefault("database", "test")
        conn = camusdb.connect(**kwargs)
        conn._transport._client = httpx.Client(transport=httpx.MockTransport(server))
        created.append(conn)
        return conn

    yield factory
    for conn in created:
        conn.close()


class TestNodeStats:
    def test_the_answer_is_decoded(self, server, connect):
        server.handlers["/v1/admin/stats"] = lambda body, request: STATS
        conn = connect()
        stats = conn.node_stats()
        method, endpoint, path, _, _ = server.requests[0]
        assert (method, path) == ("GET", "/v1/admin/stats")
        assert stats.address == endpoint == conn.endpoints[0]
        assert stats.node.node_id == "n1"
        assert stats.node.cluster_mode is True
        assert stats.node.uptime_seconds == 3600
        assert [d.role for d in stats.disks] == ["wal", "data"]
        assert stats.data_volume is not None
        assert stats.data_volume.available_bytes == 300
        assert stats.cpu.process_cpu_percent == 12.5
        assert stats.cpu.processor_count == 8
        assert stats.memory.gc_memory_limit_bytes == 0
        assert stats.errors == (
            camusdb.NodeStatsError(field="cpu.load_average_1m", message="not available"),
        )

    def test_a_value_the_server_did_not_send_is_minus_one(self, server, connect):
        server.handlers["/v1/admin/stats"] = lambda body, request: STATS
        stats = connect().node_stats()
        wal = stats.disks[0]
        assert wal.free_bytes == -1
        assert wal.mount_point == ""
        assert stats.cpu.load_average_15m == -1
        assert stats.cpu.process_cpu_window_ms == -1
        assert stats.memory.host_total_bytes == -1

    def test_an_empty_answer_still_decodes(self, server, connect):
        server.handlers["/v1/admin/stats"] = lambda body, request: {"status": "ok"}
        stats = connect().node_stats()
        assert stats.disks == ()
        assert stats.data_volume is None
        assert stats.node.sampled_at_unix_ms == -1

    def test_a_named_endpoint_is_asked(self, server, connect):
        server.handlers["/v1/admin/stats"] = lambda body, request: STATS
        conn = connect(endpoint="http://localhost:9201,http://localhost:9202")
        stats = conn.node_stats("http://localhost:9202/")
        assert stats.address == "http://localhost:9202"
        assert server.requests[0][1] == "http://localhost:9202"

    def test_an_endpoint_of_another_deployment_is_refused(self, server, connect):
        conn = connect()
        with pytest.raises(camusdb.ProgrammingError) as info:
            conn.node_stats("http://elsewhere:5095")
        assert info.value.code == codes.INVALID_INPUT
        assert server.requests == []

    def test_a_missing_privilege_is_raised(self, server, connect):
        server.handlers["/v1/admin/stats"] = lambda body, request: httpx.Response(
            403, json={"status": "failed", "code": "CADB0517", "message": "needs MONITOR"}
        )
        with pytest.raises(camusdb.ProgrammingError) as info:
            connect().node_stats()
        assert info.value.code == codes.INSUFFICIENT_PRIVILEGE

    def test_a_closed_connection_is_refused(self, connect):
        conn = connect()
        conn.close()
        with pytest.raises(camusdb.InterfaceError):
            conn.node_stats()


class TestBackups:
    def test_take_full(self, server, connect):
        server.handlers["/v1/backups/full"] = lambda body, request: {
            "status": "ok",
            "backup": BACKUP,
        }
        backup = connect().backups.take_full()
        method, _, path, body, _ = server.requests[0]
        assert (method, path, body) == ("POST", "/v1/backups/full", None)
        assert backup.backup_id == BACKUP["backupId"]
        assert backup.type == "Full"
        assert backup.format_version == 2
        assert backup.partition_count == 4
        assert backup.parent_backup_id is None
        assert backup.created_at == dt.datetime(2026, 10, 29, 12, 34, 56, 123456, tzinfo=UTC)
        assert backup.was_substituted is False

    def test_take_incremental_sends_the_parent(self, server, connect):
        parent = uuid.UUID(BACKUP["backupId"])
        server.handlers["/v1/backups/incremental"] = lambda body, request: {
            "status": "ok",
            "backup": {
                **BACKUP,
                "requestedKind": "Incremental",
                "actualKind": "Full",
                "substitutionReason": "the parent is below the retention floor",
            },
        }
        backup = connect().backups.take_incremental(parent)
        assert server.requests[0][3] == {"parentBackupId": str(parent)}
        assert backup.was_substituted is True
        assert backup.substitution_reason == "the parent is below the retention floor"

    def test_take_incremental_needs_a_parent(self, server, connect):
        with pytest.raises(camusdb.ProgrammingError):
            connect().backups.take_incremental("  ")
        assert server.requests == []

    def test_take_coordinated(self, server, connect):
        server.handlers["/v1/backups/coordinated"] = lambda body, request: {
            "status": "ok",
            "backup": {
                **BACKUP,
                "type": "Coordinated",
                "clusterSnapshotNode": 1,
                "clusterSnapshotPhysical": 1767225600000,
                "clusterSnapshotCounter": 7,
            },
        }
        backup = connect().backups.take_coordinated()
        assert backup.cluster_snapshot_physical == 1767225600000
        assert backup.cluster_snapshot_counter == 7

    def test_list_keeps_an_invalid_entry(self, server, connect):
        server.handlers["/v1/backups"] = lambda body, request: {
            "status": "ok",
            "backups": [
                BACKUP,
                {"backupId": "bad", "isInvalid": True, "invalidReason": "truncated"},
            ],
        }
        backups = connect().backups.list()
        assert server.requests[0][:3] == ("GET", "http://localhost:9100", "/v1/backups")
        assert [b.backup_id for b in backups] == [BACKUP["backupId"], "bad"]
        assert backups[1].is_invalid is True
        assert backups[1].invalid_reason == "truncated"
        assert backups[1].created_at is None

    def test_chain_quotes_the_id(self, server, connect):
        server.handlers["/v1/backups/a/b/chain"] = lambda body, request: {"status": "ok"}
        server.handlers["/v1/backups/a%2Fb/chain"] = server.handlers["/v1/backups/a/b/chain"]
        assert connect().backups.chain("a/b") == []
        assert server.requests[0][2] == "/v1/backups/a%2Fb/chain"

    def test_chain_reports_the_window_on_the_root(self, server, connect):
        server.handlers[f"/v1/backups/{BACKUP['backupId']}/chain"] = lambda body, request: {
            "status": "ok",
            "backups": [
                {**BACKUP, "minRecoverablePhysicalMs": 10, "maxRecoverablePhysicalMs": 20},
                {**BACKUP, "type": "Incremental", "parentBackupId": BACKUP["backupId"]},
            ],
        }
        chain = connect().backups.chain(BACKUP["backupId"])
        assert (chain[0].min_recoverable_physical_ms, chain[0].max_recoverable_physical_ms) == (
            10,
            20,
        )
        assert chain[1].min_recoverable_physical_ms is None
        assert chain[1].parent_backup_id == BACKUP["backupId"]

    def test_preview_and_collect_garbage(self, server, connect):
        def gc(body, request):
            return {
                "status": "ok",
                "applied": request.url.params["dryRun"] == "false",
                "bytesReclaimed": 4096,
                "retentionDeletions": [
                    {
                        "backupId": "old",
                        "type": "Full",
                        "createdAtUtc": "2026-01-01T00:00:00Z",
                        "bytes": 4000,
                        "reason": "max_chains",
                    }
                ],
                "orphanReclamations": [{"name": "tmp", "isDirectory": True, "reason": "stray"}],
            }

        server.handlers["/v1/backups/gc"] = gc
        conn = connect()
        preview = conn.backups.preview_garbage_collection()
        assert server.requests[0][:3] == (
            "POST",
            "http://localhost:9100",
            "/v1/backups/gc?dryRun=true",
        )
        assert preview.applied is False
        assert preview.bytes_reclaimed == 4096
        assert preview.retention_deletions[0].created_at == dt.datetime(2026, 1, 1, tzinfo=UTC)
        assert preview.orphan_reclamations[0].is_directory is True
        assert conn.backups.collect_garbage().applied is True
        assert server.requests[1][2] == "/v1/backups/gc?dryRun=false"

    def test_a_backup_failure_is_an_operational_error(self, server, connect):
        server.handlers["/v1/backups/full"] = lambda body, request: httpx.Response(
            503, json={"status": "failed", "code": "CADB0700", "message": "no backup_dir"}
        )
        with pytest.raises(camusdb.OperationalError) as info:
            connect().backups.take_full()
        assert info.value.code == codes.BACKUP_NOT_CONFIGURED

    def test_an_answer_without_a_backup_is_an_error(self, server, connect):
        server.handlers["/v1/backups/full"] = lambda body, request: {"status": "ok"}
        with pytest.raises(camusdb.InterfaceError):
            connect().backups.take_full()

    def test_the_backup_endpoint_is_used_as_it_is(self, server, connect):
        server.handlers["/v1/backups"] = lambda body, request: {"status": "ok", "backups": []}
        conn = connect(
            endpoint="http://localhost:9301,http://localhost:9302",
            backup_endpoint="http://localhost:9303/",
        )
        conn.backups.list()
        conn.backups.list()
        assert [r[1] for r in server.requests] == ["http://localhost:9303"] * 2

    def test_the_backup_timeout_applies(self, server, connect):
        timeouts = []

        def record(body, request):
            timeouts.append(request.extensions["timeout"]["read"])
            return {"status": "ok", "backups": []}

        server.handlers["/v1/backups"] = record
        connect(backup_timeout=42).backups.list()
        connect().backups.list()
        assert timeouts == [42, 300]

    def test_the_token_is_sent(self, server, connect):
        TOKENS._tokens.clear()
        server.handlers["/login"] = lambda body, request: {"status": "ok", "token": "tok-1"}
        server.handlers["/v1/backups"] = lambda body, request: {"status": "ok", "backups": []}
        connect(user="admin", password="secret").backups.list()
        assert [r[2] for r in server.requests] == ["/login", "/v1/backups"]
        assert server.requests[1][4]["authorization"] == "Bearer tok-1"
        TOKENS._tokens.clear()


class TestConfig:
    def test_the_connection_string_keys(self):
        config = build_config(
            "Endpoint=http://a:1;Database=d;BackupEndpoint=http://b:2/;BackupTimeout=900", {}
        )
        assert config.backup_endpoint == "http://b:2"
        assert config.backup_timeout == 900

    def test_the_defaults(self):
        config = build_config("Endpoint=http://a:1;Database=d", {})
        assert config.backup_endpoint is None
        assert config.backup_timeout == 300

    def test_one_backup_endpoint_only(self):
        with pytest.raises(camusdb.ProgrammingError):
            build_config(
                "Endpoint=http://a:1;Database=d;BackupEndpoint='http://b:2,http://c:3'", {}
            )

    def test_credentials_are_not_sent_in_clear_to_the_backup_endpoint(self):
        with pytest.raises(camusdb.InterfaceError) as info:
            build_config(
                "Endpoint=https://a:1;Database=d;User=u;Password=p;BackupEndpoint=http://b:2", {}
            )
        assert info.value.code == codes.INSECURE_TRANSPORT


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("2026-10-29T12:34:56Z", dt.datetime(2026, 10, 29, 12, 34, 56, tzinfo=UTC)),
        ("2026-10-29T12:34:56.5", dt.datetime(2026, 10, 29, 12, 34, 56, 500000, tzinfo=UTC)),
        ("2026-10-29T14:34:56+02:00", dt.datetime(2026, 10, 29, 12, 34, 56, tzinfo=UTC)),
        ("2026-10-29", None),
        ("not a date", None),
        (None, None),
    ],
)
def test_datetime(text, expected):
    assert _datetime(text) == expected


@pytest.mark.anyio
async def test_async(server):
    server.handlers["/v1/admin/stats"] = lambda body, request: STATS
    server.handlers["/v1/backups/full"] = lambda body, request: {"status": "ok", "backup": BACKUP}
    server.handlers["/v1/backups/incremental"] = server.handlers["/v1/backups/full"]
    server.handlers["/v1/backups"] = lambda body, request: {"status": "ok", "backups": [BACKUP]}
    conn = await camusdb.aio.connect(endpoint="http://localhost:9400", database="test")
    conn._transport._client = httpx.AsyncClient(transport=httpx.MockTransport(server))
    async with conn:
        stats = await conn.node_stats()
        assert stats.node.version == "1.2.3"
        backup = await conn.backups.take_full()
        assert backup.backup_id == BACKUP["backupId"]
        assert [b.backup_id for b in await conn.backups.list()] == [BACKUP["backupId"]]
        await conn.backups.take_incremental(backup.backup_id)
        await conn.backups.chain(backup.backup_id)
        await conn.backups.preview_garbage_collection()
    assert [r[2] for r in server.requests] == [
        "/v1/admin/stats",
        "/v1/backups/full",
        "/v1/backups",
        "/v1/backups/incremental",
        f"/v1/backups/{BACKUP['backupId']}/chain",
        "/v1/backups/gc?dryRun=true",
    ]
