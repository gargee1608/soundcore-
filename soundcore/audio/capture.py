"""Capture and playback. Tests and non-Pi machines use the simulated devices."""

from __future__ import annotations

import math
import subprocess
import threading

from soundcore.audio.pcm import apply_gain, pcm_rms, tone

_TONES = {
    "sim:radio-1": 440.0,
    "sim:fm": 554.0,
    "sim:handheld": 330.0,
    "sim:ptt": 660.0,
    "sim:jack": 494.0,
}


class SimulatedCapture:
    def __init__(self, device_id: str, sample_rate: int, gain_db: float = 0.0):
        self.device_id = device_id
        self.sample_rate = sample_rate
        self.gain_db = gain_db
        self.freq = _TONES.get(device_id, 400.0)
        self._phase = 0.0
        self._tick = 0
        self.started = False

    def start(self) -> None:
        self.started = True

    def read(self, frames: int) -> bytes:
        # A slow amplitude wobble so the meter moves instead of sitting still.
        self._tick += frames
        wobble = 0.55 + 0.45 * math.sin(self._tick / self.sample_rate * 1.7)
        pcm, self._phase = tone(
            self.sample_rate,
            frames,
            self.freq,
            phase=self._phase,
            amplitude=0.22 * wobble,
        )
        return apply_gain(pcm, self.gain_db)

    def stop(self) -> None:
        self.started = False

    @property
    def rms_hint(self) -> float:
        return 0.2


class ScriptedCapture:
    """Yields a fixed buffer once, then silence. Used by tests."""

    def __init__(self, pcm: bytes, sample_rate: int = 16000):
        self._pcm = pcm
        self.sample_rate = sample_rate
        self._sent = False
        self.started = False

    def start(self) -> None:
        self.started = True

    def read(self, frames: int) -> bytes:
        if not self._sent:
            self._sent = True
            return self._pcm
        return b"\x00\x00" * frames

    def stop(self) -> None:
        self.started = False


class SimulatedPlayback:
    def __init__(self, device_id: str, sample_rate: int):
        self.device_id = device_id
        self.sample_rate = sample_rate
        self.received = bytearray()
        self.rms = 0.0
        self._lock = threading.Lock()

    def start(self) -> None:
        return None

    def write(self, pcm: bytes) -> None:
        with self._lock:
            self.received.extend(pcm)
            self.rms = pcm_rms(pcm)

    def stop(self) -> None:
        return None


class _FfmpegPipe:
    def __init__(self, command: list[str]):
        self.command = command
        self.proc: subprocess.Popen | None = None

    def start(self) -> None:
        self.proc = subprocess.Popen(
            self.command,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )

    def stop(self) -> None:
        proc = self.proc
        self.proc = None
        if proc is None:
            return
        if proc.stdin:
            try:
                proc.stdin.close()
            except OSError:
                pass
        if proc.stdout:
            try:
                proc.stdout.close()
            except OSError:
                pass
        proc.terminate()
        try:
            proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            proc.kill()


class AlsaCapture(_FfmpegPipe):
    def __init__(self, device_id: str, sample_rate: int, gain_db: float = 0.0):
        self.device_id = device_id
        self.sample_rate = sample_rate
        self.gain_db = gain_db
        super().__init__(
            [
                "ffmpeg",
                "-hide_banner",
                "-loglevel",
                "error",
                "-f",
                "alsa",
                "-i",
                device_id,
                "-ac",
                "1",
                "-ar",
                str(sample_rate),
                "-f",
                "s16le",
                "-acodec",
                "pcm_s16le",
                "pipe:1",
            ]
        )

    def read(self, frames: int) -> bytes:
        if self.proc is None or self.proc.stdout is None:
            return b""
        needed = frames * 2
        chunks = []
        got = 0
        while got < needed:
            piece = self.proc.stdout.read(needed - got)
            if not piece:
                break
            chunks.append(piece)
            got += len(piece)
        return apply_gain(b"".join(chunks), self.gain_db)


class AlsaPlayback(_FfmpegPipe):
    def __init__(self, device_id: str, sample_rate: int):
        self.device_id = device_id
        self.sample_rate = sample_rate
        super().__init__(
            [
                "ffmpeg",
                "-hide_banner",
                "-loglevel",
                "error",
                "-f",
                "s16le",
                "-ar",
                str(sample_rate),
                "-ac",
                "1",
                "-i",
                "pipe:0",
                "-f",
                "alsa",
                device_id,
            ]
        )

    def write(self, pcm: bytes) -> None:
        if self.proc is None or self.proc.stdin is None:
            return
        try:
            self.proc.stdin.write(pcm)
            self.proc.stdin.flush()
        except (BrokenPipeError, OSError):
            self.stop()
