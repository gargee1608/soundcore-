"""One playback per USB output; mix many inputs with soft limiting."""

from __future__ import annotations

import logging
import struct
import threading
from dataclasses import dataclass

from soundcore.audio.pcm import apply_gain, pcm_rms

logger = logging.getLogger(__name__)


def mix_pcm(chunks: list[bytes]) -> bytes:
    if not chunks:
        return b""
    if len(chunks) == 1:
        return chunks[0]
    frame_bytes = len(chunks[0])
    if any(len(c) != frame_bytes for c in chunks):
        frame_bytes = min(len(c) for c in chunks)
        chunks = [c[:frame_bytes] for c in chunks]
    count = frame_bytes // 2
    if count == 0:
        return b""
    accum = [0] * count
    for chunk in chunks:
        samples = struct.unpack("<" + "h" * count, chunk[: frame_bytes])
        for i, sample in enumerate(samples):
            accum[i] += sample
    out = []
    for total in accum:
        scaled = int(total * (0.85 if len(chunks) > 2 else 1.0))
        if scaled > 32767:
            scaled = 32767
        elif scaled < -32767:
            scaled = -32767
        out.append(scaled)
    return struct.pack("<" + "h" * count, *out)


@dataclass(frozen=True)
class MixSource:
    input_channel_id: int
    gain_db: float


@dataclass
class MixOutputSpec:
    output_channel_id: int
    device_id: str
    backend: str
    sample_rate: int
    name: str
    sources: list[MixSource]


class _OutputSink:
    def __init__(self, spec: MixOutputSpec, playback):
        self.spec = spec
        self.playback = playback
        self._latest: dict[int, bytes] = {}
        self._lock = threading.Lock()
        self.rms = 0.0
        self.last_error = ""

    def push(self, input_id: int, pcm: bytes, gain_db: float) -> None:
        if not pcm:
            return
        boosted = apply_gain(pcm, gain_db) if gain_db else pcm
        with self._lock:
            self._latest[input_id] = boosted
        try:
            with self._lock:
                chunks = list(self._latest.values())
            mixed = mix_pcm(chunks)
            if mixed:
                self.playback.write(mixed)
                self.rms = pcm_rms(mixed)
        except Exception as exc:
            self.last_error = str(exc)
            logger.debug("mixer write failed for %s", spec.device_id, exc_info=True)


class OutputMixer:
    """Fan-in multiple input streams onto each physical output device."""

    def __init__(self, backend):
        self._backend = backend
        self._sinks: dict[int, _OutputSink] = {}
        self._input_map: dict[int, list[int]] = {}
        self._lock = threading.Lock()

    def reconfigure(self, outputs: list[MixOutputSpec]) -> None:
        desired_ids = {item.output_channel_id for item in outputs}
        with self._lock:
            stale = [oid for oid in self._sinks if oid not in desired_ids]
            for oid in stale:
                sink = self._sinks.pop(oid)
                try:
                    sink.playback.stop()
                except Exception:
                    logger.debug("mixer stop failed", exc_info=True)
            input_map: dict[int, list[int]] = {}
            for item in outputs:
                for source in item.sources:
                    input_map.setdefault(source.input_channel_id, []).append(item.output_channel_id)
                current = self._sinks.get(item.output_channel_id)
                signature = (
                    item.device_id,
                    item.backend,
                    item.sample_rate,
                    tuple((s.input_channel_id, s.gain_db) for s in item.sources),
                )
                if current is not None and getattr(current, "_signature", None) == signature:
                    current.spec = item
                    continue
                if current is not None:
                    try:
                        current.playback.stop()
                    except Exception:
                        pass
                playback = self._backend.open_playback(
                    item.device_id,
                    item.sample_rate,
                    backend_name=item.backend,
                )
                try:
                    playback.start()
                except Exception as exc:
                    playback.last_error = str(exc)
                    logger.exception("mixer playback start failed for %s", item.device_id)
                sink = _OutputSink(item, playback)
                sink._signature = signature  # type: ignore[attr-defined]
                self._sinks[item.output_channel_id] = sink
            self._input_map = input_map

    def feed(self, input_id: int, pcm: bytes, *, gain_db: float = 0.0) -> None:
        with self._lock:
            output_ids = list(self._input_map.get(input_id, []))
            sinks = [self._sinks[oid] for oid in output_ids if oid in self._sinks]
        for sink in sinks:
            source_gain = gain_db
            for source in sink.spec.sources:
                if source.input_channel_id == input_id:
                    source_gain = source.gain_db
                    break
            sink.push(input_id, pcm, source_gain)

    def output_levels(self) -> dict[int, float]:
        with self._lock:
            return {oid: sink.rms for oid, sink in self._sinks.items()}

    def playback_reports(self) -> list[dict]:
        reports = []
        with self._lock:
            items = list(self._sinks.items())
        for output_id, sink in items:
            if hasattr(sink.playback, "status"):
                report = sink.playback.status()
            else:
                report = {
                    "backend": sink.spec.backend,
                    "device": sink.spec.device_id,
                    "running": True,
                    "last_error": sink.last_error or getattr(sink.playback, "last_error", ""),
                }
            report["output_channel_id"] = output_id
            report["mix_inputs"] = len(sink.spec.sources)
            reports.append(report)
        return reports

    def stop(self) -> None:
        with self._lock:
            for sink in self._sinks.values():
                try:
                    sink.playback.stop()
                except Exception:
                    logger.debug("mixer stop failed", exc_info=True)
            self._sinks.clear()
            self._input_map.clear()