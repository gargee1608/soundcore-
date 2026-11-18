"""Receive-only SanketPTT input.

Verified against https://github.com/vibhvesh/sanketptt (web ``index.html`` and
``pi/sanket/mqtt_relay.py``):

- prefix ``ptt-radio-66ab9178/``
- topics ``<prefix><channel>/{presence/<id>,floor,audio,sstv}``
- audio payload ``[n][id bytes][Int16LE PCM at 12 kHz]``
- floor payload ``{"t":"claim"|"hb"|"release","id","name"}``
- presence payload ``{"id","name","ts"}`` (extra fields such as ``loc`` are ignored)
- the web client keeps channel case; Sanket Edge's ``clean_ch`` also lowercases

Broker hostnames match that repo. Usernames and passwords are not stored here.
They come from the environment, and a console list can replace the hostnames.

SoundCore does not publish floor or audio. It publishes presence only when
powered on. Several inputs share one MQTT connection.
"""

from __future__ import annotations

import json
import logging
import struct
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import parse_qs, urlparse

logger = logging.getLogger(__name__)

PREFIX = "ptt-radio-66ab9178/"
RATE = 12000
WEB_BASE = "https://vibhvesh.github.io/sanketptt/"
# Same public hosts as vibhvesh/sanketptt, in the same order, with no userinfo.
# public.cloud.shiftr.io expects the published demo login. That login is read
# from the environment and applied only to this host.
PUBLIC_BROKERS = (
    "wss://public.cloud.shiftr.io",
    "wss://broker.emqx.io:8084/mqtt",
    "wss://broker.hivemq.com:8884/mqtt",
    "wss://test.mosquitto.org:8081",
)
SHIFTR_HOST = "public.cloud.shiftr.io"
_BROKER_SCHEMES = {"ws", "wss", "mqtt", "mqtts"}
FLOOR_TIMEOUT_S = 2.5
PRESENCE_TTL_S = 15.0
MIN_CLIP_S = 0.4
MAX_CLIP_S = 60.0
HEARTBEAT_S = 5.0  # web client HB_MS
STALE_S = 60.0  # Sanket Edge rebuilds a client with no connection this long
HANDSHAKE_S = 15.0  # wait for TLS and CONNACK before dropping the socket
PRESENCE_WILDCARD = PREFIX + "+/presence/+"
AUDIO_WILDCARD = PREFIX + "+/audio"
FLOOR_WILDCARD = PREFIX + "+/floor"

_hub: SanketHub | None = None
_hub_lock = threading.Lock()


def clean_channel(value: str) -> str:
    """Web ``?ch=`` sanitizer: drop characters other than letters, digits, underscore, and hyphen.

    Case is kept. The browser does not lowercase the topic, so a pasted link must
    subscribe to that same name. Sanket Edge's ``clean_ch`` also lowercases; a
    mixed-case name typed only on the Pi would not match the web topic.
    """
    import re

    return re.sub(r"[^A-Za-z0-9_-]", "", str(value or ""))[:40]


def default_device_name() -> str:
    """Web name filter: letters, digits, and . _ : - only, at most 40 characters."""
    import re
    import socket

    host = socket.gethostname().split(".")[0]
    raw = f"SoundCore-{host}"
    cleaned = re.sub(r"[^A-Za-z0-9._:-]", "", raw)[:40]
    return cleaned or "SoundCore"


def load_client_id(data_dir: Path) -> str:
    """Stable 8-character id, same length the web client uses, kept across restarts."""
    import random
    import string

    path = Path(data_dir) / ".sanket_id"
    if path.is_file():
        existing = path.read_text(encoding="utf-8").strip()
        if existing:
            return existing[:10]
    chosen = "".join(random.choices(string.ascii_lowercase + string.digits, k=8))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(chosen, encoding="utf-8")
    return chosen


def web_url(channel: str) -> str:
    return f"{WEB_BASE}?ch={clean_channel(channel)}"


def device_id_for(channel: str) -> str:
    return f"sanketptt:{clean_channel(channel)}"


def channel_from_device(device_id: str) -> str:
    raw = device_id or ""
    if raw.startswith("sanketptt:"):
        raw = raw.split(":", 1)[1]
    return clean_channel(raw)


