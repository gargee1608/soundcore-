"""Per-channel capture threads: record, relay, and publish live audio together."""

from __future__ import annotations

import json
import logging
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from soundcore.audio.naming import build_recording_relative_path, new_unique_id
from soundcore.ops import channel_endpoint
from soundcore.audio.pcm import pcm_rms
from soundcore.audio.recorder import WavRecorder, fanout
from soundcore.db import session_scope
from soundcore.models import Channel, Recording, Route, StorageState, Stream, utcnow
from soundcore.notify import notify
from soundcore.settings_store import load_runtime
from soundcore.storage.guard import admission, measure_disk
from soundcore.transcription.worker import enqueue_transcription

logger = logging.getLogger(__name__)


@dataclass
class ChannelSpec:
    id: int
    name: str
    slug: str
    alsa_device: str
    sample_rate: int
    gain_db: float
    stream_id: int | None
    device_id: str = ""
    audio_backend: str = "alsa"

    def __post_init__(self) -> None:
        if not self.device_id:
            self.device_id = self.alsa_device

    @property
    def signature(self) -> tuple:
        return (self.device_id, self.audio_backend, self.sample_rate, round(self.gain_db, 2))


@dataclass
class RouteTarget:
    input_channel_id: int
    output_channel_id: int
    device_id: str
    sample_rate: int
    backend: str
    output_name: str
    connected: bool
    detail: str


def plan_routes(route_rows, outputs_by_id, available_ids: set[str]) -> tuple[dict[int, list[RouteTarget]], list[dict]]:
    """Keep a route even when its sink is gone, and say so instead of dropping it.

    Disabled outputs and missing channel rows are reported too. Only enabled
    routes are expected in route_rows.
    """
    plan: dict[int, list[RouteTarget]] = {}
    notices: list[dict] = []
    for route in route_rows:
        output = outputs_by_id.get(route.output_channel_id)
        if output is None:
            notices.append(
                {
                    "input_channel_id": route.input_channel_id,
                    "output_channel_id": route.output_channel_id,
                    "reason": "missing",
                    "detail": "Destination channel is gone",
                    "connected": False,
                }
            )
            continue
        device_id, backend = channel_endpoint(output)
        if not output.enabled:
            notices.append(
                {
                    "input_channel_id": route.input_channel_id,
                    "output_channel_id": output.id,
                    "device_id": device_id,
                    "backend": backend,
                    "name": output.name,
                    "reason": "disabled",
                    "detail": f"{output.name} is disabled",
                    "connected": False,
                }
            )
            continue
        connected = device_id in available_ids
        detail = "" if connected else f"{output.name} is not available ({device_id})"
        if not connected:
            notices.append(
                {
                    "input_channel_id": route.input_channel_id,
                    "output_channel_id": output.id,
                    "device_id": device_id,
                    "backend": backend,
                    "name": output.name,
                    "reason": "disconnected",
                    "detail": detail,
                    "connected": False,
                }
            )
        target = RouteTarget(
            input_channel_id=route.input_channel_id,
            output_channel_id=output.id,
            device_id=device_id,
            sample_rate=output.sample_rate,
            backend=backend,
            output_name=output.name,
            connected=connected,
            detail=detail,
        )
        plan.setdefault(route.input_channel_id, []).append(target)
    return plan, notices


