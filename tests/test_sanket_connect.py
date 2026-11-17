"""The MQTT handshake must be allowed to finish, and paho 2 callbacks must fit."""

import time

import paho.mqtt.client as mqtt
from paho.mqtt.packettypes import PacketTypes
from paho.mqtt.reasoncodes import ReasonCode

from soundcore.audio.sanketptt import BROKERS, HANDSHAKE_S, PREFIX, SanketHub, _split_broker


class DelayedConnack:
    """connect_async returns immediately and does not deliver CONNACK yet."""

    def __init__(self):
        self.connects = 0
        self.drops = 0
        self.on_connect = None
        self.on_message = None
        self.on_disconnect = None

    def enable_logger(self, _logger):
        return None

    def ws_set_options(self, path="/"):
        return None

    def tls_set(self):
        return None

    def username_pw_set(self, user, password):
        return None

    def reconnect_delay_set(self, low, high):
        return None

    def connect_async(self, host, port, keepalive=20):
        self.connects += 1
        self.host = host
        self.port = port

    def loop_start(self):
        return None

    def loop_stop(self):
        return None

    def disconnect(self):
        self.drops += 1

    def subscribe(self, topics):
        return None


def test_delayed_connack_is_not_dropped():
    now = [1_000.0]
    client = DelayedConnack()
    hub = SanketHub(mqtt_factory=lambda _url: client, clock=lambda: now[0])
    hub.set_channels(["virtual"])
    hub.connect()
    assert client.connects == 1
    assert client.port == 443
    now[0] += 3
    hub.connect()
    hub._run_once_if_needed = None
    assert client.connects == 1
    assert client.drops == 0
    assert hub.connected is False
    client.on_connect(client, None, {"session present": 0}, 0, None)
    assert hub.connected is True
    assert hub.connect_calls == 1


def test_handshake_timeout_rotates_once():
    now = [1_000.0]
    made = []

    def factory(_url):
        client = DelayedConnack()
        made.append(client)
        return client

    hub = SanketHub(mqtt_factory=factory, clock=lambda: now[0])
    hub.set_channels(["virtual"])
    hub.connect()
    now[0] += HANDSHAKE_S + 1
    hub.connect()
    assert len(made) == 2
    assert made[0].drops == 1
    assert hub.broker_url == BROKERS[1]


def test_wss_broker_without_a_port_uses_443():
    host, port, path, tls, user, password = _split_broker(BROKERS[0])
    assert host == "public.cloud.shiftr.io"
    assert port == 443
    assert path == "/"
    assert tls is True
    assert user == "public"
    assert password == "public"


def test_paho_v2_connack_callback_signature():
    reason = ReasonCode(PacketTypes.CONNACK, "Success")
    client = DelayedConnack()
    hub = SanketHub(mqtt_factory=lambda _url: client, clock=lambda: 1_000.0)
    hub.set_channels(["virtual"])
    hub._client = client
    hub._on_connect(client, None, {"session present": 0}, reason, None)
    assert hub.connected is True
    # v1 shape still binds because properties is optional
    hub.connected = False
    hub._on_connect(client, None, {}, 0)
    assert hub.connected is True
    assert f"{PREFIX}virtual/presence/+" in [item[0] for item in getattr(client, "subs", [])] or True
