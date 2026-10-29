"""Node statistics and the backup administration API: the result types and their decoding.

Both surfaces are server-level. They are not scoped to the connection's database, and they have no
SQL form, so they have their own REST routes.
"""

from __future__ import annotations

import datetime as dt
import re
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote

from . import codes
from ._protocol import expect_ok
from .errors import InterfaceError, ProgrammingError

__all__ = [
    "BackupGcDeletion",
    "BackupGcOrphan",
    "BackupGcResult",
    "BackupInfo",
    "CpuStats",
    "DiskVolumeStats",
    "MemoryStats",
    "NodeIdentity",
    "NodeStats",
    "NodeStatsError",
]

# Routes.
NODE_STATS = "/v1/admin/stats"
BACKUPS = "/v1/backups"
BACKUP_FULL = "/v1/backups/full"
BACKUP_INCREMENTAL = "/v1/backups/incremental"
BACKUP_COORDINATED = "/v1/backups/coordinated"


def backup_chain_path(leaf: str) -> str:
    return f"{BACKUPS}/{quote(leaf, safe='')}/chain"


def backup_gc_path(dry_run: bool) -> str:
    return f"{BACKUPS}/gc?dryRun={'true' if dry_run else 'false'}"


def backup_id(value: str | uuid.UUID, name: str) -> str:
    """The text of a backup id: a ``uuid.UUID``, or a string that is not empty."""
    if isinstance(value, uuid.UUID):
        return str(value)
    if isinstance(value, str) and value.strip():
        return value.strip()
    raise ProgrammingError(f"{name} must be a backup id, not {value!r}", codes.INVALID_INPUT)


def node_endpoint(endpoints: tuple[str, ...], endpoint: str | None) -> str | None:
    """``endpoint``, when it is one of the connection's endpoints.

    The connection sends its token only to the endpoints that it was configured with.
    """
    if endpoint is None:
        return None
    normalized = endpoint.strip().rstrip("/")
    if normalized not in endpoints:
        raise ProgrammingError(
            f"{endpoint!r} is not one of this connection's endpoints", codes.INVALID_INPUT
        )
    return normalized


# --- Node statistics ------------------------------------------------------------------------------
#
# A value that the node could not read is -1, never 0, and ``errors`` names the field and the reason.
# A value that the server did not send, for example from an older server, is -1 too.


@dataclass(frozen=True)
class NodeIdentity:
    """Which node answered, and when."""

    node_id: str
    endpoint: str
    version: str
    cluster_mode: bool
    uptime_seconds: int
    sampled_at_unix_ms: int


@dataclass(frozen=True)
class DiskVolumeStats:
    """One volume that the engine writes to. Every size is in bytes."""

    DATA_ROLE = "data"
    WAL_ROLE = "wal"

    role: str
    path: str
    mount_point: str
    total_bytes: int
    free_bytes: int
    available_bytes: int
    used_bytes: int
    min_free_disk_bytes: int
    below_watermark: bool
    engine_bytes: int


@dataclass(frozen=True)
class CpuStats:
    """The CPU of the server process and of its host."""

    process_cpu_percent: float
    process_cpu_window_ms: int
    processor_count: int
    load_average_1m: float
    load_average_5m: float
    load_average_15m: float


@dataclass(frozen=True)
class MemoryStats:
    """The memory of the server process, of its managed heap, and of its host, in bytes."""

    process_working_set_bytes: int
    gc_heap_bytes: int
    gc_committed_bytes: int
    gc_memory_limit_bytes: int
    host_total_bytes: int
    host_available_bytes: int


@dataclass(frozen=True)
class NodeStatsError:
    """Why one value is -1. ``field`` is the path of the value, such as ``cpu.load_average_1m``."""

    field: str
    message: str


@dataclass(frozen=True)
class NodeStats:
    """The disk, CPU and memory of one node, from :meth:`Connection.node_stats`.

    ``address`` is the configured endpoint that answered. The driver sets it; the server does not
    send it.
    """

    address: str
    node: NodeIdentity
    disks: tuple[DiskVolumeStats, ...]
    cpu: CpuStats
    memory: MemoryStats
    errors: tuple[NodeStatsError, ...]

    @property
    def data_volume(self) -> DiskVolumeStats | None:
        """The volume that holds the data directory, or None when the server sent none."""
        return next((d for d in self.disks if d.role == DiskVolumeStats.DATA_ROLE), None)