class AudioService:
    def __init__(self, settings, backend, bus):
        self.settings = settings
        self.backend = backend
        self.bus = bus
        self._levels: dict[int, float] = {}
        self._level_lock = threading.Lock()
        self._route_lock = threading.Lock()
        self._routes: dict[int, list[RouteTarget]] = {}
        self._notices: list[dict] = []
        self._playback_status: dict[str, dict] = {}
        self._status_lock = threading.Lock()
        self._stop = threading.Event()
        self._workers: dict[int, tuple[tuple, threading.Event, threading.Thread]] = {}
        self._thread: threading.Thread | None = None
        self._runtime_cache = None
        self._runtime_at = 0.0

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._supervise, name="soundcore-audio", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        workers = list(self._workers.values())
        for _sig, stop, _thread in workers:
            stop.set()
        for _sig, _stop_evt, thread in workers:
            thread.join(timeout=3)
        self._workers.clear()
        if self._thread:
            self._thread.join(timeout=3)

    def level(self, channel_id: int) -> float:
        with self._level_lock:
            return self._levels.get(channel_id, 0.0)

    def levels(self) -> dict[int, float]:
        with self._level_lock:
            return dict(self._levels)

    def _set_level(self, channel_id: int, value: float) -> None:
        with self._level_lock:
            self._levels[channel_id] = value

    def _supervise(self) -> None:
        while not self._stop.is_set():
            try:
                self._reconcile()
            except Exception:
                logger.exception("audio supervisor failed")
            self._stop.wait(2)

    def _reconcile(self) -> None:
        with session_scope() as session:
            rows = session.query(Channel).filter_by(direction="in", enabled=True).all()
            streams = {stream.channel_id: stream.id for stream in session.query(Stream).all()}
            desired = {}
            for row in rows:
                device_id, backend_name = channel_endpoint(row)
                desired[row.id] = ChannelSpec(
                    id=row.id,
                    name=row.name,
                    slug=row.slug,
                    alsa_device=row.alsa_device,
                    sample_rate=row.sample_rate,
                    gain_db=row.gain_db,
                    stream_id=streams.get(row.id),
                    device_id=device_id,
                    audio_backend=backend_name,
                )
            route_rows = session.query(Route).filter_by(enabled=True).all()
            outputs = {row.id: row for row in session.query(Channel).filter_by(direction="out").all()}
            try:
                available = {
                    device.id
                    for device in self.backend.list_devices()
                    if device.available
                }
            except Exception:
                logger.exception("device scan failed")
                available = set()
            routes, notices = plan_routes(route_rows, outputs, available)
            desired_specs = desired
            self._sync_sanket(session, rows)
        with self._route_lock:
            self._routes = routes
            self._notices = notices
        self._publish_status(desired_specs, available)

        stale = [channel_id for channel_id in self._workers if channel_id not in desired]
        for channel_id in stale:
            _sig, stop, thread = self._workers.pop(channel_id)
            stop.set()
            thread.join(timeout=3)
            self._set_level(channel_id, 0.0)

        for channel_id, spec in desired_specs.items():
            current = self._workers.get(channel_id)
            if current and (current[0] != spec.signature or not current[2].is_alive()):
                _sig, stop, thread = self._workers.pop(channel_id)
                stop.set()
                thread.join(timeout=3)
                current = None
            if current is None:
                stop = threading.Event()
                thread = threading.Thread(
                    target=self._run_channel,
                    args=(spec, stop),
                    name=f"soundcore-ch-{spec.slug}",
                    daemon=True,
                )
                self._workers[channel_id] = (spec.signature, stop, thread)
                thread.start()

    def _sync_sanket(self, session, enabled_inputs) -> None:
        from soundcore.audio.sanketptt import channel_from_device, device_id_for, get_hub
        from soundcore.models import Channel, Setting
        from soundcore.ops import save_channel

        def flag(key: str) -> bool:
            row = session.get(Setting, key)
            return bool(row and row.value.strip().lower() in {"1", "true", "yes", "on"})

        hub = get_hub()
        hub.data_dir = Path(self.settings.data_dir)
        names = []
        for row in enabled_inputs:
            _device, backend_name = channel_endpoint(row)
            if backend_name == "sanketptt":
                names.append(channel_from_device(row.device_id or row.alsa_device))
        discover = flag("sanket_discover")
        auto_add = flag("sanket_auto_add")
        hub.set_channels(names)
        hub.set_discover(discover or auto_add)
        if names or discover or auto_add:
            hub.ensure()
        hub.expire()
        if auto_add:
            known = {
                channel_from_device(row.device_id or row.alsa_device)
                for row in session.query(Channel).all()
                if (row.audio_backend or "") == "sanketptt" or (row.device_id or "").startswith("sanketptt:")
            }
            for name in hub.status().get("channels", {}):
                if name in known:
                    continue
                try:
                    save_channel(
                        session,
                        self.settings,
                        channel_id=None,
                        name=name,
                        alsa_device=device_id_for(name),
                        device_id=device_id_for(name),
                        audio_backend="sanketptt",
                        sample_rate=self.settings.default_sample_rate,
                        gain_db=0.0,
                        enabled=False,
                        direction="in",
                        device_kind="radio",
                        actor="sanket-discover",
                    )
                    known.add(name)
                except Exception:
                    logger.exception("could not auto-add SanketPTT channel %s", name)
        hub.write_status()

    def _routes_for(self, channel_id: int) -> list[RouteTarget]:
        with self._route_lock:
            return list(self._routes.get(channel_id, []))

    def _run_channel(self, spec: ChannelSpec, stop: threading.Event) -> None:
        capture = self.backend.open_capture(
            spec.device_id, spec.sample_rate, spec.gain_db, backend_name=spec.audio_backend
        )
        try:
            capture.start()
        except Exception:
            logger.exception("capture failed to start for %s", spec.device_id)
            return
        playbacks = []
        open_ids: list[str] = []
        writer: WavRecorder | None = None
        recording_id: int | None = None
        chunk_started = 0.0
        last_disk_alert = 0.0
        frame_seconds = 0.02
        frames = max(1, int(spec.sample_rate * frame_seconds))
        # Pace to the device clock. ALSA reads already block; the simulated
        # backend returns immediately, and without this a chunk fills with
        # minutes of audio in a few seconds.
        next_tick = time.monotonic()
        try:
            while not stop.is_set():
                self._sync_playbacks(spec, playbacks, open_ids)
                runtime = self._runtime()
                now_mono = time.monotonic()
                # SanketPTT is quiet between calls. Do not open a WAV for that
                # silence. A file opens when audio arrives and closes when the
                # floor is released or the 2.5 s floor timeout ends it. A call
                # longer than the chunk length still rotates like any other input.
                gated = bool(getattr(capture, "records_only_while_active", False))
                if gated and not getattr(capture, "in_transmission", False):
                    try:
                        capture.read(frames)
                    except Exception:
                        logger.exception("read failed on %s", spec.device_id)
                    if writer is not None:
                        self._finalize(recording_id, writer)
                        writer = None
                        recording_id = None
                    self._set_level(spec.id, 0.0)
                    stop.wait(0.05)
                    continue
                rotate = writer is None or (now_mono - chunk_started) >= runtime.chunk_seconds
                if rotate:
                    blocked = self._disk_blocks_new_file()
                    if blocked and writer is not None:
                        if now_mono - last_disk_alert > 60:
                            last_disk_alert = now_mono
                            self._alert(
                                "warning",
                                "disk_reserve",
                                f"{spec.name} is still recording into the open file because free space is at the 5% reserve.",
                            )
                    elif blocked and writer is None:
                        if now_mono - last_disk_alert > 60:
                            last_disk_alert = now_mono
                            self._alert(
                                "error",
                                "disk_reserve",
                                f"Not opening a new file for {spec.name}. Less than 5% of the volume is free.",
                            )
                        stop.wait(1)
                        continue
                    else:
                        self._finalize(recording_id, writer)
                        writer, recording_id = self._open_chunk(spec, runtime)
                        chunk_started = now_mono
                        if writer is None:
                            stop.wait(1)
                            continue
                try:
                    pcm = capture.read(frames)
                except Exception:
                    logger.exception("read failed on %s", spec.device_id)
                    stop.wait(1)
                    continue
                if not pcm:
                    stop.wait(0.2)
                    continue
                fanout(pcm, writer, list(playbacks), self.bus, spec.id, spec.sample_rate)
                self._set_level(spec.id, pcm_rms(pcm))
                self._note_playbacks(playbacks)
                next_tick += frame_seconds
                delay = next_tick - time.monotonic()
                if delay > 0:
                    stop.wait(delay)
                else:
                    next_tick = time.monotonic()
        finally:
            self._finalize(recording_id, writer)
            for playback in playbacks:
                try:
                    playback.stop()
                except Exception:
                    logger.debug("playback stop failed", exc_info=True)
            try:
                capture.stop()
            except Exception:
                logger.debug("capture stop failed", exc_info=True)
            self._set_level(spec.id, 0.0)

    def _sync_playbacks(self, spec: ChannelSpec, playbacks: list, open_ids: list[str]) -> None:
        desired = self._routes_for(spec.id)
        desired_ids = [target.device_id for target in desired]
        if desired_ids == open_ids and all(_playback_alive(item) for item in playbacks):
            return
        if desired_ids == open_ids:
            for playback in playbacks:
                if not _playback_alive(playback):
                    try:
                        playback.start()
                    except Exception:
                        logger.exception("playback restart failed")
            self._note_playbacks(playbacks)
            return
        for playback in playbacks:
            try:
                playback.stop()
            except Exception:
                logger.debug("playback stop failed", exc_info=True)
        playbacks.clear()
        open_ids.clear()
        for target in desired:
            playback = self.backend.open_playback(
                target.device_id,
                target.sample_rate or spec.sample_rate,
                backend_name=target.backend,
            )
            try:
                playback.start()
            except Exception as exc:
                playback.last_error = str(exc)
                logger.exception("playback failed to start for %s", target.device_id)
            playbacks.append(playback)
            open_ids.append(target.device_id)
        self._note_playbacks(playbacks)

    def _note_playbacks(self, playbacks: list) -> None:
        reports = {}
        for playback in playbacks:
            if hasattr(playback, "status"):
                report = playback.status()
            else:
                report = {
                    "backend": "alsa",
                    "device": getattr(playback, "device_id", ""),
                    "running": True,
                    "last_error": getattr(playback, "last_error", ""),
                }
            reports[str(report.get("device") or id(playback))] = report
        with self._status_lock:
            self._playback_status.update(reports)

    def _publish_status(self, desired: dict, available: set[str]) -> None:
        with self._route_lock:
            notices = list(self._notices)
            routes = {key: list(value) for key, value in self._routes.items()}
        with self._status_lock:
            playbacks = list(self._playback_status.values())
        last_error = ""
        for notice in notices:
            if notice.get("reason") == "disconnected":
                last_error = notice.get("detail") or last_error
        for report in playbacks:
            if report.get("last_error"):
                last_error = report["last_error"]
        selected_inputs = [
            {
                "id": spec.id,
                "name": spec.name,
                "device_id": spec.device_id,
                "backend": spec.audio_backend,
            }
            for spec in desired.values()
        ]
        selected_outputs = []
        for targets in routes.values():
            for target in targets:
                selected_outputs.append(
                    {
                        "id": target.output_channel_id,
                        "name": target.output_name,
                        "device_id": target.device_id,
                        "backend": target.backend,
                        "connected": target.connected,
                        "detail": target.detail,
                    }
                )
        payload = {
            "input_backends": sorted({spec.audio_backend for spec in desired.values()}) or ["alsa"],
            "output_backends": sorted({item.get("backend", "") for item in selected_outputs if item.get("backend")}),
            "selected_inputs": selected_inputs,
            "selected_outputs": selected_outputs,
            "available_device_ids": sorted(available),
            "notices": notices,
            "playbacks": playbacks,
            "last_error": last_error,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }
        path = Path(self.settings.data_dir) / "logs" / "audio_status.json"
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(payload), encoding="utf-8")
        except OSError:
            logger.debug("could not write audio status", exc_info=True)


    def _runtime(self):
        now = time.monotonic()
        if self._runtime_cache is not None and now - self._runtime_at < 2:
            return self._runtime_cache
        with session_scope() as session:
            runtime = load_runtime(session, self.settings)
        self._runtime_cache = runtime
        self._runtime_at = now
        return runtime

    def _disk_blocks_new_file(self) -> bool:
        runtime = self._runtime()
        usage = measure_disk(self.settings.data_dir)
        return admission(usage, reserve_ratio=runtime.free_reserve_ratio) == "refuse"

    def _alert(self, level: str, kind: str, message: str) -> None:
        try:
            with session_scope() as session:
                runtime = load_runtime(session, self.settings)
                bridge = _NotifySettings(self.settings, runtime)
                notify(session, bridge, level, kind, message)
        except Exception:
            logger.exception("failed to record audio alert")

    def _open_chunk(self, spec: ChannelSpec, runtime) -> tuple[WavRecorder | None, int | None]:
        try:
            tz = ZoneInfo(runtime.timezone)
        except Exception:
            tz = timezone.utc
        local_now = datetime.now(tz)
        started = local_now.astimezone(timezone.utc)
        unique_id = new_unique_id()
        relative = build_recording_relative_path(spec.slug, local_now, unique_id)
        path = Path(self.settings.data_dir) / relative
        try:
            with session_scope() as session:
                recording = Recording(
                    channel_id=spec.id,
                    stream_id=spec.stream_id,
                    filename=path.name,
                    local_path=relative,
                    started_at=started,
                    storage_state=StorageState.WRITING,
                )
                session.add(recording)
                session.flush()
                recording_id = recording.id
            writer = WavRecorder(path, spec.sample_rate)
            writer.open()
            return writer, recording_id
        except Exception:
            logger.exception("could not open recording for %s", spec.slug)
            return None, None

    def _finalize(self, recording_id: int | None, writer: WavRecorder | None) -> None:
        if writer is None or recording_id is None:
            return
        try:
            stats = writer.close()
        except Exception:
            logger.exception("failed to close wav for recording %s", recording_id)
            return
        try:
            with session_scope() as session:
                recording = session.get(Recording, recording_id)
                if recording is None:
                    return
                if stats["frames"] <= 0:
                    path = Path(self.settings.data_dir) / (recording.local_path or "")
                    if path.is_file():
                        path.unlink()
                    session.delete(recording)
                    return
                ended = utcnow()
                recording.ended_at = ended
                recording.duration_s = stats["duration_s"]
                recording.size_bytes = stats["size_bytes"]
                recording.checksum = stats["checksum"]
                recording.last_accessed_at = ended
                recording.storage_state = StorageState.LOCAL_ONLY
                runtime = load_runtime(session, self.settings)
                if runtime.transcribe_enabled:
                    enqueue_transcription(session, recording)
        except Exception:
            logger.exception("failed to finalize recording %s", recording_id)


def load_audio_status(data_dir) -> dict:
    path = Path(data_dir) / "logs" / "audio_status.json"
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _playback_alive(playback) -> bool:
    proc = getattr(playback, "proc", None)
    if proc is not None and proc.poll() is not None:
        return False
    if getattr(playback, "last_error", "") and proc is None and getattr(playback, "_sock", None) is None:
        return False
    return True

class _NotifySettings:
    def __init__(self, settings, runtime):
        self.smtp_host = settings.smtp_host
        self.smtp_port = settings.smtp_port
        self.smtp_user = settings.smtp_user
        self.smtp_password = settings.smtp_password
        self.smtp_from = settings.smtp_from
        self.smtp_tls = settings.smtp_tls
        self.notify_email = runtime.notify_email
        self.notify_webhook = runtime.notify_webhook
