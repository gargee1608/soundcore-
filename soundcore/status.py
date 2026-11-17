"""Dashboard payload shared by the HTML page and the polling API."""

from __future__ import annotations

from soundcore.capacity import evaluate_capacity
from soundcore.models import Channel, Event, Recording, Route, StorageState, Transcription
from soundcore.settings_store import load_runtime
from soundcore.storage.guard import admission, measure_disk


def console_status(session, settings, audio=None) -> dict:
    runtime = load_runtime(session, settings)
    inputs = session.query(Channel).filter_by(direction="in").order_by(Channel.id).all()
    outputs = session.query(Channel).filter_by(direction="out").order_by(Channel.id).all()
    active = sum(1 for channel in inputs if channel.enabled)
    capacity = evaluate_capacity(active, runtime.stream_cap, warn_ratio=runtime.warn_ratio)
    usage = measure_disk(settings.data_dir)
    disk_level = "ok"
    if usage.used_ratio >= runtime.cleanup_used_ratio or admission(usage, reserve_ratio=runtime.free_reserve_ratio) == "refuse":
        disk_level = "critical"
    elif admission(usage, reserve_ratio=runtime.free_reserve_ratio) == "warn":
        disk_level = "warning"
    if disk_level == "critical":
        disk_message = (
            f"Storage is {usage.used_ratio:.0%} full. New streams are refused. "
            "Only recordings with a verified S3 copy can be removed."
        )
    elif disk_level == "warning":
        disk_message = f"Free space is getting low ({usage.free_ratio:.0%} free). The 5% reserve is close."
    else:
        disk_message = f"{usage.free_ratio:.0%} free. The appliance keeps at least 5% of the volume unused."

    levels = audio.levels() if audio is not None else {}
    routes = session.query(Route).filter_by(enabled=True).all()
    route_labels = []
    output_by_id = {channel.id: channel for channel in outputs}
    input_by_id = {channel.id: channel for channel in inputs}
    for route in routes:
        src = input_by_id.get(route.input_channel_id)
        dst = output_by_id.get(route.output_channel_id)
        if src and dst:
            route_labels.append(
                {
                    "id": route.id,
                    "input_id": src.id,
                    "output_id": dst.id,
                    "input": src.name,
                    "output": dst.name,
                }
            )
    alerts = (
        session.query(Event).filter(Event.acked_at.is_(None)).order_by(Event.id.desc()).limit(6).all()
    )
    open_recordings = session.query(Recording).filter_by(storage_state=StorageState.WRITING).count()
    recording_count = session.query(Recording).count()
    pending_tx = (
        session.query(Transcription)
        .filter(Transcription.status.in_(["pending", "retrying", "running"]))
        .count()
    )
    return {
        "capacity": capacity.as_dict(),
        "disk": {
            "used_bytes": usage.used,
            "total_bytes": usage.total,
            "free_bytes": usage.free,
            "used_ratio": round(usage.used_ratio, 4),
            "free_ratio": round(usage.free_ratio, 4),
            "level": disk_level,
            "message": disk_message,
        },
        "counts": {
            "inputs": len(inputs),
            "outputs": len(outputs),
            "active_inputs": active,
            "open_recordings": open_recordings,
            "recordings": recording_count,
            "pending_transcriptions": pending_tx,
            "alerts": len(alerts),
        },
        "channels": [
            {
                "id": channel.id,
                "name": channel.name,
                "slug": channel.slug,
                "direction": channel.direction,
                "device": channel.device_id or channel.alsa_device,
                "backend": channel.audio_backend or "alsa",
                "kind": channel.device_kind,
                "sample_rate": channel.sample_rate,
                "gain_db": channel.gain_db,
                "enabled": channel.enabled,
                "level": round(levels.get(channel.id, 0.0), 4),
            }
            for channel in inputs + outputs
        ],
        "routes": route_labels,
        "alerts": [
            {
                "id": event.id,
                "level": event.level,
                "kind": event.kind,
                "message": event.message,
                "created_at": event.created_at.isoformat() if event.created_at else "",
            }
            for event in alerts
        ],
        "runtime": {
            "stream_cap": runtime.stream_cap,
            "chunk_seconds": runtime.chunk_seconds,
            "timezone": runtime.timezone,
            "transcribe_enabled": runtime.transcribe_enabled,
        },
    }
