"""16-bit little-endian mono helpers."""

from __future__ import annotations

import math
import struct


def pcm_rms(pcm: bytes) -> float:
    sample_count = len(pcm) // 2
    if sample_count == 0:
        return 0.0
    samples = struct.unpack("<" + "h" * sample_count, pcm[: sample_count * 2])
    total = 0.0
    for sample in samples:
        total += sample * sample
    return math.sqrt(total / sample_count) / 32768.0


def apply_gain(pcm: bytes, gain_db: float) -> bytes:
    if not pcm or abs(gain_db) < 0.05:
        return pcm
    factor = 10 ** (gain_db / 20.0)
    sample_count = len(pcm) // 2
    samples = struct.unpack("<" + "h" * sample_count, pcm[: sample_count * 2])
    scaled = []
    for sample in samples:
        value = int(sample * factor)
        if value > 32767:
            value = 32767
        elif value < -32767:
            value = -32767
        scaled.append(value)
    return struct.pack("<" + "h" * len(scaled), *scaled)


def tone(sample_rate: int, frames: int, freq: float, *, phase: float, amplitude: float = 0.35) -> tuple[bytes, float]:
    """Return PCM and the advanced phase, in radians."""
    step = 2 * math.pi * freq / sample_rate
    values = []
    for _ in range(frames):
        sample = int(max(-32767, min(32767, math.sin(phase) * amplitude * 32767)))
        values.append(sample)
        phase += step
    return struct.pack("<" + "h" * frames, *values), phase
