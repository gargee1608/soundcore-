"""APScheduler wiring. The offload job fires at midnight in the device timezone."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from soundcore.models import S3Object, StorageState
from soundcore.storage.guard import (
    DiskUsage,
    RecordingView,
    emergency_candidates,
    measure_disk,
    midnight_candidates,
    needs_emergency_cleanup,
)
from soundcore.storage.offload import offload_recording, view_from_row

logger = logging.getLogger(__name__)

MIDNIGHT_JOB_ID = "midnight_offload"
GUARD_JOB_ID = "storage_guard"


def build_scheduler(timezone_name: str) -> BackgroundScheduler:
    tz = ZoneInfo(timezone_name)
    return BackgroundScheduler(timezone=tz)


def register_jobs(
    scheduler: BackgroundScheduler,
    timezone_name: str,
    midnight_fn,
    guard_fn,
    *,
    guard_interval_seconds: int = 60,
) -> BackgroundScheduler:
    tz = ZoneInfo(timezone_name)
    scheduler.add_job(
        midnight_fn,
        CronTrigger(hour=0, minute=0, second=0, timezone=tz),
        id=MIDNIGHT_JOB_ID,
        replace_existing=True,
        misfire_grace_time=3600,
        coalesce=True,
        max_instances=1,
    )
    scheduler.add_job(
        guard_fn,
        IntervalTrigger(seconds=guard_interval_seconds, timezone=tz),
        id=GUARD_JOB_ID,
        replace_existing=True,
        coalesce=True,
        max_instances=1,
    )
    return scheduler


def _views(session) -> list[tuple[RecordingView, object]]:
    from soundcore.models import Recording

    rows = session.query(Recording).all()
    paired = []
    for rec in rows:
        s3 = session.query(S3Object).filter_by(recording_id=rec.id).one_or_none()
        verified_at = s3.verified_at if s3 is not None else None
        paired.append((view_from_row(rec, verified_at), rec))
    return paired


def run_midnight_offload(session, uploader, data_dir, *, now: datetime, idle_days: int = 7) -> dict:
    """Select idle files and offload them. Open recordings are never selected."""
    paired = _views(session)
    chosen_ids = {view.id for view in midnight_candidates([p[0] for p in paired], now, idle_days=idle_days)}
    summary = {"considered": len(chosen_ids), "deleted": 0, "kept": 0, "skipped": 0}
    for view, rec in paired:
        if view.id not in chosen_ids:
            continue
        if uploader is None:
            summary["kept"] += 1
            continue
        outcome = offload_recording(session, rec, uploader, data_dir, delete_local=True)
        if outcome == "deleted":
            summary["deleted"] += 1
        elif outcome == "skipped":
            summary["skipped"] += 1
        else:
            summary["kept"] += 1
    return summary


def run_storage_guard(
    session,
    uploader,
    data_dir,
    *,
    now: datetime,
    usage: DiskUsage | None = None,
    used_ratio: float = 0.95,
    playback_grace_seconds: int = 120,
    notify=None,
) -> dict:
    """At the cleanup threshold, offload the oldest files. Never unlink an unverified one."""
    disk = usage if usage is not None else measure_disk(data_dir)
    summary = {
        "triggered": False,
        "used_ratio": disk.used_ratio,
        "deleted": 0,
        "kept": 0,
        "space_recovered": disk.used_ratio < used_ratio,
    }
    if not needs_emergency_cleanup(disk, used_ratio=used_ratio):
        return summary
    summary["triggered"] = True
    message = (
        f"Recordings volume is {disk.used_ratio:.0%} full. "
        "Cleaning up oldest files only after S3 verification. Unverified audio stays on disk."
    )
    if notify:
        notify(session, "warning", "disk_threshold", message, {"used_ratio": disk.used_ratio})
    paired = _views(session)
    grace = timedelta(seconds=playback_grace_seconds)
    ordered = emergency_candidates([p[0] for p in paired], now, playback_grace=grace)
    by_id = {rec.id: rec for _view, rec in paired}
    for view in ordered:
        # Stop once a caller-supplied usage is a static test fixture: we still
        # walk candidates because the fixture cannot shrink. Live calls pass a
        # fresh measurement between files via the loop below only when usage is None.
        rec = by_id[view.id]
        if uploader is None and rec.storage_state != StorageState.VERIFIED_REMOTE:
            summary["kept"] += 1
            continue
        if uploader is None and rec.storage_state == StorageState.VERIFIED_REMOTE:
            outcome = offload_recording(session, rec, _NullUploader(), data_dir, delete_local=True)
        else:
            outcome = offload_recording(session, rec, uploader, data_dir, delete_local=True)
        if outcome == "deleted":
            summary["deleted"] += 1
        else:
            summary["kept"] += 1
        if usage is None:
            disk = measure_disk(data_dir)
            summary["used_ratio"] = disk.used_ratio
            if not needs_emergency_cleanup(disk, used_ratio=used_ratio):
                summary["space_recovered"] = True
                break
    if usage is None:
        disk = measure_disk(data_dir)
        summary["used_ratio"] = disk.used_ratio
        summary["space_recovered"] = disk.used_ratio < used_ratio
    if not summary["space_recovered"] and summary["kept"] and notify:
        notify(
            session,
            "error",
            "disk_stuck",
            "Disk is still past the cleanup line. Unverified recordings were not deleted.",
            {"used_ratio": summary["used_ratio"], "kept": summary["kept"]},
        )
    return summary


class _NullUploader:
    """Placeholder so verified-only deletes can share offload_recording without uploading."""

    bucket = ""
    prefix = ""

    def key_for(self, relative_path: str) -> str:
        return relative_path

    def upload_and_verify(self, local_path, key=None):
        from soundcore.storage.s3 import VerifyResult

        return VerifyResult(ok=False, stage="noop", error="uploader not configured")
