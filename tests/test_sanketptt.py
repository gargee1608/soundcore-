"""SanketPTT receive path. The MQTT client is fake; nothing dials a public broker."""

import json
import struct

from soundcore.audio.capture import PipeWirePlayback
from soundcore.audio.recorder import fanout
from soundcore.audio.sanketptt import (
    AUDIO_WILDCARD,
    BROKERS,
    PREFIX,
    PRESENCE_WILDCARD,
    RATE,
    SanketCapture,
    SanketHub,
    clean_channel,
    decode_audio_payload,
    device_id_for,
    parse_sanket_source,
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
    assert hub.broker_url == BROKERS[0]
    hub.fail_connect()
    assert hub.broker_url == BROKERS[1]
    assert hub.backoff_s >= 0.6


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
