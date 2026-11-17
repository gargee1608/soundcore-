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
    kind: str  # usb | jack | hdmi | bluetooth | virtual
    usb_id: str | None = None
    backend: str = "alsa"  # alsa | pipewire | simulated
    available: bool = True


class AudioBackendError(RuntimeError):
    pass


def guess_kind(card_name: str, card_index: int) -> str:
    blob = card_name.lower()
    if "hdmi" in blob or "vc4" in blob:
        return "hdmi"
    if any(hint in blob for hint in _JACK_HINTS):
        return "jack"
    if card_index == 0 and "usb" not in blob and "hdmi" not in blob:
        return "jack"
    return "usb"


def is_bluetooth_name(name: str) -> bool:
    blob = (name or "").lower()
    return "bluetooth" in blob or "bluez" in blob


def is_hdmi_name(name: str) -> bool:
    blob = (name or "").lower()
    return "hdmi" in blob or "vc4" in blob


def infer_backend(device_id: str) -> str:
    device_id = device_id or ""
    if device_id.startswith("pipewire:"):
        return "pipewire"
    if device_id.startswith("sim:"):
        return "simulated"
    if device_id.startswith("sanketptt:"):
        return "sanketptt"
    return "alsa"


def pipewire_target(device_id: str) -> str | None:
    """Pulse sink or source name. None means the default sink or source."""
    if not device_id.startswith("pipewire:"):
        return device_id or None
    target = device_id.split(":", 1)[1]
    if target in {"", "default"}:
        return None
    return target


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
        blob = f"{label} {short} {long_name}"
        # Bluetooth is a PipeWire sink, not an ALSA card. HDMI is output only.
        # Cards that are only mass storage or HID never appear in arecord/aplay,
        # and this parser does not read lsusb.
        if is_bluetooth_name(blob):
            continue
        if direction == "in" and is_hdmi_name(blob):
            continue
        kind = guess_kind(blob, int(card))
        if kind == "jack" and direction == "in":
            label = f"3.5 mm {label}"
        devices.append(
            DeviceInfo(
                id=device_id,
                name=label.strip(),
                direction=direction,
                kind=kind,
                usb_id=f"card{card}:{short.strip()}",
                backend="alsa",
                available=True,
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

    def open_capture(self, device_id: str, sample_rate: int, gain_db: float, backend_name: str | None = None):
        from soundcore.audio.capture import AlsaCapture

        return AlsaCapture(device_id, sample_rate, gain_db)

    def open_playback(self, device_id: str, sample_rate: int, backend_name: str | None = None):
        from soundcore.audio.capture import AlsaPlayback

        return AlsaPlayback(device_id, sample_rate)


class SimulatedBackend:
    """Fixed desk of radios, a 3.5 mm input, and two USB outputs. No hardware required."""

    name = "simulated"

    def list_devices(self) -> list[DeviceInfo]:
        return [
            DeviceInfo("sim:radio-1", "Radio 1", "in", "usb", "sim-usb-1", "simulated"),
            DeviceInfo("sim:fm", "FM receiver", "in", "usb", "sim-usb-2", "simulated"),
            DeviceInfo("sim:handheld", "Handheld", "in", "usb", "sim-usb-3", "simulated"),
            DeviceInfo("sim:ptt", "Software PTT", "in", "virtual", "sim-virtual-ptt", "simulated"),
            DeviceInfo("sim:jack", "3.5 mm line in", "in", "jack", "sim-jack", "simulated"),
            DeviceInfo("sim:speaker", "USB speaker", "out", "usb", "sim-usb-out-1", "simulated"),
            DeviceInfo("sim:headset", "USB headset", "out", "usb", "sim-usb-out-2", "simulated"),
        ]

    def open_capture(self, device_id: str, sample_rate: int, gain_db: float, backend_name: str | None = None):
        from soundcore.audio.capture import SimulatedCapture

        return SimulatedCapture(device_id, sample_rate, gain_db)

    def open_playback(self, device_id: str, sample_rate: int, backend_name: str | None = None):
        from soundcore.audio.capture import SimulatedPlayback

        return SimulatedPlayback(device_id, sample_rate)


def _normalize_token(value: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (value or "").lower())


def parse_pactl_short(text: str, *, direction: str) -> list[DeviceInfo]:
    """Parse `pactl list short sinks` or `sources`.

    Monitor sources are not capture devices. Bluetooth sinks stay on the
    pipewire backend even when the node name starts with bluez_.
    """
    devices: list[DeviceInfo] = []
    seen: set[str] = set()
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.lower().startswith("sink") or line.lower().startswith("source"):
            continue
        parts = line.split("\t")
        if len(parts) < 2:
            parts = line.split()
        if len(parts) < 2 or not parts[0].isdigit():
            continue
        name = parts[1]
        state = parts[-1] if len(parts) >= 5 else ""
        if direction == "in" and ".monitor" in name:
            continue
        device_id = f"pipewire:{name}"
        if device_id in seen:
            continue
        seen.add(device_id)
        kind = "bluetooth" if name.startswith("bluez_") or is_bluetooth_name(name) else "usb"
        available = state.upper() not in {"UNLINKED", "INVALID", "GONE"}
        devices.append(
            DeviceInfo(
                id=device_id,
                name=name,
                direction=direction,
                kind=kind,
                usb_id=None,
                backend="pipewire",
                available=available,
            )
        )
    return devices


def parse_wpctl_status(text: str) -> dict[str, list[dict]]:
    """Pull display names from `wpctl status`.

    Device rows carry the [bluez] tag. Sink and source rows usually only
    carry a volume tag, so Bluetooth naming uses the device section.
    """
    section = None
    parsed = {"devices": [], "sinks": [], "sources": []}
    node_re = re.compile(r"(?:\*\s+)?(\d+)\.\s+(.+?)(?:\s+\[([^\]]+)\])?\s*$")
    for raw in text.splitlines():
        line = raw.strip()
        if "Devices:" in line:
            section = "devices"
            continue
        if "Sinks:" in line:
            section = "sinks"
            continue
        if "Sources:" in line:
            section = "sources"
            continue
        if any(token in line for token in ("Filters:", "Streams:", "Video", "Sink endpoints", "Source endpoints")):
            section = None
            continue
        if section is None:
            continue
        match = node_re.search(line)
        if not match:
            continue
        tag = (match.group(3) or "").lower()
        parsed[section].append({"name": match.group(2).strip(), "tag": tag})
    return parsed


def _label_kind(label: dict) -> str:
    tag = label.get("tag") or ""
    name = label.get("name") or ""
    if "bluez" in tag or is_bluetooth_name(name):
        return "bluetooth"
    if is_hdmi_name(name) or "hdmi" in tag:
        return "hdmi"
    return "usb"


def apply_wpctl_labels(devices: list[DeviceInfo], wpctl_text: str) -> list[DeviceInfo]:
    if not wpctl_text.strip():
        return devices
    parsed = parse_wpctl_status(wpctl_text)
    bluez_names = [row["name"] for row in parsed["devices"] if _label_kind(row) == "bluetooth"]
    labeled: list[DeviceInfo] = []
    for device in devices:
        pool = parsed["sinks"] if device.direction == "out" else parsed["sources"]
        name = device.name
        kind = device.kind
        if device.kind == "bluetooth" and bluez_names:
            name = bluez_names.pop(0)
            kind = "bluetooth"
        else:
            pactl_name = device.id.split(":", 1)[-1]
            for row in pool:
                if _names_match(pactl_name, row["name"]):
                    name = row["name"]
                    if _label_kind(row) == "bluetooth":
                        kind = "bluetooth"
                    break
        labeled.append(
            DeviceInfo(
                id=device.id,
                name=name,
                direction=device.direction,
                kind=kind,
                usb_id=device.usb_id,
                backend=device.backend,
                available=device.available,
            )
        )
    return labeled


def _names_match(pactl_name: str, label: str) -> bool:
    left = _normalize_token(pactl_name)
    right = _normalize_token(label)
    if not left or not right:
        return False
    return left in right or right in left


class PipeWireBackend:
    """Sinks and sources from pactl, with wpctl display names when it is installed."""

    name = "pipewire"

    def __init__(self, runner=None):
        self._runner = runner or _run
        self.last_error = ""

    def list_devices(self) -> list[DeviceInfo]:
        try:
            sinks = self._runner(["pactl", "list", "short", "sinks"])
            sources = self._runner(["pactl", "list", "short", "sources"])
        except AudioBackendError as exc:
            self.last_error = str(exc)
            return []
        self.last_error = ""
        devices = parse_pactl_short(sinks, direction="out") + parse_pactl_short(sources, direction="in")
        try:
            wpctl = self._runner(["wpctl", "status"])
        except AudioBackendError:
            wpctl = ""
        return apply_wpctl_labels(devices, wpctl)

    def open_capture(self, device_id: str, sample_rate: int, gain_db: float, backend_name: str | None = None):
        from soundcore.audio.capture import PipeWireCapture

        return PipeWireCapture(pipewire_target(device_id), sample_rate, gain_db)

    def open_playback(self, device_id: str, sample_rate: int, backend_name: str | None = None):
        from soundcore.audio.capture import PipeWirePlayback

        return PipeWirePlayback(pipewire_target(device_id), sample_rate)


class HybridBackend:
    """ALSA for USB and jack capture/playback, PipeWire for Pulse sinks such as Bluetooth."""

    name = "hybrid"

    def __init__(self, alsa=None, pipewire=None):
        self.alsa = alsa or AlsaBackend()
        self.pipewire = pipewire or PipeWireBackend()
        self.last_error = ""

    def list_devices(self) -> list[DeviceInfo]:
        devices: list[DeviceInfo] = []
        try:
            devices.extend(self.alsa.list_devices())
        except AudioBackendError as exc:
            self.last_error = str(exc)
        try:
            devices.extend(self.pipewire.list_devices())
            if self.pipewire.last_error and not self.last_error:
                self.last_error = self.pipewire.last_error
        except AudioBackendError as exc:
            self.last_error = str(exc)
        return devices

    def rescan(self) -> None:
        self.last_error = ""
        if hasattr(self.pipewire, "last_error"):
            self.pipewire.last_error = ""

    def open_capture(self, device_id: str, sample_rate: int, gain_db: float, backend_name: str | None = None):
        kind = backend_name or infer_backend(device_id)
        if kind == "pipewire":
            return self.pipewire.open_capture(device_id, sample_rate, gain_db)
        if kind == "sanketptt":
            from soundcore.audio.sanketptt import SanketCapture, channel_from_device, get_hub

            return SanketCapture(channel_from_device(device_id), sample_rate, get_hub())
        if kind == "simulated":
            return SimulatedBackend().open_capture(device_id, sample_rate, gain_db)
        return self.alsa.open_capture(device_id, sample_rate, gain_db)

    def open_playback(self, device_id: str, sample_rate: int, backend_name: str | None = None):
        kind = backend_name or infer_backend(device_id)
        if kind == "pipewire":
            return self.pipewire.open_playback(device_id, sample_rate)
        if kind == "simulated":
            return SimulatedBackend().open_playback(device_id, sample_rate)
        return self.alsa.open_playback(device_id, sample_rate)


def get_backend(name: str):
    choice = (name or "auto").lower()
    if choice == "simulated":
        return SimulatedBackend()
    if choice == "alsa":
        return AlsaBackend()
    if choice == "pipewire":
        return PipeWireBackend()
    if choice == "hybrid":
        return HybridBackend()
    hybrid = HybridBackend()
    try:
        capture = hybrid.alsa.list_devices()
    except AudioBackendError:
        capture = []
    if any(device.direction == "in" for device in capture):
        return hybrid
    return SimulatedBackend()
