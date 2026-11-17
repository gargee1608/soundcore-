"""ALSA device discovery. Parsing is pure so it can be tested without a Pi."""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass

_LINE_RE = re.compile(
    r"card\s+(\d+):\s*([^,\[]*?)\s*\[([^\]]+)\],\s*device\s+(\d+):\s*([^,\[]*?)\s*\[([^\]]+)\]",
    re.IGNORECASE,
)

_JACK_HINTS = ("headphone", "bcm2835", "bcm2835 headphones", "alc", "analog", "jack", "3.5")


@dataclass(frozen=True)
class DeviceInfo:
    id: str
    name: str
    direction: str  # in | out
    kind: str  # usb | jack | virtual
    usb_id: str | None = None


class AudioBackendError(RuntimeError):
    pass


def guess_kind(card_name: str, card_index: int) -> str:
    blob = card_name.lower()
    if any(hint in blob for hint in _JACK_HINTS):
        return "jack"
    if card_index == 0 and "usb" not in blob and "hdmi" not in blob:
        return "jack"
    return "usb"


def parse_alsa_listing(text: str, *, direction: str) -> list[DeviceInfo]:
    devices: list[DeviceInfo] = []
    seen: set[str] = set()
    for raw in text.splitlines():
        match = _LINE_RE.search(raw.strip())
        if not match:
            continue
        card, short, long_name, device, _dev_short, dev_long = match.groups()
        device_id = f"plughw:{card},{device}"
        if device_id in seen:
            continue
        seen.add(device_id)
        label = long_name.strip() or short.strip() or dev_long.strip()
        kind = guess_kind(f"{label} {short}", int(card))
        if kind == "jack" and direction == "in":
            label = f"3.5 mm {label}"
        devices.append(
            DeviceInfo(
                id=device_id,
                name=label.strip(),
                direction=direction,
                kind=kind,
                usb_id=f"card{card}:{short.strip()}",
            )
        )
    return devices


def _run(command: list[str]) -> str:
    try:
        completed = subprocess.run(command, check=False, capture_output=True, text=True, timeout=5)
    except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
        raise AudioBackendError(f"Cannot run {' '.join(command)}: {exc}") from exc
    if completed.returncode not in (0, 1):
        # arecord exits 1 when there are no capture devices, and still prints the header.
        raise AudioBackendError(completed.stderr.strip() or f"{command[0]} failed")
    return completed.stdout or ""


class AlsaBackend:
    name = "alsa"

    def list_devices(self) -> list[DeviceInfo]:
        capture = parse_alsa_listing(_run(["arecord", "-l"]), direction="in")
        playback = parse_alsa_listing(_run(["aplay", "-l"]), direction="out")
        return capture + playback

    def open_capture(self, device_id: str, sample_rate: int, gain_db: float):
        from soundcore.audio.capture import AlsaCapture

        return AlsaCapture(device_id, sample_rate, gain_db)

    def open_playback(self, device_id: str, sample_rate: int):
        from soundcore.audio.capture import AlsaPlayback

        return AlsaPlayback(device_id, sample_rate)


class SimulatedBackend:
    """Fixed desk of radios, a 3.5 mm input, and two USB outputs. No hardware required."""

    name = "simulated"

    def list_devices(self) -> list[DeviceInfo]:
        return [
            DeviceInfo("sim:radio-1", "Radio 1", "in", "usb", "sim-usb-1"),
            DeviceInfo("sim:fm", "FM receiver", "in", "usb", "sim-usb-2"),
            DeviceInfo("sim:handheld", "Handheld", "in", "usb", "sim-usb-3"),
            DeviceInfo("sim:ptt", "Software PTT", "in", "virtual", "sim-virtual-ptt"),
            DeviceInfo("sim:jack", "3.5 mm line in", "in", "jack", "sim-jack"),
            DeviceInfo("sim:speaker", "USB speaker", "out", "usb", "sim-usb-out-1"),
            DeviceInfo("sim:headset", "USB headset", "out", "usb", "sim-usb-out-2"),
        ]

    def open_capture(self, device_id: str, sample_rate: int, gain_db: float):
        from soundcore.audio.capture import SimulatedCapture

        return SimulatedCapture(device_id, sample_rate, gain_db)

    def open_playback(self, device_id: str, sample_rate: int):
        from soundcore.audio.capture import SimulatedPlayback

        return SimulatedPlayback(device_id, sample_rate)


def get_backend(name: str):
    choice = (name or "auto").lower()
    if choice == "simulated":
        return SimulatedBackend()
    if choice == "alsa":
        return AlsaBackend()
    try:
        backend = AlsaBackend()
        devices = backend.list_devices()
        if any(device.direction == "in" for device in devices):
            return backend
    except AudioBackendError:
        pass
    return SimulatedBackend()