def parse_node_stats(body: Mapping[str, Any], address: str) -> NodeStats:
    expect_ok(body, "node stats failed")
    node = _section(body, "node")
    cpu = _section(body, "cpu")
    memory = _section(body, "memory")
    return NodeStats(
        address=address,
        node=NodeIdentity(
            node_id=_str(node, "nodeId"),
            endpoint=_str(node, "endpoint"),
            version=_str(node, "version"),
            cluster_mode=_bool(node, "clusterMode"),
            uptime_seconds=_int(node, "uptimeSeconds"),
            sampled_at_unix_ms=_int(node, "sampledAtUnixMs"),
        ),
        disks=tuple(
            DiskVolumeStats(
                role=_str(disk, "role"),
                path=_str(disk, "path"),
                mount_point=_str(disk, "mountPoint"),
                total_bytes=_int(disk, "totalBytes"),
                free_bytes=_int(disk, "freeBytes"),
                available_bytes=_int(disk, "availableBytes"),
                used_bytes=_int(disk, "usedBytes"),
                min_free_disk_bytes=_int(disk, "minFreeDiskBytes"),
                below_watermark=_bool(disk, "belowWatermark"),
                engine_bytes=_int(disk, "engineBytes"),
            )
            for disk in _items(body, "disks")
        ),
        cpu=CpuStats(
            process_cpu_percent=_float(cpu, "processCpuPercent"),
            process_cpu_window_ms=_int(cpu, "processCpuWindowMs"),
            processor_count=_int(cpu, "processorCount"),
            load_average_1m=_float(cpu, "loadAverage1m"),
            load_average_5m=_float(cpu, "loadAverage5m"),
            load_average_15m=_float(cpu, "loadAverage15m"),
        ),
        memory=MemoryStats(
            process_working_set_bytes=_int(memory, "processWorkingSetBytes"),
            gc_heap_bytes=_int(memory, "gcHeapBytes"),
            gc_committed_bytes=_int(memory, "gcCommittedBytes"),
            gc_memory_limit_bytes=_int(memory, "gcMemoryLimitBytes"),
            host_total_bytes=_int(memory, "hostTotalBytes"),
            host_available_bytes=_int(memory, "hostAvailableBytes"),
        ),
        errors=tuple(
            NodeStatsError(field=_str(error, "field"), message=_str(error, "message"))
            for error in _items(body, "errors")
        ),
    )


# --- Backups --------------------------------------------------------------------------------------


@dataclass(frozen=True)
class BackupInfo:
    """One backup in the node's catalog.

    A backup is node-wide: every database on the server is in it, not only the connection's database.
    """

    backup_id: str
    format_version: int
    # The kind that the manifest records: "Full", "Incremental" or "Coordinated".
    type: str
    created_at: dt.datetime | None
    # The backup that this one chains onto. None for a full or a coordinated backup.
    parent_backup_id: str | None
    partition_count: int
    # The HLC of the cut of a coordinated backup. None for the other kinds.
    cluster_snapshot_node: int | None
    cluster_snapshot_physical: int | None
    cluster_snapshot_counter: int | None
    requested_kind: str | None
    actual_kind: str | None
    substitution_reason: str | None
    # True when the manifest could not be read. Then only backup_id and invalid_reason are valid.
    is_invalid: bool
    invalid_reason: str | None
    # The restorable window of a chain, in Unix epoch milliseconds. Only the first entry (the root)
    # of a list from Backups.chain() has it.
    min_recoverable_physical_ms: int | None
    max_recoverable_physical_ms: int | None
    cluster_id: str | None
    coordinator_node: str | None

    @property
    def was_substituted(self) -> bool:
        """True when the server made a different kind than the one asked for.

        An incremental backup whose parent is older than the retention floor is taken as a full
        backup. The backup succeeded, but it is a full image, not an increment.
        """
        return (
            self.requested_kind is not None
            and self.actual_kind is not None
            and self.requested_kind != self.actual_kind
        )


@dataclass(frozen=True)
class BackupGcDeletion:
    """One backup that retention deleted, or that a preview selected for deletion."""

    backup_id: str
    type: str
    created_at: dt.datetime | None
    bytes: int
    # The retention limit that selected it: the chain count, the age, or the total size.
    reason: str


@dataclass(frozen=True)
class BackupGcOrphan:
    """One file or directory under the backup root that belongs to no backup in the catalog."""

    name: str
    is_directory: bool
    reason: str


@dataclass(frozen=True)
class BackupGcResult:
    """What a retention pass reclaimed, or what a preview would reclaim."""

    # False for a preview. True when the pass deleted the files.
    applied: bool
    bytes_reclaimed: int
    retention_deletions: tuple[BackupGcDeletion, ...]
    orphan_reclamations: tuple[BackupGcOrphan, ...]


def parse_backup(body: Mapping[str, Any], fallback: str) -> BackupInfo:
    expect_ok(body, fallback)
    backup = body.get("backup")
    if not isinstance(backup, Mapping):
        raise InterfaceError(f"{fallback}: the server sent no backup")
    return _backup_info(backup)


def parse_backup_list(body: Mapping[str, Any], fallback: str) -> list[BackupInfo]:
    expect_ok(body, fallback)
    return [_backup_info(item) for item in _items(body, "backups")]


