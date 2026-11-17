"""Activity log, the JSON endpoint, and a real AudioService segment close."""

import threading
import time
from pathlib import Path

from soundcore.activity import MAX_EVENTS, build_activity, record_activity
from soundcore.app import create_app
from soundcore.audio.capture import ScriptedCapture
from soundcore.audio.sanketptt import PREFIX, SanketCapture, SanketHub
from soundcore.audio.service import AudioService, ChannelSpec
from soundcore.config import Settings
from soundcore.db import create_all, init_engine, session_scope
from soundcore.models import ActivityEvent, Channel, Recording, StorageState, utcnow
from soundcore.settings_store import ensure_settings


def _settings(tmp_path):
    data = tmp_path / "data"
    data.mkdir(parents=True, exist_ok=True)
    url = f"sqlite:///{tmp_path / 'app.db'}"
    init_engine(url, force=True)
    create_all()
    settings = Settings(
        secret_key="test-secret",
        database_url=url,
        data_dir=data,
        admin_email="admin@example.com",
        admin_password="desk-secret",
        audio_backend="simulated",
        embed_worker=False,
        seed_demo=False,
        transcribe_enabled=False,
        live_pcm_port=0,
    )
    with session_scope() as session:
        ensure_settings(session, settings)
    return settings


def _channel(slug, *, backend="alsa", device="fake:0"):
    with session_scope() as session:
        row = Channel(
            name=slug,
            slug=slug,
            direction="in",
            alsa_device=device,
            audio_backend=backend,
            device_id=device,
            device_kind="radio" if backend == "sanketptt" else "usb",
            sample_rate=16000,
            format="s16le",
            gain_db=0.0,
            enabled=True,
            created_at=utcnow(),
            updated_at=utcnow(),
        )
        session.add(row)
        session.flush()
        return row.id, row.slug


class _Backend:
    def __init__(self, capture):
        self.capture = capture

    def open_capture(self, device_id, sample_rate, gain_db, backend_name=None):
        return self.capture

    def list_devices(self):
        return []


def _wait_closed(channel_id):
    for _ in range(40):
        with session_scope() as session:
            row = session.query(Recording).filter_by(channel_id=channel_id).order_by(Recording.id.desc()).first()
            if row is not None and row.storage_state == StorageState.LOCAL_ONLY and row.duration_s:
                return row.local_path, row.duration_s
        time.sleep(0.05)
    return None, None


def test_finalize_is_a_real_audioservice_method():
    assert callable(AudioService._finalize)
    assert callable(AudioService._open_chunk)
    assert "_finalize" in AudioService.__dict__


def test_alsa_style_run_channel_closes_a_segment(tmp_path):
    settings = _settings(tmp_path)
    channel_id, slug = _channel("mic")
    pcm = b"\x01\x00" * 200
    capture = ScriptedCapture(pcm, sample_rate=16000)
    service = AudioService(settings, _Backend(capture), None)
    spec = ChannelSpec(
        id=channel_id,
        name="mic",
        slug=slug,
        alsa_device="fake:0",
        sample_rate=16000,
        gain_db=0.0,
        stream_id=None,
        device_id="fake:0",
        audio_backend="alsa",
    )
    stop = threading.Event()
    thread = threading.Thread(target=service._run_channel, args=(spec, stop), daemon=True)
    thread.start()
    path, duration = _wait_closed(channel_id)
    if path is None:
        stop.set()
        thread.join(timeout=3)
        path, duration = _wait_closed(channel_id)
    else:
        stop.set()
        thread.join(timeout=3)
    assert path
    assert duration and duration > 0
    assert (Path(settings.data_dir) / path).is_file()
    with session_scope() as session:
        kinds = [row.kind for row in session.query(ActivityEvent).all()]
    assert "file_closed" in kinds


def test_sanket_run_channel_closes_when_the_transmission_ends(tmp_path):
    settings = _settings(tmp_path)
    device = "sanketptt:virtual"
    channel_id, slug = _channel("virtual", backend="sanketptt", device=device)
    hub = SanketHub(clock=lambda: 1_700_000_000.0)
    capture = SanketCapture("virtual", 16000, hub)
    service = AudioService(settings, _Backend(capture), None)
    spec = ChannelSpec(
        id=channel_id,
        name="virtual",
        slug=slug,
        alsa_device=device,
        sample_rate=16000,
        gain_db=0.0,
        stream_id=None,
        device_id=device,
        audio_backend="sanketptt",
    )
    stop = threading.Event()
    thread = threading.Thread(target=service._run_channel, args=(spec, stop), daemon=True)
    thread.start()
    time.sleep(0.15)
    hub.handle(f"{PREFIX}virtual/floor", '{"t":"claim","id":"ab","name":"Bea"}')
    hub.handle(f"{PREFIX}virtual/audio", bytes([2]) + b"ab" + (b"\x10\x00" * 6000))
    time.sleep(0.3)
    hub.handle(f"{PREFIX}virtual/floor", '{"t":"release","id":"ab","name":"Bea"}')
    path, duration = _wait_closed(channel_id)
    stop.set()
    thread.join(timeout=3)
    assert path
    assert duration and duration > 0
    assert (Path(settings.data_dir) / path).is_file()


def test_event_ring_keeps_the_newest_thousand(tmp_path):
    _settings(tmp_path)
    for index in range(MAX_EVENTS + 5):
        record_activity("error", f"event-{index}", channel="mic")
    with session_scope() as session:
        rows = session.query(ActivityEvent).order_by(ActivityEvent.id.asc()).all()
    assert len(rows) == MAX_EVENTS
    assert rows[-1].message == f"event-{MAX_EVENTS + 4}"
    assert rows[0].message == "event-5"


def test_activity_endpoint_returns_snapshot_and_events(tmp_path):
    settings = _settings(tmp_path)
    record_activity("file_closed", "Closed clip.wav", channel="mic")
    app = create_app(settings)
    client = app.test_client()
    page = client.get("/login")
    token = page.get_data(as_text=True).split('name="csrf_token" value="', 1)[1].split('"', 1)[0]
    client.post(
        "/login",
        data={"email": "admin@example.com", "password": "desk-secret", "csrf_token": token},
    )
    denied = client.get("/api/activity")
    # The previous client is authenticated. A fresh client is not.
    stranger = app.test_client()
    assert stranger.get("/api/activity").status_code in {302, 401}
    body = client.get("/api/activity?kind=file_closed&channel=mic").get_json()
    assert "channels" in body
    assert "routes" in body
    assert "connections" in body
    assert body["connections"]["disk"]["total_bytes"] > 0
    assert body["events"][0]["message"] == "Closed clip.wav"
    assert body["events"][0]["kind"] == "file_closed"
    html = client.get("/activity").get_data(as_text=True)
    assert "Activity" in html
    assert "SanketPTT" in html
    assert denied.status_code == 200
