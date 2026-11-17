"""Capture and playback. Tests and non-Pi machines use the simulated devices."""

from __future__ import annotations

import math
import os
import shutil
import socket
import subprocess
import threading
import time

from soundcore.audio.detect import AudioBackendError
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


def playback_command(sink: str | None, sample_rate: int, which=None) -> list[str]:
    """paplay is the reliable Pulse client on Raspberry Pi OS. ffmpeg is the fallback."""
    which = which or shutil.which
    if which("paplay"):
        command = [
            "paplay",
            "--raw",
            "--format=s16le",
            f"--rate={sample_rate}",
            "--channels=1",
            "--latency-msec=200",
        ]
        if sink:
            command.append(f"--device={sink}")
        return command
    if which("ffmpeg"):
        command = [
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
            "pulse",
            sink or "default",
        ]
        return command
    raise AudioBackendError("Neither paplay nor ffmpeg is available for PipeWire playback")


def capture_command(source: str | None, sample_rate: int, which=None) -> list[str]:
    which = which or shutil.which
    if which("parec"):
        command = [
            "parec",
            "--raw",
            "--format=s16le",
            f"--rate={sample_rate}",
            "--channels=1",
        ]
        if source:
            command.append(f"--device={source}")
        return command
    if which("ffmpeg"):
        return [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "pulse",
            "-i",
            source or "default",
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
    raise AudioBackendError("Neither parec nor ffmpeg is available for PipeWire capture")


class _StatusMixin:
    def status(self) -> dict:
        running = bool(self.proc is not None and self.proc.poll() is None) if hasattr(self, "proc") else False
        return {
            "backend": "pipewire",
            "device": getattr(self, "sink", None) or getattr(self, "source", None) or "default",
            "running": running or bool(getattr(self, "_sock", None)),
            "last_error": getattr(self, "last_error", ""),
        }


class PipeWirePlayback(_StatusMixin):
    """Play s16le mono PCM to a Pulse sink. A dead process is restarted on the next write.

    The pending buffer is capped at about one second so a missing Bluetooth
    sink cannot grow memory while the capture thread keeps recording.
    """

    def __init__(self, sink: str | None, sample_rate: int, *, popen=None, which=None, retry_interval: float = 1.0):
        self.sink = sink
        self.sample_rate = sample_rate
        self.last_error = ""
        self.proc: subprocess.Popen | None = None
        self._popen = popen or subprocess.Popen
        self._which = which or shutil.which
        self._retry_interval = retry_interval
        self._next_retry = 0.0
        self._pending = bytearray()
        self._max_pending = max(sample_rate * 2, 2)
        self._lock = threading.Lock()
        self._stderr = ""
        self._sock = None

    def start(self) -> None:
        with self._lock:
            self._spawn()

    def write(self, pcm: bytes) -> None:
        if not pcm:
            return
        with self._lock:
            if not self._healthy():
                self._remember_error()
                self._spawn()
            if not self._healthy():
                self._pending.extend(pcm)
                if len(self._pending) > self._max_pending:
                    del self._pending[: len(self._pending) - self._max_pending]
                return
            payload = bytes(self._pending) + pcm
            self._pending.clear()
            try:
                if self._sock is not None:
                    self._sock.sendall(payload)
                elif self.proc is not None and self.proc.stdin is not None:
                    self.proc.stdin.write(payload)
                    self.proc.stdin.flush()
                self.last_error = ""
            except (BrokenPipeError, OSError) as exc:
                self.last_error = str(exc) or "playback process exited"
                self._stop_locked()

    def stop(self) -> None:
        with self._lock:
            self._stop_locked()

    def _healthy(self) -> bool:
        if self._sock is not None:
            return True
        return self.proc is not None and self.proc.poll() is None

    def _spawn(self) -> None:
        if self._healthy():
            return
        now = time.monotonic()
        if now < self._next_retry:
            return
        self._next_retry = now + self._retry_interval
        self._stop_locked()
        if os.environ.get("SOUNDCORE_AUDIO_BRIDGE") == "1":
            self._spawn_bridge()
            return
        try:
            command = playback_command(self.sink, self.sample_rate, self._which)
        except AudioBackendError as exc:
            self.last_error = str(exc)
            return
        try:
            self.proc = self._popen(
                command,
                stdin=subprocess.PIPE,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
            )
        except OSError as exc:
            self.last_error = str(exc)
            self.proc = None
            return
        if self.proc.poll() is not None:
            self._remember_error()
            self.proc = None

    def _spawn_bridge(self) -> None:
        port = int(os.environ.get("SOUNDCORE_AUDIO_BRIDGE_PORT") or "8743")
        try:
            sock = socket.create_connection(("127.0.0.1", port), timeout=1.5)
            header = f"RATE {self.sample_rate} SINK {self.sink or 'default'}\n".encode()
            sock.sendall(header)
            self._sock = sock
            self.last_error = ""
        except OSError as exc:
            self.last_error = f"audio bridge unavailable: {exc}"
            self._sock = None

    def _remember_error(self) -> None:
        text = ""
        proc = self.proc
        if proc is not None and proc.stderr is not None:
            try:
                chunk = proc.stderr.read()
                if isinstance(chunk, bytes):
                    text = chunk.decode("utf-8", errors="replace")
            except Exception:
                text = ""
        self._stderr = (text or self._stderr)[-500:]
        if self._stderr.strip():
            self.last_error = self._stderr.strip()
        elif not self.last_error:
            self.last_error = "playback process exited"

    def _stop_locked(self) -> None:
        sock = self._sock
        self._sock = None
        if sock is not None:
            try:
                sock.close()
            except OSError:
                pass
        proc = self.proc
        self.proc = None
        if proc is None:
            return
        if proc.stdin:
            try:
                proc.stdin.close()
            except OSError:
                pass
        try:
            proc.terminate()
            proc.wait(timeout=1)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass


class PipeWireCapture(_FfmpegPipe):
    def __init__(self, source: str | None, sample_rate: int, gain_db: float = 0.0, *, which=None):
        self.source = source
        self.sample_rate = sample_rate
        self.gain_db = gain_db
        self.last_error = ""
        super().__init__(capture_command(source, sample_rate, which))

    def read(self, frames: int) -> bytes:
        if self.proc is None or self.proc.stdout is None:
            return b""
        if self.proc.poll() is not None:
            self.last_error = "PipeWire capture process exited"
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