def parse_broker_list(text: str) -> list[str]:
    """Split a comma- or newline-separated broker list. Drop userinfo."""
    urls: list[str] = []
    seen: set[str] = set()
    for part in str(text or "").replace(",", "\n").splitlines():
        url = part.strip()
        if not url:
            continue
        url = strip_broker_userinfo(url)
        if url in seen:
            continue
        parsed = urlparse(url if "://" in url else f"wss://{url}")
        if parsed.scheme not in _BROKER_SCHEMES or not parsed.hostname:
            raise ValueError(f"Broker URL is not an MQTT WebSocket address: {url}")
        normal = url if "://" in url else f"wss://{url}"
        normal = strip_broker_userinfo(normal)
        if normal not in seen:
            seen.add(normal)
            urls.append(normal)
        if len(urls) >= 8:
            break
    return urls


def strip_broker_userinfo(url: str) -> str:
    parsed = urlparse(url)
    if not parsed.scheme or not parsed.hostname:
        return url
    if not parsed.username and not parsed.password:
        return url
    port = f":{parsed.port}" if parsed.port else ""
    path = parsed.path or ""
    query = f"?{parsed.query}" if parsed.query else ""
    return f"{parsed.scheme}://{parsed.hostname}{port}{path}{query}"


def redact_broker(url: str) -> str:
    return strip_broker_userinfo(url or "")


def apply_broker_auth(urls: list[str], username: str, password: str) -> list[str]:
    """Attach env credentials only to the public Shiftr host, and only when the URL has none."""
    user = (username or "").strip()
    secret = password or ""
    if not user:
        return list(urls)
    from urllib.parse import quote

    applied: list[str] = []
    for url in urls:
        parsed = urlparse(url)
        if parsed.username or (parsed.hostname or "") != SHIFTR_HOST:
            applied.append(url)
            continue
        port = f":{parsed.port}" if parsed.port else ""
        path = parsed.path or ""
        query = f"?{parsed.query}" if parsed.query else ""
        auth = quote(user, safe="")
        if secret:
            auth = f"{auth}:{quote(secret, safe='')}"
        applied.append(f"{parsed.scheme}://{auth}@{parsed.hostname}{port}{path}{query}")
    return applied


def resolve_brokers(settings, session=None) -> list[str]:
    """DB list wins when set, then the env list, then the public hosts."""
    raw = ""
    if session is not None:
        from soundcore.models import Setting

        row = session.get(Setting, "sanket_brokers")
        if row is not None and (row.value or "").strip():
            raw = row.value
    if not raw:
        raw = getattr(settings, "sanket_brokers", "") or ""
    urls = parse_broker_list(raw) if str(raw).strip() else list(PUBLIC_BROKERS)
    if not urls:
        urls = list(PUBLIC_BROKERS)
    return apply_broker_auth(
        urls,
        getattr(settings, "sanket_broker_username", "") or "",
        getattr(settings, "sanket_broker_password", "") or "",
    )


def probe_connection(brokers: list[str], *, timeout: float = 5.0, factory=None) -> dict:
    """Open each broker until one CONNACK arrives. Nothing is published."""
    attempts = []
    rows = [url for url in brokers if url]
    if not rows:
        return {"ok": False, "broker": "", "error": "No brokers configured", "attempts": []}
    for url in rows:
        attempt = _probe_one(url, timeout=timeout, factory=factory)
        attempt["broker"] = redact_broker(url)
        attempts.append(attempt)
        if attempt["ok"]:
            return {"ok": True, "broker": attempt["broker"], "error": "", "attempts": attempts}
    return {"ok": False, "broker": "", "error": attempts[-1]["error"], "attempts": attempts}


