"""In-process fan-out plus a localhost UDP hop for the split web/worker processes."""

from __future__ import annotations

import logging
import socket
import struct
import threading

logger = logging.getLogger(__name__)

_MAGIC = b"SC1"
_HEADER = struct.Struct("<3sIII")


def pack_frame(channel_id: int, sample_rate: int, pcm: bytes) -> bytes:
    return _HEADER.pack(_MAGIC, channel_id, sample_rate, len(pcm)) + pcm


def unpack_frame(packet: bytes):
    if len(packet) < _HEADER.size:
        return None
    magic, channel_id, sample_rate, length = _HEADER.unpack_from(packet)
    if magic != _MAGIC:
        return None
    pcm = packet[_HEADER.size : _HEADER.size + length]
    if len(pcm) != length:
        return None
    return channel_id, sample_rate, pcm


class InProcessBus:
    def __init__(self):
        self._lock = threading.Lock()
        self._subs: dict[int, dict[str, callable]] = {}

    def publish(self, channel_id: int, sample_rate: int, pcm: bytes) -> None:
        with self._lock:
            callbacks = list(self._subs.get(channel_id, {}).values())
        for callback in callbacks:
            try:
                callback(sample_rate, pcm)
            except Exception:
                logger.exception("live listener failed")

    def subscribe(self, channel_id: int, listener_id: str, callback) -> None:
        with self._lock:
            self._subs.setdefault(channel_id, {})[listener_id] = callback

    def unsubscribe(self, listener_id: str) -> None:
        with self._lock:
            for channel_subs in self._subs.values():
                channel_subs.pop(listener_id, None)


class UdpPublisher:
    """Worker side. subscribe is a no-op; the web process owns listeners."""

    def __init__(self, host: str, port: int):
        self.addr = (host, port)
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)

    def publish(self, channel_id: int, sample_rate: int, pcm: bytes) -> None:
        packet = pack_frame(channel_id, sample_rate, pcm)
        try:
            self._sock.sendto(packet, self.addr)
        except OSError:
            logger.debug("live udp send failed", exc_info=True)

    def subscribe(self, channel_id: int, listener_id: str, callback) -> None:
        return None

    def unsubscribe(self, listener_id: str) -> None:
        return None

    def close(self) -> None:
        self._sock.close()


class UdpSubscriber:
    """Web side. Datagrams are republished on an in-process bus."""

    def __init__(self, bus: InProcessBus, host: str, port: int):
        self.bus = bus
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock.bind((host, port))
        self._sock.settimeout(0.5)
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._loop, name="soundcore-live", daemon=True)

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._thread.join(timeout=2)
        self._sock.close()

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                packet, _addr = self._sock.recvfrom(65535)
            except socket.timeout:
                continue
            except OSError:
                break
            frame = unpack_frame(packet)
            if frame is None:
                continue
            channel_id, sample_rate, pcm = frame
            self.bus.publish(channel_id, sample_rate, pcm)
