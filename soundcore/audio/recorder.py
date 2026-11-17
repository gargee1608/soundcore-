"""WAV chunk writer. One open file per channel; rotation finalizes the database row."""

from __future__ import annotations

import wave
from pathlib import Path

from soundcore.storage.s3 import file_sha256


class WavRecorder:
    def __init__(self, path: Path, sample_rate: int):
        self.path = Path(path)
        self.sample_rate = sample_rate
        self.frames = 0
        self._wav: wave.Wave_write | None = None

    def open(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle = wave.open(str(self.path), "wb")
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(self.sample_rate)
        self._wav = handle

    def write(self, pcm: bytes) -> None:
        if not pcm or self._wav is None:
            return
        self._wav.writeframes(pcm)
        self.frames += len(pcm) // 2

    def close(self) -> dict:
        if self._wav is not None:
            self._wav.close()
            self._wav = None
        size = self.path.stat().st_size if self.path.is_file() else 0
        duration = self.frames / self.sample_rate if self.sample_rate else 0
        checksum = file_sha256(self.path) if size else ""
        return {"size_bytes": size, "duration_s": duration, "checksum": checksum, "frames": self.frames}


def fanout(pcm: bytes, writer: WavRecorder | None, playbacks: list, bus, channel_id: int, sample_rate: int) -> None:
    """Send one buffer to the file, the routed outputs, and live listen together."""
    if writer is not None:
        writer.write(pcm)
    for playback in playbacks:
        playback.write(pcm)
    if bus is not None:
        bus.publish(channel_id, sample_rate, pcm)