def _probe_one(url: str, *, timeout: float, factory) -> dict:
    done = threading.Event()
    outcome = {"ok": False, "error": "no CONNACK"}

    def on_connect(_client, _userdata, _flags, rc, _props=None):
        code = getattr(rc, "value", rc)
        if code in (0, None):
            outcome["ok"] = True
            outcome["error"] = ""
        else:
            outcome["error"] = f"broker refused: {rc}"
        done.set()

    def on_disconnect(_client, _userdata, _flags=None, rc=None, _props=None):
        if not done.is_set():
            outcome["error"] = f"disconnected ({rc})"
            done.set()

    maker = factory or _paho_factory
    try:
        client = maker(url)
    except Exception as exc:
        return {"ok": False, "error": str(exc)}
    client.on_connect = on_connect
    client.on_disconnect = on_disconnect
    try:
        host, port, path, use_tls, username, password = _split_broker(url)
        if hasattr(client, "ws_set_options"):
            client.ws_set_options(path=path or "/")
        if use_tls and hasattr(client, "tls_set"):
            client.tls_set()
        if username and hasattr(client, "username_pw_set"):
            client.username_pw_set(username, password or "")
        if hasattr(client, "connect_async"):
            client.connect_async(host, port, keepalive=10)
        if hasattr(client, "loop_start"):
            client.loop_start()
        done.wait(timeout)
    except Exception as exc:
        outcome["error"] = str(exc)
    finally:
        try:
            client.disconnect()
        except Exception:
            pass
        if hasattr(client, "loop_stop"):
            try:
                client.loop_stop()
            except Exception:
                pass
    if not done.is_set() and not outcome["ok"]:
        outcome["error"] = "timed out waiting for CONNACK"
    return {"ok": bool(outcome["ok"]), "error": outcome["error"]}


@dataclass(frozen=True)
class SanketSource:
    channel: str
    listen_only: bool = False
    device_name: str = ""
    original: str = ""


def parse_sanket_source(text: str) -> SanketSource:
    """Accept a channel name or a SanketPTT link.

    The web app reads ``ch``, ``listen``, ``name``, and ``embed``. ``embed`` only
    changes the page CSS, so it is ignored. ``listen=1`` is recorded and does not
    change receive behaviour, because SoundCore never transmits.
    """
    raw = (text or "").strip()
    if not raw:
        raise ValueError("Enter a SanketPTT channel name or link.")
    listen_only = False
    device_name = ""
    channel = raw
    if "://" in raw or raw.startswith("?") or "ch=" in raw:
        parsed = urlparse(raw if "://" in raw else f"https://vibhvesh.github.io/sanketptt/{raw}")
        query = parse_qs(parsed.query)
        channel = (query.get("ch") or ["virtual"])[0] or "virtual"
        listen_only = (query.get("listen") or ["0"])[0] == "1"
        device_name = (query.get("name") or [""])[0][:40]
    channel = clean_channel(channel)
    if not channel:
        raise ValueError("That channel name is empty after it is cleaned.")
    return SanketSource(channel=channel, listen_only=listen_only, device_name=device_name, original=raw)


def decode_audio_payload(payload: bytes) -> tuple[str, bytes]:
    """Return (speaker id, int16le pcm) from one audio packet."""
    if not payload:
        return "", b""
    n = payload[0]
    if n < 0 or 1 + n > len(payload):
        return "", b""
    speaker = payload[1 : 1 + n].decode("latin-1")
    pcm = payload[1 + n :]
    if len(pcm) % 2:
        pcm = pcm[: len(pcm) - 1]
    return speaker, pcm


def resample_s16le(pcm: bytes, src_rate: int, dst_rate: int) -> bytes:
    """Linear resample, matching Sanket Edge ``resample`` (np.interp over samples)."""
    if not pcm or src_rate == dst_rate:
        return pcm
    count = len(pcm) // 2
    if count == 0:
        return b""
    samples = struct.unpack("<" + "h" * count, pcm[: count * 2])
    if count == 1 or dst_rate <= 0 or src_rate <= 0:
        return pcm
    out_n = int(round(count * dst_rate / src_rate))
    if out_n <= 0:
        return b""
    if out_n == 1:
        return struct.pack("<h", samples[0])
    out = []
    last = count - 1
    for i in range(out_n):
        pos = last * i / (out_n - 1)
        left = int(pos)
        right = min(left + 1, last)
        frac = pos - left
        value = int(samples[left] * (1 - frac) + samples[right] * frac)
        if value > 32767:
            value = 32767
        elif value < -32767:
            value = -32767
        out.append(value)
    return struct.pack("<" + "h" * len(out), *out)