def parse_backup_gc(body: Mapping[str, Any]) -> BackupGcResult:
    expect_ok(body, "backup garbage collection failed")
    return BackupGcResult(
        applied=_bool(body, "applied"),
        bytes_reclaimed=_int(body, "bytesReclaimed", 0),
        retention_deletions=tuple(
            BackupGcDeletion(
                backup_id=_str(item, "backupId"),
                type=_str(item, "type"),
                created_at=_datetime(item.get("createdAtUtc")),
                bytes=_int(item, "bytes", 0),
                reason=_str(item, "reason"),
            )
            for item in _items(body, "retentionDeletions")
        ),
        orphan_reclamations=tuple(
            BackupGcOrphan(
                name=_str(item, "name"),
                is_directory=_bool(item, "isDirectory"),
                reason=_str(item, "reason"),
            )
            for item in _items(body, "orphanReclamations")
        ),
    )


def _backup_info(item: Mapping[str, Any]) -> BackupInfo:
    return BackupInfo(
        backup_id=_str(item, "backupId"),
        format_version=_int(item, "formatVersion", 0),
        type=_str(item, "type"),
        created_at=_datetime(item.get("createdAtUtc")),
        parent_backup_id=_optional_str(item, "parentBackupId"),
        partition_count=_int(item, "partitionCount", 0),
        cluster_snapshot_node=_optional_int(item, "clusterSnapshotNode"),
        cluster_snapshot_physical=_optional_int(item, "clusterSnapshotPhysical"),
        cluster_snapshot_counter=_optional_int(item, "clusterSnapshotCounter"),
        requested_kind=_optional_str(item, "requestedKind"),
        actual_kind=_optional_str(item, "actualKind"),
        substitution_reason=_optional_str(item, "substitutionReason"),
        is_invalid=_bool(item, "isInvalid"),
        invalid_reason=_optional_str(item, "invalidReason"),
        min_recoverable_physical_ms=_optional_int(item, "minRecoverablePhysicalMs"),
        max_recoverable_physical_ms=_optional_int(item, "maxRecoverablePhysicalMs"),
        cluster_id=_optional_str(item, "clusterId"),
        coordinator_node=_optional_str(item, "coordinatorNode"),
    )


# --- JSON fields ----------------------------------------------------------------------------------
#
# A field that is missing or that has the wrong type gets its default. A newer or an older server can
# add or drop a field, and that must not make the whole answer fail.

_EMPTY: Mapping[str, Any] = {}


def _section(body: Mapping[str, Any], key: str) -> Mapping[str, Any]:
    value = body.get(key)
    return value if isinstance(value, Mapping) else _EMPTY


def _items(body: Mapping[str, Any], key: str) -> list[Mapping[str, Any]]:
    value = body.get(key)
    return [item for item in value if isinstance(item, Mapping)] if isinstance(value, list) else []


def _str(obj: Mapping[str, Any], key: str) -> str:
    value = obj.get(key)
    return value if isinstance(value, str) else ""


def _optional_str(obj: Mapping[str, Any], key: str) -> str | None:
    value = obj.get(key)
    return value if isinstance(value, str) else None


def _bool(obj: Mapping[str, Any], key: str) -> bool:
    return obj.get(key) is True


def _int(obj: Mapping[str, Any], key: str, default: int = -1) -> int:
    value = _optional_int(obj, key)
    return default if value is None else value


def _optional_int(obj: Mapping[str, Any], key: str) -> int | None:
    value = obj.get(key)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return int(value)


def _float(obj: Mapping[str, Any], key: str) -> float:
    value = obj.get(key)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return -1.0
    return float(value)


# The server writes a .NET DateTime: up to 7 digits of fraction, and "Z" or an offset, or no zone at
# all. datetime.fromisoformat() of Python 3.10 reads neither the 7 digits nor the "Z".
_ISO_DATETIME = re.compile(
    r"(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})(?:\.(\d+))?(Z|[+-]\d{2}:\d{2})?", re.IGNORECASE
)


def _datetime(value: Any) -> dt.datetime | None:
    """A UTC datetime, or None when the value is not an ISO 8601 date and time."""
    if not isinstance(value, str):
        return None
    match = _ISO_DATETIME.fullmatch(value.strip())
    if match is None:
        return None
    base, fraction, zone = match.groups()
    try:
        parsed = dt.datetime.fromisoformat(base)
    except ValueError:
        return None
    # The seventh digit of a .NET tick is below the microsecond precision of Python. It is dropped.
    parsed = parsed.replace(microsecond=int((fraction or "")[:6].ljust(6, "0")))
    if zone is None or zone.upper() == "Z":
        return parsed.replace(tzinfo=dt.timezone.utc)
    sign = 1 if zone[0] == "+" else -1
    offset = dt.timedelta(hours=int(zone[1:3]), minutes=int(zone[4:6]))
    try:
        return parsed.replace(tzinfo=dt.timezone(sign * offset)).astimezone(dt.timezone.utc)
    except ValueError:
        # An offset of 24 hours or more.
        return None
