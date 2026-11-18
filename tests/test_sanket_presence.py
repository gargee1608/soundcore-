"""SanketPTT stays powered on: presence, heartbeat, will, and reconnect. No live broker."""

import json

from soundcore.audio.sanketptt import HEARTBEAT_S, PREFIX, PUBLIC_BROKERS, SanketHub


class RecordingClient:
    def __init__(self):
        self.subs = []
        self.published = []
        self.wills = []
        self.on_connect = None
        self.on_message = None
        self.on_disconnect = None

    def ws_set_options(self, path="/"):
        return None

    def tls_set(self):
        return None

    def username_pw_set(self, user, password):
        return None

    def reconnect_delay_set(self, low, high):
        return None

    def will_set(self, topic, payload=None, qos=0, retain=False):
        body = payload if isinstance(payload, (bytes, bytearray)) else str(payload or "").encode()
        self.wills.append({"topic": topic, "payload": bytes(body), "retain": retain, "qos": qos})

    def connect_async(self, host, port, keepalive=20):
        if self.on_connect:
            self.on_connect(self, None, None, 0)

    def loop_start(self):
        return None

    def subscribe(self, topics):
        self.subs.extend(topics)

    def publish(self, topic, payload, qos=0, retain=False):
        body = payload if isinstance(payload, (bytes, bytearray)) else str(payload).encode()
        self.published.append({"topic": topic, "payload": bytes(body), "retain": retain})

    def disconnect(self):
        return None

    def loop_stop(self):
        return None


def _hub(now):
    made = []

    def factory(_url):
        client = RecordingClient()
        made.append(client)
        return client

    hub = SanketHub(mqtt_factory=factory, clock=lambda: now[0])
    return hub, made


def _presence(client):
    return [item for item in client.published if "/presence/" in item["topic"]]


def test_enabled_channel_auto_joins_with_presence_and_will():
    now = [1_700_000_000.0]
    hub, made = _hub(now)
    hub.set_channels(["virtual", "police1"])
    hub.set_presence(True, "SoundCore-pi", "stable01")
    hub.connect()
    assert len(made) == 1
    client = made[0]
    topics = [item[0] for item in client.subs]
    assert f"{PREFIX}virtual/presence/+" in topics
    assert f"{PREFIX}virtual/audio" in topics
    assert f"{PREFIX}police1/floor" in topics
    assert f"{PREFIX}police1/sstv" in topics
    assert {item["topic"] for item in client.wills} == {
        f"{PREFIX}virtual/presence/stable01",
        f"{PREFIX}police1/presence/stable01",
    }
    assert all(item["payload"] == b"" and item["retain"] is True for item in client.wills)
    bodies = []
    for item in _presence(client):
        assert item["retain"] is True
        assert item["payload"]
        bodies.append(json.loads(item["payload"]))
    assert {item["topic"].split("/")[1] for item in _presence(client)} == {"virtual", "police1"}
    assert bodies[0]["id"] == "stable01"
    assert bodies[0]["name"] == "SoundCore-pi"
    assert bodies[0]["ts"] == int(now[0] * 1000)
    assert all("/floor" not in item["topic"] and not item["topic"].endswith("/audio") for item in client.published)
    assert hub.status()["power"] == "online"
    assert hub.broker_url == PUBLIC_BROKERS[0]


def test_heartbeat_repeats_presence_on_the_web_interval():
    now = [1_700_000_000.0]
    hub, made = _hub(now)
    hub.set_channels(["virtual"])
    hub.set_presence(True, "SoundCore-pi", "stable01")
    hub.connect()
    assert len(_presence(made[0])) == 1
    now[0] += HEARTBEAT_S - 0.1
    hub.tick()
    assert len(_presence(made[0])) == 1
    now[0] += 0.2
    hub.tick()
    assert len(_presence(made[0])) == 2
    second = json.loads(_presence(made[0])[1]["payload"])
    assert second["ts"] == int(now[0] * 1000)
    assert all("/floor" not in item["topic"] and not item["topic"].endswith("/audio") for item in made[0].published)


def test_drop_reconnects_and_publishes_presence_again():
    now = [1_700_000_000.0]
    hub, made = _hub(now)
    hub.set_channels(["virtual"])
    hub.set_presence(True, "SoundCore-pi", "stable01")
    hub.connect()
    hub._on_disconnect(made[0], None, None, 1)
    assert hub.connected is False
    assert hub.status()["power"] == "reconnecting"
    hub.connect()
    assert len(made) == 2
    assert hub.connected is True
    assert len(_presence(made[1])) == 1
    assert made[1].wills[0]["topic"] == f"{PREFIX}virtual/presence/stable01"
    assert made[1].wills[0]["payload"] == b""
    assert all("/floor" not in item["topic"] and not item["topic"].endswith("/audio") for item in made[0].published + made[1].published)


def test_client_id_stays_on_disk(tmp_path):
    from soundcore.audio.sanketptt import load_client_id

    first = load_client_id(tmp_path)
    second = load_client_id(tmp_path)
    assert first == second
    assert len(first) == 8