class SanketCapture:
    """Pulls resampled PCM for one channel. Idle air produces no samples."""

    records_only_while_active = True

    def __init__(self, channel: str, sample_rate: int, hub: SanketHub):
        self.channel = channel
        self.sample_rate = sample_rate
        self.hub = hub
        self._buf = bytearray()
        self._lock = threading.Lock()
        self.started = False

    def start(self) -> None:
        self.started = True
        self.hub.add_capture(self)

    def stop(self) -> None:
        self.started = False
        self.hub.remove_capture(self)

    def push(self, pcm_12k: bytes) -> None:
        converted = resample_s16le(pcm_12k, RATE, self.sample_rate)
        if not converted:
            return
        with self._lock:
            self._buf.extend(converted)

    def read(self, frames: int) -> bytes:
        needed = max(0, frames) * 2
        with self._lock:
            if not self._buf:
                return b""
            take = min(needed, len(self._buf))
            out = bytes(self._buf[:take])
            del self._buf[:take]
            return out

    @property
    def in_transmission(self) -> bool:
        with self._lock:
            pending = bool(self._buf)
        return pending or self.hub.floor_active(self.channel)


@dataclass
class _ChannelState:
    users: dict[str, dict] = field(default_factory=dict)
    floor: dict | None = None
    clip_samples: int = 0
    clip_pcm: bytearray = field(default_factory=bytearray)
    last_packet: float = 0.0
    last_talker: str = ""


