"""SanketPTT receive path. The MQTT client is fake; nothing dials a public broker."""

import json
import struct

import pytest

from soundcore.audio.capture import PipeWirePlayback
from soundcore.audio.recorder import fanout
from soundcore.audio.sanketptt import (
    AUDIO_WILDCARD,
    PREFIX,
    PRESENCE_WILDCARD,
    PUBLIC_BROKERS,
    RATE,
    SanketCapture,
    SanketHub,
    apply_broker_auth,
    clean_channel,
    decode_audio_payload,
    device_id_for,
    parse_sanket_source,
    redact_broker,
    resample_s16le,
    web_url,
)


class FakeClient:
    def __init__(self):
        self.subs = []
        self.on_connect = None
        self.on_message = None
        self.on_disconnect = None
        self.host = ""

    def ws_set_options(self, path="/"):
        return None

    def tls_set(self):
        return None

    def username_pw_set(self, user, password):
        return None

    def reconnect_delay_set(self, low, high):
        return None

    def connect_async(self, host, port, keepalive=20):
        self.host = host
        if self.on_connect:
            self.on_connect(self, None, None, 0)

    def loop_start(self):
        return None

    def subscribe(self, topics):
        self.subs.extend(topics)

    def disconnect(self):
        return None

    def loop_stop(self):
        return None


def _factory(store):
    def factory(_url):
        client = FakeClient()
        store.append(client)
        return client

    return factory


def _topics(client):
    return [item[0] for item in client.subs]


def test_url_parsing_uses_the_web_ch_param():
    source = parse_sanket_source("https://vibhvesh.github.io/sanketptt/?ch=police1")
    assert source.channel == "police1"
    assert device_id_for(source.channel) == "sanketptt:police1"
    assert web_url("police1") == "https://vibhvesh.github.io/sanketptt/?ch=police1"
    listen = parse_sanket_source("https://vibhvesh.github.io/sanketptt/?ch=City-Control&listen=1&name=Desk&embed=1")
    assert listen.channel == "City-Control"
    assert listen.listen_only is True
    assert listen.device_name == "Desk"
    assert parse_sanket_source("https://vibhvesh.github.io/sanketptt/").channel == "virtual"
    assert parse_sanket_source("  Fire_2  ").channel == "Fire_2"
    assert clean_channel("no spaces!") == "nospaces"


def test_payload_decode_and_resample():
    pcm = struct.pack("<4h", 0, 1000, -1000, 500)
    payload = bytes([3]) + b"ada" + pcm
    speaker, audio = decode_audio_payload(payload)
    assert speaker == "ada"
    assert audio == pcm
    up = resample_s16le(struct.pack("<" + "h" * 12, *range(12)), RATE, 16000)
    assert len(up) // 2 == 16


def test_one_connection_serves_many_channels_and_discovery():
    made = []
    hub = SanketHub(mqtt_factory=_factory(made), clock=lambda: 1_700_000_000.0)
    hub.set_channels(["police1", "fire2"])
    hub.set_discover(True)
    hub.connect()
    assert len(made) == 1
    topics = _topics(made[0])
    assert f"{PREFIX}police1/audio" in topics
    assert f"{PREFIX}fire2/audio" in topics
    assert f"{PREFIX}police1/floor" in topics
    assert PRESENCE_WILDCARD in topics
    assert AUDIO_WILDCARD in topics
    assert f"{PREFIX}+/floor" in topics
    now = 1_700_000_000.0
    hub.handle(
        f"{PREFIX}city-control/presence/abc",
        json.dumps({"id": "abc", "name": "Ada", "ts": int(now * 1000)}),
    )
    hub.handle(
        f"{PREFIX}city-control/floor",
        json.dumps({"t": "claim", "id": "abc", "name": "Ada"}),
    )
    heard = hub.status()["channels"]["city-control"]
    assert heard["online"] == 1
    assert heard["talking"] == "Ada"
    assert "police1" in hub.status()["subscribed"]
    assert "fire2" in hub.status()["subscribed"]


def test_broker_fallback_rotates_with_backoff():
    hub = SanketHub(mqtt_factory=_factory([]))
    assert hub.broker_url == PUBLIC_BROKERS[0]
    hub.fail_connect()
    assert hub.broker_url == PUBLIC_BROKERS[1]
    assert hub.backoff_s >= 0.6


def test_eight_sanket_channels_share_one_connection():
    made = []
    hub = SanketHub(mqtt_factory=_factory(made), clock=lambda: 1_700_000_000.0)
    names = [f"ch{i}" for i in range(8)]
    hub.set_channels(names)
    hub.connect()
    assert len(made) == 1
    topics = _topics(made[0])
    for name in names:
        assert f"{PREFIX}{name}/audio" in topics
        assert f"{PREFIX}{name}/floor" in topics


