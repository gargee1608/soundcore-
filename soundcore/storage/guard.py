"""Free-space policy.

Local audio is removed only after a verified remote copy is committed.
Unverified files are skipped even when the disk is past the cleanup line.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from soundcore.models import StorageState


@dataclass
class DiskUsage:
    used: int
    total: int

    @property
    def free(self) -> int:
        return max(0, self.total - self.used)

    @property
    def used_ratio(self) -> float:
        if self.total <= 0:
            return 1.0
        return self.used / self.total

    @property
    def free_ratio(self) -> float:
        if self.total <= 0:
            return 0.0
        return self.free / self.total


@dataclass
class RecordingView:
    """Plain snapshot so the policy can be tested without a live session."""

    id: int
    storage_state: str
    started_at: datetime
    ended_at: datetime | None
    last_accessed_at: datetime | None
    verified_at: datetime | None
    local_path: str | None
    playing: bool = False


def measure_disk(path) -> DiskUsage:
    import shutil

    raw = shutil.disk_usage(path)
    return DiskUsage(used=raw.used, total=raw.total)


def admission(usage: DiskUsage, *, reserve_ratio: float = 0.05) -> str:
    """Return ok, warn, or refuse for starting a new stream or chunk.

    refuse means free space is at or below the reserve (default 5%).
    warn means free space is within twice the reserve, still above the line.
    """
    if usage.total <= 0:
        return "refuse"
    if usage.free_ratio <= reserve_ratio:
        return "refuse"
    if usage.free_ratio <= reserve_ratio * 2:
        return "warn"
    return "ok"


def needs_emergency_cleanup(usage: DiskUsage, *, used_ratio: float = 0.95) -> bool:
    return usage.used_ratio >= used_ratio


def is_open(rec: RecordingView) -> bool:
    return rec.storage_state == StorageState.WRITING or rec.ended_at is None


def safe_to_delete_local(rec: RecordingView) -> tuple[bool, str]:
    """Hard gate used immediately before any unlink."""
    if is_open(rec):
        return False, "open"
    if rec.playing:
        return False, "playing"
    if rec.storage_state != StorageState.VERIFIED_REMOTE:
        return False, "not_verified"
    if rec.verified_at is None:
        return False, "missing_verified_at"
    if not rec.local_path:
        return False, "no_path"
    return True, "ok"


def _touched(rec: RecordingView) -> datetime:
    return rec.last_accessed_at or rec.ended_at or rec.started_at


def midnight_candidates(
    recordings: list[RecordingView],
    now: datetime,
    *,
    idle_days: int = 7,
) -> list[RecordingView]:
    cutoff = now - timedelta(days=idle_days)
    chosen: list[RecordingView] = []
    for rec in recordings:
        if is_open(rec) or rec.playing:
            continue
        if rec.storage_state == StorageState.LOCAL_DELETED:
            continue
        if rec.storage_state not in (
            StorageState.LOCAL_ONLY,
            StorageState.UPLOADED_UNVERIFIED,
            StorageState.VERIFIED_REMOTE,
        ):
            continue
        if _touched(rec) > cutoff:
            continue
        chosen.append(rec)
    chosen.sort(key=lambda rec: rec.started_at)
    return chosen


def emergency_candidates(
    recordings: list[RecordingView],
    now: datetime,
    *,
    playback_grace: timedelta = timedelta(minutes=2),
) -> list[RecordingView]:
    chosen: list[RecordingView] = []
    for rec in recordings:
        if is_open(rec) or rec.playing:
            continue
        if rec.storage_state == StorageState.LOCAL_DELETED:
            continue
        if rec.storage_state not in (
            StorageState.LOCAL_ONLY,
            StorageState.UPLOADED_UNVERIFIED,
            StorageState.VERIFIED_REMOTE,
        ):
            continue
        if rec.last_accessed_at is not None and (now - rec.last_accessed_at) < playback_grace:
            continue
        chosen.append(rec)
    chosen.sort(key=lambda rec: rec.started_at)
    return chosen


def delete_local_if_verified(rec: RecordingView, data_dir) -> tuple[bool, str]:
    """Unlink only when the snapshot itself proves the remote copy was verified."""
    from pathlib import Path

    allowed, reason = safe_to_delete_local(rec)
    if not allowed:
        return False, reason
    path = Path(data_dir) / rec.local_path
    # Refuse to follow a path that escapes the data directory.
    root = Path(data_dir).resolve()
    target = path.resolve()
    if root != target and root not in target.parents:
        return False, "path_escape"
    if target.is_file():
        target.unlink()
    return True, "deleted"
