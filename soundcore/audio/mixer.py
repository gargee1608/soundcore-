"""One mixer thread and one writer per output.

Inputs publish the latest 20 ms frame. The mixer sums those frames with
per-route gain and a soft limiter, then writes the result once. Changing
who is in the sum does not reopen a healthy writer.
"""

from __future__ import annotations

import array
import threading
import time


def _samples(pcm: bytes, count: int) -> list[int]:
    available = len(pcm) // 2
    if available <= 0:
        return [0] * count
    unpacked = array.array("h")
    unpacked.frombytes(pcm[: available * 2])
    if len(unpacked) >= count:
        return list(unpacked[:count])
    return list(unpacked) + [0] * (count - len(unpacked))


def mix_s16le(frames: list[tuple[bytes, float]], samples: int) -> tuple[bytes, float]:
    """Sum mono s16le frames. `gain_db` is applied per frame. Peak is scaled to full scale."""
    if samples <= 0:
        return b"", 0.0
    mixed = [0] * samples
    for pcm, gain_db in frames:
        factor = 10 ** (gain_db / 20.0) if abs(gain_db) >= 0.05 else 1.0
        for index, sample in enumerate(_samples(pcm, samples)):
            mixed[index] += int(sample * factor)
    peak = max((abs(sample) for sample in mixed), default=0)
    if peak > 32767:
        scale = 32767 / peak
        mixed = [int(sample * scale) for sample in mixed]
        peak = 32767
    out = array.array("h", mixed)
    return out.tobytes(), peak / 32768.0


class OutputMixer:
    def __init__(self, device_id: str, sample_rate: int, backend: str, open_playback):
        self.device_id = device_id
        self.sample_rate = sample_rate
        self.backend = backend
        self._open_playback = open_playback
        self.members: dict[int, float] = {}
        self._latest: dict[int, bytes] = {}
        self._lock = threading.Lock()
        self._playback = None
        self.opened_hw = ""
        self.level = 0.0
        self.last_error = ""
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.frames = max(1, int(sample_rate * 0.02))

    def set_members(self, members: dict[int, float]) -> None:
        with self._lock:
            self.members = dict(members)
            for key in list(self._latest):
                if key not in self.members:
                    self._latest.pop(key, None)

    def push(self, input_id: int, pcm: bytes) -> None:
        with self._lock:
            if input_id not in self.members:
                return
            self._latest[input_id] = pcm

    def ensure_writer(self, hw: str) -> None:
        """Reopen only when the device node changed or the writer died."""
        alive = _writer_alive(self._playback)
        if hw and alive and self.opened_hw == hw:
            return
        self._close_writer()
        if not hw:
            self.last_error = "not connected"
            return
        try:
            playback = self._open_playback(self.device_id, self.sample_rate, self.backend)
            playback.opened_hw = getattr(playback, "opened_hw", "") or hw
            playback.start()
        except Exception as exc:
            self._playback = None
            self.opened_hw = ""
            self.last_error = str(exc)
            return
        self._playback = playback
        self.opened_hw = hw
        self.last_error = ""

    def _close_writer(self) -> None:
        playback = self._playback
        self._playback = None
        self.opened_hw = ""
        if playback is None:
            return
        try:
            playback.stop()
        except Exception:
            pass

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name=f"mix-{self.device_id}", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=1)
        self._close_writer()

    def _run(self) -> None:
        next_tick = time.monotonic()
        while not self._stop.is_set():
            with self._lock:
                members = dict(self.members)
                latest = dict(self._latest)
            frames = [(latest.get(input_id, b""), gain) for input_id, gain in members.items()]
            pcm, level = mix_s16le(frames, self.frames) if members else (b"", 0.0)
            self.level = level
            playback = self._playback
            if playback is not None and pcm:
                try:
                    playback.write(pcm)
                except Exception as exc:
                    self.last_error = str(exc)
            next_tick += 0.02
            delay = next_tick - time.monotonic()
            if delay > 0:
                self._stop.wait(delay)
            else:
                next_tick = time.monotonic()


def _writer_alive(playback) -> bool:
    if playback is None:
        return False
    proc = getattr(playback, "proc", None)
    if proc is not None and proc.poll() is not None:
        return False
    if getattr(playback, "last_error", "") and proc is None:
        return False
    return True


class MixerBank:
    """One OutputMixer per output device."""

    def __init__(self, open_playback, resolve):
        self._open_playback = open_playback
        self._resolve = resolve
        self._lock = threading.Lock()
        self._mixers: dict[str, OutputMixer] = {}

    def apply(self, routes_by_input: dict, resolve=None) -> None:
        grouped: dict[str, dict] = {}
        for targets in routes_by_input.values():
            for target in targets:
                slot = grouped.setdefault(
                    target.device_id,
                    {
                        "rate": target.sample_rate or 48000,
                        "backend": target.backend,
                        "members": {},
                    },
                )
                slot["members"][target.input_channel_id] = float(getattr(target, "gain_db", 0.0) or 0.0)
                if target.sample_rate:
                    slot["rate"] = target.sample_rate
        resolve = resolve or self._resolve
        with self._lock:
            for device_id in list(self._mixers):
                if device_id not in grouped:
                    self._mixers.pop(device_id).stop()
            for device_id, slot in grouped.items():
                mixer = self._mixers.get(device_id)
                if mixer is None or mixer.sample_rate != slot["rate"]:
                    if mixer is not None:
                        mixer.stop()
                    mixer = OutputMixer(device_id, slot["rate"], slot["backend"], self._open_playback)
                    mixer.start()
                    self._mixers[device_id] = mixer
                mixer.set_members(slot["members"])
                hw = resolve(device_id) if resolve else device_id
                if device_id.startswith("alsa:") and not hw:
                    hw = ""
                elif not hw:
                    hw = device_id
                mixer.ensure_writer(hw)

    def push(self, input_id: int, pcm: bytes) -> None:
        with self._lock:
            mixers = list(self._mixers.values())
        for mixer in mixers:
            mixer.push(input_id, pcm)

    def stop(self) -> None:
        with self._lock:
            mixers = list(self._mixers.values())
            self._mixers.clear()
        for mixer in mixers:
            mixer.stop()

    def levels(self) -> dict[str, float]:
        with self._lock:
            return {device_id: mixer.level for device_id, mixer in self._mixers.items()}