def test_shiftr_login_is_not_in_source_and_status_is_redacted():
    from pathlib import Path

    text = (Path(__file__).resolve().parents[1] / "soundcore" / "audio" / "sanketptt.py").read_text(encoding="utf-8")
    assert "public:public@" not in text
    urls = apply_broker_auth(list(PUBLIC_BROKERS), "public", "public")
    assert urls[0].startswith("wss://public:public@public.cloud.shiftr.io")
    assert "public:" not in urls[1]
    hub = SanketHub(clock=lambda: 1_700_000_000.0)
    hub.set_brokers(urls)
    dumped = json.dumps(hub.status())
    assert "public:public" not in dumped
    assert redact_broker(urls[0]) == "wss://public.cloud.shiftr.io"


def test_default_cap_holds_eight_sanket_inputs(data_dir):
    from soundcore.config import Settings
    from soundcore.db import session_scope
    from soundcore.ops import OpsError, save_channel

    settings = Settings(secret_key="x", database_url="sqlite://", data_dir=data_dir, stream_cap=8)
    with session_scope() as db:
        for index in range(8):
            save_channel(
                db,
                settings,
                channel_id=None,
                name=f"S{index}",
                alsa_device=f"sanketptt:ch{index}",
                device_id=f"sanketptt:ch{index}",
                audio_backend="sanketptt",
                sample_rate=16000,
                gain_db=0,
                enabled=True,
                direction="in",
                device_kind="radio",
            )
        with pytest.raises(OpsError, match="Stream cap"):
            save_channel(
                db,
                settings,
                channel_id=None,
                name="S9",
                alsa_device="sanketptt:ch9",
                device_id="sanketptt:ch9",
                audio_backend="sanketptt",
                sample_rate=16000,
                gain_db=0,
                enabled=True,
                direction="in",
                device_kind="radio",
            )


def test_saved_broker_list_drops_passwords_and_overrides_env(data_dir):
    from soundcore.config import Settings
    from soundcore.db import session_scope
    from soundcore.audio.sanketptt import parse_broker_list, resolve_brokers
    from soundcore.models import Setting

    settings = Settings(
        secret_key="x",
        database_url="sqlite://",
        data_dir=data_dir,
        sanket_brokers="wss://broker.hivemq.com:8884/mqtt",
        sanket_broker_username="public",
        sanket_broker_password="public",
    )
    with session_scope() as db:
        db.add(
            Setting(
                key="sanket_brokers",
                value="wss://public:secret@public.cloud.shiftr.io\nwss://broker.emqx.io:8084/mqtt",
            )
        )
        db.flush()
        urls = resolve_brokers(settings, db)
    assert urls[0].startswith("wss://public:public@public.cloud.shiftr.io")
    assert "secret" not in urls[0]
    assert urls[1] == "wss://broker.emqx.io:8084/mqtt"
    with pytest.raises(ValueError):
        parse_broker_list("https://example.com/file")


def test_sanket_audio_is_routed_to_pipewire_playback():
    made = []
    hub = SanketHub(mqtt_factory=_factory(made), clock=lambda: 1_700_000_000.0)
    capture = SanketCapture("police1", 16000, hub)
    capture.start()
    hub.handle(
        f"{PREFIX}police1/floor",
        json.dumps({"t": "claim", "id": "ab", "name": "Bea"}),
    )
    samples = struct.pack("<" + "h" * 6000, *([1000] * 6000))
    hub.handle(f"{PREFIX}police1/audio", bytes([2]) + b"ab" + samples)
    assert capture.in_transmission
    played = []

    class Proc:
        def __init__(self):
            self.stdin = self
            self.stderr = None
            self.buf = bytearray()
            self.alive = True

        def write(self, data):
            self.buf.extend(data)
            return len(data)

        def flush(self):
            return None

        def close(self):
            return None

        def poll(self):
            return None if self.alive else 1

        def terminate(self):
            self.alive = False

        def kill(self):
            self.alive = False

        def wait(self, timeout=None):
            return 0

    proc = Proc()
    playback = PipeWirePlayback(
        "bluez_output.AA.1",
        16000,
        popen=lambda *_a, **_k: proc,
        which=lambda name: "/usr/bin/paplay" if name == "paplay" else None,
        retry_interval=0,
    )
    playback.start()
    chunk = capture.read(8000)
    fanout(chunk, None, [playback], None, 9, 16000)
    assert chunk
    assert bytes(proc.buf) == chunk
    hub.handle(f"{PREFIX}police1/floor", json.dumps({"t": "release", "id": "ab", "name": "Bea"}))
    hub.expire()
    assert capture.read(100) == b""


def test_short_transmission_is_not_emitted():
    hub = SanketHub(clock=lambda: 1_700_000_000.0)
    capture = SanketCapture("virtual", 16000, hub)
    capture.start()
    hub.handle(f"{PREFIX}virtual/floor", json.dumps({"t": "claim", "id": "zz", "name": "Cy"}))
    hub.handle(f"{PREFIX}virtual/audio", bytes([2]) + b"zz" + struct.pack("<10h", *([1] * 10)))
    hub.handle(f"{PREFIX}virtual/floor", json.dumps({"t": "release", "id": "zz", "name": "Cy"}))
    assert capture.read(100) == b""
    assert capture.in_transmission is False