class SanketHub:
    """One MQTT connection shared by every SanketPTT input."""

    def __init__(self, mqtt_factory=None, data_dir: Path | None = None, clock=None):
        self._factory = mqtt_factory
        self.data_dir = Path(data_dir) if data_dir else None
        self._clock = clock or time.time
        self._lock = threading.Lock()
        self._channels: set[str] = set()
        self._discover = False
        self._state: dict[str, _ChannelState] = {}
        self._captures: dict[str, list[SanketCapture]] = {}
        self._client = None
        self._subs: set[str] = set()
        self.connected = False
        self.last_error = ""
        self.broker_index = 0
        self.backoff_s = 0.3
        self.reconnects = 0
        self._was_connected = False
        self._closed = False
        self._thread: threading.Thread | None = None
        self.presence_enabled = False
        self.presence_name = ""
        self.client_id = ""
        self._last_beat = 0.0
        self._last_activity = 0.0
        self._connecting = False
        self._connect_started = 0.0
        self.connect_calls = 0
        self.published: list[dict] = []
        self._brokers: tuple[str, ...] = ()

    def ensure(self) -> None:
        """Keep one reconnect loop running. Tests call connect() directly instead."""
        if self._thread and self._thread.is_alive():
            return
        self._closed = False
        self._thread = threading.Thread(target=self._run, name="sanketptt", daemon=True)
        self._thread.start()

    def _run(self) -> None:
        while not self._closed:
            if not self.connected and not self._handshake_pending():
                self.connect()
            time.sleep(1.0 if (self.connected or self._handshake_pending()) else min(self.backoff_s, 1.0))
            self.tick()

    def _handshake_pending(self) -> bool:
        if self.connected or not self._connecting:
            return False
        return (self._clock() - self._connect_started) < HANDSHAKE_S

    @property
    def brokers(self) -> tuple[str, ...]:
        return self._brokers or PUBLIC_BROKERS

    @property
    def broker_url(self) -> str:
        rows = self.brokers
        return rows[self.broker_index % len(rows)]

    def set_brokers(self, urls: list[str] | tuple[str, ...]) -> None:
        cleaned = tuple(dict.fromkeys(url for url in urls if url)) or PUBLIC_BROKERS
        current = self.broker_url if self._brokers else ""
        with self._lock:
            self._brokers = cleaned
        if not self.connected or not current:
            return
        if current in cleaned:
            self.broker_index = cleaned.index(current)
            return
        self.broker_index = 0
        self.connected = False
        self.last_error = "broker list changed"
        self._drop_client()

    def set_channels(self, names: set[str] | list[str]) -> None:
        cleaned = {clean_channel(name) for name in names if clean_channel(name)}
        with self._lock:
            old = set(self._channels)
            changed = cleaned != old
            self._channels = cleaned
        if self.connected and self.presence_enabled:
            self._clear_presence(old - cleaned)
            self.beat()
        if changed and self.connected:
            self._sync_subs()

    def set_presence(self, enabled: bool, name: str, client_id: str) -> None:
        """Listen-only 'POWER ON'. Publishes presence; never floor or audio."""
        turned_off = self.presence_enabled and not enabled
        self.presence_enabled = bool(enabled)
        self.presence_name = (name or "")[:40]
        self.client_id = (client_id or self.client_id or "")[:10]
        if turned_off:
            self._clear_presence(self._channels)
        elif self.connected and self.presence_enabled:
            self.beat()

    def set_discover(self, enabled: bool) -> None:
        with self._lock:
            changed = bool(enabled) != self._discover
            self._discover = bool(enabled)
        if changed and self.connected:
            self._sync_subs()

    def add_capture(self, capture: SanketCapture) -> None:
        with self._lock:
            self._captures.setdefault(capture.channel, []).append(capture)

    def remove_capture(self, capture: SanketCapture) -> None:
        with self._lock:
            rows = self._captures.get(capture.channel, [])
            self._captures[capture.channel] = [row for row in rows if row is not capture]

    def floor_active(self, channel: str) -> bool:
        now = self._clock()
        with self._lock:
            state = self._state.get(channel)
            if not state or not state.floor:
                return False
            return now - float(state.floor.get("seen") or 0) <= FLOOR_TIMEOUT_S

    def connect(self) -> None:
        """Open the current broker. A handshake already in flight is left alone."""
        if self._closed or self.connected:
            return
        if self._handshake_pending():
            return
        if self._connecting:
            logger.warning(
                "sanket: handshake to %s exceeded %.0fs with no CONNACK; rotating broker",
                redact_broker(self.broker_url),
                HANDSHAKE_S,
            )
            self._rotate_broker(failed_before_connect=True)
        self._drop_client()
        self.connect_calls += 1
        url = self.broker_url
        logger.info("sanket: connecting to broker %s", redact_broker(url))
        factory = self._factory or _paho_factory
        try:
            client = factory(url)
        except Exception as exc:
            self._connecting = False
            self.last_error = str(exc)
            logger.warning("sanket: broker %s failed to build a client: %s", redact_broker(url), exc, exc_info=True)
            self._rotate_broker(failed_before_connect=True)
            return
        client.on_connect = self._on_connect
        client.on_message = self._on_message
        client.on_disconnect = self._on_disconnect
        if hasattr(client, "enable_logger"):
            client.enable_logger(logger)
        self._client = client
        self._install_wills(client)
        try:
            host, port, path, use_tls, username, password = _split_broker(url)
            if hasattr(client, "ws_set_options"):
                client.ws_set_options(path=path or "/")
            if use_tls and hasattr(client, "tls_set"):
                client.tls_set()
            if username and hasattr(client, "username_pw_set"):
                client.username_pw_set(username, password or "")
            if hasattr(client, "reconnect_delay_set"):
                client.reconnect_delay_set(1, 30)
            self._connecting = True
            self._connect_started = self._clock()
            client.connect_async(host, port, keepalive=20)
            if hasattr(client, "loop_start"):
                client.loop_start()
        except Exception as exc:
            self._connecting = False
            self.last_error = str(exc)
            self.connected = False
            logger.warning("sanket: connect to %s failed: %s", redact_broker(url), exc, exc_info=True)
            self._rotate_broker(failed_before_connect=True)

    def close(self) -> None:
        self._clear_presence(self._channels)
        self._closed = True
        self._drop_client()
        self.connected = False

    def fail_connect(self) -> None:
        """Test hook: the broker refused or never answered."""
        self.connected = False
        self._rotate_broker(failed_before_connect=not self._was_connected)

    def handle(self, topic: str, payload: bytes | str) -> None:
        if isinstance(payload, str):
            payload = payload.encode("utf-8")
        self._handle(topic, payload)

    def status(self) -> dict:
        now = self._clock()
        with self._lock:
            rows = {}
            for name, state in self._state.items():
                rows[name] = self._public_row(state, now)
            subscribed = sorted(self._channels)
        return {
            "connected": self.connected,
            "broker": redact_broker(self.broker_url) if (self.connected or self.last_error) else "",
            "broker_url": redact_broker(self.broker_url),
            "brokers": [redact_broker(url) for url in self.brokers],
            "last_error": self.last_error,
            "reconnects": self.reconnects,
            "backoff_s": self.backoff_s,
            "discover": self._discover,
            "subscribed": subscribed,
            "channels": rows,
            "presence": self.presence_enabled,
            "presence_name": self.presence_name,
            "client_id": self.client_id,
            "power": self.power_state(),
        }

    def write_status(self) -> None:
        if self.data_dir is None:
            return
        path = self.data_dir / "logs" / "sanket_status.json"
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(self.status()), encoding="utf-8")
        except OSError:
            logger.debug("could not write sanket status", exc_info=True)

    def power_state(self) -> str:
        if not self.presence_enabled:
            return "off"
        if self.connected:
            return "online"
        return "reconnecting"

    def beat(self) -> None:
        """Retained presence, same JSON the web client publishes from R.beat."""
        if not self.presence_enabled or not self.client_id or not self.presence_name:
            return
        now_ms = int(self._clock() * 1000)
        payload = json.dumps({"id": self.client_id, "name": self.presence_name, "ts": now_ms})
        for channel in sorted(self._channels):
            self._publish(f"{PREFIX}{channel}/presence/{self.client_id}", payload, retain=True)
        self._last_beat = self._clock()

    def tick(self) -> None:
        """Heartbeat every 5s, and a watchdog if the broker goes quiet."""
        self.expire()
        now = self._clock()
        if self.presence_enabled and self.connected and now - self._last_beat >= HEARTBEAT_S:
            self.beat()
        if self.connected and self._last_activity and now - self._last_activity > STALE_S:
            logger.info("sanket: no MQTT activity for %.0fs on %s; forcing reconnect", now - self._last_activity, redact_broker(self.broker_url))
            self.last_error = "stale connection"
            self.connected = False
            self._drop_client()
            self._rotate_broker(failed_before_connect=False)

    def _install_wills(self, client) -> None:
        if not self.presence_enabled or not self.client_id or not hasattr(client, "will_set"):
            return
        for channel in sorted(self._channels):
            client.will_set(
                f"{PREFIX}{channel}/presence/{self.client_id}",
                b"",
                qos=0,
                retain=True,
            )

    def _publish(self, topic: str, payload, *, retain: bool) -> None:
        if "/floor" in topic or topic.endswith("/audio") or topic.endswith("/sstv"):
            raise RuntimeError(f"SanketPTT must not publish {topic}")
        body = payload if isinstance(payload, (bytes, bytearray)) else str(payload).encode("utf-8")
        self.published.append({"topic": topic, "payload": bytes(body), "retain": retain})
        client = self._client
        if client is not None and hasattr(client, "publish"):
            client.publish(topic, body, qos=0, retain=retain)

    def _clear_presence(self, channels) -> None:
        if not self.client_id:
            return
        for channel in sorted(channels):
            self._publish(f"{PREFIX}{channel}/presence/{self.client_id}", b"", retain=True)

    def subscriptions(self) -> list[str]:
        topics = []
        for channel in sorted(self._channels):
            topics.extend(
                [
                    f"{PREFIX}{channel}/presence/+",
                    f"{PREFIX}{channel}/floor",
                    f"{PREFIX}{channel}/audio",
                    f"{PREFIX}{channel}/sstv",
                ]
            )
        if self._discover:
            topics.extend([PRESENCE_WILDCARD, AUDIO_WILDCARD, FLOOR_WILDCARD])
        return topics

    def _on_connect(self, client, userdata, flags, rc, props=None):
        try:
            code = getattr(rc, "value", rc)
            if code not in (0, None):
                self._connecting = False
                self.last_error = f"broker refused: {rc}"
                self.connected = False
                logger.warning("sanket: on_connect refused reason=%s flags=%s broker=%s", rc, flags, redact_broker(self.broker_url))
                self._rotate_broker(failed_before_connect=True)
                return
            self._connecting = False
            self.connected = True
            relink = self._was_connected
            self._was_connected = True
            self.last_error = ""
            self.backoff_s = 0.3
            self._subs = set()
            self._sync_subs()
            self._last_activity = self._clock()
            channels = ", ".join(sorted(self._channels)) or "no channels"
            self._log_link(
                "stream_reconnected" if relink else "stream_connected",
                f"SanketPTT {'reconnected' if relink else 'connected'} via {redact_broker(self.broker_url)} ({channels})",
            )
            logger.info("sanket: on_connect reason=%s broker=%s", rc, redact_broker(self.broker_url))
        except Exception:
            self._connecting = False
            self.connected = False
            logger.warning("sanket: on_connect callback failed", exc_info=True)
            return
        logger.info(
            "sanket: connected broker=%s subscriptions=%s",
            redact_broker(self.broker_url),
            ", ".join(self.subscriptions()) or "(none)",
        )
        if self.presence_enabled:
            self.beat()
            logger.info("sanket: powered on as %s id=%s", self.presence_name, self.client_id)
        self.write_status()

    def _on_disconnect(self, client, userdata, flags=None, rc=None, props=None):
        try:
            # _drop_client clears _client before disconnect, so that callback
            # must not rotate or cancel the handshake that replaces it.
            if client is not self._client:
                return
            if self._connecting and not self.connected:
                logger.warning("sanket: on_disconnect during handshake reason=%s flags=%s", rc, flags)
                return
            if self.connected:
                self.last_error = f"disconnected ({rc})"
                channels = ", ".join(sorted(self._channels)) or "no channels"
                self._log_link(
                    "stream_disconnected",
                    f"SanketPTT disconnected from {redact_broker(self.broker_url)} ({channels}): {rc}",
                    level="warning",
                )
            self._connecting = False
            self.connected = False
            self.reconnects += 1
            logger.warning("sanket: on_disconnect reason=%s flags=%s broker=%s", rc, flags, redact_broker(self.broker_url))
            if not self._closed:
                self._rotate_broker(failed_before_connect=False)
            self.write_status()
        except Exception:
            logger.warning("sanket: on_disconnect callback failed", exc_info=True)

    def _on_message(self, client, userdata, message):
        topic = getattr(message, "topic", "")
        payload = getattr(message, "payload", b"") or b""
        try:
            self._handle(topic, payload)
        except Exception:
            logger.exception("sanket message failed")

    def _rotate_broker(self, *, failed_before_connect: bool) -> None:
        if failed_before_connect:
            previous = self.broker_url
            self.broker_index = (self.broker_index + 1) % len(self.brokers)
            self.backoff_s = min(30.0, max(0.3, self.backoff_s * 2))
            logger.info("sanket: leaving broker %s; next %s in %.1fs", redact_broker(previous), redact_broker(self.broker_url), self.backoff_s)
        else:
            self.backoff_s = min(30.0, max(1.5, self.backoff_s))
            logger.info("sanket: reconnecting to broker %s in %.1fs", redact_broker(self.broker_url), self.backoff_s)
        self.reconnects += 0

    def _sync_subs(self) -> None:
        client = self._client
        if client is None or not self.connected:
            return
        want = set(self.subscriptions())
        fresh = sorted(want - self._subs)
        if fresh and hasattr(client, "subscribe"):
            client.subscribe([(topic, 0) for topic in fresh])
        self._subs = want

    def _handle(self, topic: str, payload: bytes) -> None:
        if not topic.startswith(PREFIX):
            return
        self._last_activity = self._clock()
        rest = topic[len(PREFIX) :]
        channel, _, sub = rest.partition("/")
        channel = clean_channel(channel)
        if not channel or clean_channel(channel) != channel:
            return
        now = self._clock()
        with self._lock:
            state = self._state.setdefault(channel, _ChannelState())
            state.last_packet = now
            if sub.startswith("presence/"):
                uid = sub[len("presence/") :]
                if uid and uid == self.client_id:
                    return
                if not payload:
                    state.users.pop(uid, None)
                else:
                    try:
                        message = json.loads(payload)
                    except json.JSONDecodeError:
                        return
                    ts = float(message.get("ts") or 0)
                    if ts and now * 1000 - ts > 60000:
                        return
                    state.users[uid] = {
                        "name": str(message.get("name") or "?")[:40],
                        "seen": now,
                    }
            elif sub == "floor":
                self._on_floor(state, payload, now)
            elif sub == "audio" and payload:
                self._on_audio(channel, state, payload, now)
        self.write_status()

    def _on_floor(self, state: _ChannelState, payload: bytes, now: float) -> None:
        try:
            message = json.loads(payload)
        except json.JSONDecodeError:
            return
        kind = message.get("t")
        uid = message.get("id")
        if kind in {"claim", "hb"}:
            if not state.floor or state.floor.get("id") != uid:
                self._drop_short_clip(state)
                state.floor = {
                    "id": uid,
                    "name": str(message.get("name") or "?")[:40],
                    "seen": now,
                }
            else:
                state.floor["seen"] = now
            state.last_talker = state.floor["name"]
        elif kind == "release" and state.floor and state.floor.get("id") == uid:
            self._finish_clip(state)
            state.floor = None

    def _on_audio(self, channel: str, state: _ChannelState, payload: bytes, now: float) -> None:
        speaker, pcm = decode_audio_payload(payload)
        floor = state.floor
        if not floor or floor.get("id") != speaker:
            return
        floor["seen"] = now
        state.last_talker = floor.get("name") or speaker
        state.clip_pcm.extend(pcm)
        state.clip_samples += len(pcm) // 2
        crossed = state.clip_samples >= int(MIN_CLIP_S * RATE)
        if crossed:
            chunk = bytes(state.clip_pcm)
            state.clip_pcm.clear()
            for capture in self._captures.get(channel, []):
                capture.push(chunk)
            if state.clip_samples <= int(MIN_CLIP_S * RATE) + (len(pcm) // 2):
                logger.info(
                    "sanket: audio on %s from %s (%d bytes)",
                    channel,
                    state.last_talker or speaker,
                    len(pcm),
                )
        if state.clip_samples >= int(MAX_CLIP_S * RATE):
            self._finish_clip(state)

    def _drop_short_clip(self, state: _ChannelState) -> None:
        if state.clip_samples and state.clip_samples < int(MIN_CLIP_S * RATE):
            state.clip_pcm.clear()
            state.clip_samples = 0

    def _finish_clip(self, state: _ChannelState) -> None:
        if state.clip_samples < int(MIN_CLIP_S * RATE):
            state.clip_pcm.clear()
            state.clip_samples = 0
            return
        state.clip_pcm.clear()
        state.clip_samples = 0

    def _public_row(self, state: _ChannelState, now: float) -> dict:
        online = [user["name"] for user in state.users.values() if now - user["seen"] <= PRESENCE_TTL_S]
        talking = ""
        if state.floor and now - float(state.floor.get("seen") or 0) <= FLOOR_TIMEOUT_S:
            talking = str(state.floor.get("name") or "")
        return {
            "online": len(online),
            "online_names": online,
            "talking": talking,
            "last_packet": state.last_packet,
            "last_talker": state.last_talker,
        }

    def expire(self) -> None:
        """Drop a floor that has been quiet for 2.5 s and forget stale presence."""
        now = self._clock()
        with self._lock:
            for state in self._state.values():
                stale = [uid for uid, user in state.users.items() if now - user["seen"] > PRESENCE_TTL_S]
                for uid in stale:
                    del state.users[uid]
                if state.floor and now - float(state.floor.get("seen") or 0) > FLOOR_TIMEOUT_S:
                    self._finish_clip(state)
                    state.floor = None

    def _log_link(self, kind: str, message: str, *, level: str = "info") -> None:
        try:
            from soundcore.activity import record_activity

            record_activity(kind, message, channel="sanket", level=level)
        except Exception:
            logger.exception("sanket link event was not stored")

    def _drop_client(self) -> None:
        client = self._client
        self._client = None
        self._connecting = False
        self._subs = set()
        if client is None:
            return
        try:
            client.disconnect()
        except Exception:
            pass
        if hasattr(client, "loop_stop"):
            try:
                client.loop_stop()
            except Exception:
                pass


def load_sanket_status(data_dir) -> dict:
    path = Path(data_dir) / "logs" / "sanket_status.json"
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def get_hub() -> SanketHub:
    global _hub
    with _hub_lock:
        if _hub is None:
            _hub = SanketHub()
        return _hub


def set_hub(hub: SanketHub | None) -> None:
    global _hub
    with _hub_lock:
        _hub = hub


def _split_broker(url: str) -> tuple[str, int, str, bool, str, str]:
    from urllib.parse import unquote, urlparse

    parsed = urlparse(url)
    scheme = parsed.scheme
    port = parsed.port or {"wss": 443, "ws": 80, "mqtts": 8883}.get(scheme, 1883)
    return (
        parsed.hostname or "",
        port,
        parsed.path or "/",
        scheme in {"wss", "mqtts"},
        unquote(parsed.username or ""),
        unquote(parsed.password or ""),
    )


def _paho_factory(url: str):
    import paho.mqtt.client as mqtt

    parsed_scheme = url.split(":", 1)[0]
    # VERSION2 callbacks are (client, userdata, flags, reason_code, properties).
    # reconnect_on_failure stays off so this hub, not paho, owns the retry.
    client = mqtt.Client(
        mqtt.CallbackAPIVersion.VERSION2,
        client_id=f"soundcore_{int(time.time())}",
        transport="websockets" if parsed_scheme in {"ws", "wss"} else "tcp",
        clean_session=True,
        reconnect_on_failure=False,
    )
    return client
