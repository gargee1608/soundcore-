"""PipeWire playback beside ALSA capture. Subprocesses are fakes; no Pi hardware."""

import io
from pathlib import Path
from types import SimpleNamespace

from soundcore.audio.capture import (
    AlsaCapture,
    AlsaPlayback,
    PipeWirePlayback,
    ScriptedCapture,
)
from soundcore.audio.detect import (
    AlsaBackend,
    AudioBackendError,
    DeviceInfo,
    HybridBackend,
    PipeWireBackend,
    SimulatedBackend,
    get_backend,
    parse_alsa_listing,
    parse_pactl_short,
)
from soundcore.audio.recorder import fanout
from soundcore.audio.service import plan_routes
from soundcore.db import create_all, init_engine, migrate_schema
from soundcore.ops import channel_endpoint

HDMI_AND_USB = """
card 0: vc4hdmi0 [vc4-hdmi-0], device 0: MAI PCM i2s-hifi-0 [MAI PCM i2s-hifi-0]
card 1: vc4hdmi1 [vc4-hdmi-1], device 0: MAI PCM i2s-hifi-0 [MAI PCM i2s-hifi-0]
card 2: Device [USB Audio], device 0: USB Audio [USB Audio]
card 3: Bluetooth [Bluetooth Audio], device 0: Bluetooth [Bluetooth Audio]
"""

LSUSB_NOISE = """
Bus 001 Device 004: ID 1234:5678 Mass Storage
Bus 001 Device 005: ID abcd:0001 HID Keyboard
"""

SINKS = (
    "52\tbluez_output.AA_BB_CC_DD_EE_FF.1\tPipeWire\ts16le 2ch 48000Hz\tRUNNING\n"
    "53\talsa_output.usb-C-Media_USB_Audio_Device-00.analog-stereo\tPipeWire\ts16le 2ch 48000Hz\tRUNNING\n"
)
SOURCES = (
    "10\talsa_input.usb-C-Media_USB_Audio_Device-00.analog-stereo\tPipeWire\ts16le 2ch 48000Hz\tRUNNING\n"
    "11\tbluez_output.AA_BB_CC_DD_EE_FF.1.monitor\tPipeWire\ts16le 2ch 48000Hz\tRUNNING\n"
    "12\talsa_output.usb-C-Media_USB_Audio_Device-00.analog-stereo.monitor\tPipeWire\ts16le 2ch 48000Hz\tIDLE\n"
)
WPCTL = """
Audio
 ├─ Devices:
 │      45. pTron BT SPEAKER                 [bluez]
 │      46. USB Audio Device                 [alsa]
 ├─ Sinks:
 │  *   52. pTron BT SPEAKER                 [vol: 0.40]
 │      53. USB Audio Device                 [vol: 1.00]
 ├─ Sources:
 │      10. USB Audio Device                 [vol: 1.00]
 └─ Streams:
"""


def _which_paplay(name):
    return "/usr/bin/paplay" if name == "paplay" else None


class FakeStdin:
    def __init__(self):
        self.buf = bytearray()

    def write(self, data):
        self.buf.extend(data)
        return len(data)

    def flush(self):
        return None

    def close(self):
        return None


class FakeProc:
    def __init__(self, alive=True, stderr=b""):
        self.stdin = FakeStdin()
        self.stderr = io.BytesIO(stderr)
        self.stdout = None
        self.alive = alive
        self.killed = False

    def poll(self):
        if self.alive and not self.killed:
            return None
        return 1

    def terminate(self):
        self.killed = True

    def kill(self):
        self.killed = True

    def wait(self, timeout=None):
        self.killed = True
        return 1


def test_alsa_discovery_skips_hdmi_inputs_bluetooth_and_lsusb():
    capture = parse_alsa_listing(HDMI_AND_USB + LSUSB_NOISE, direction="in")
    playback = parse_alsa_listing(HDMI_AND_USB, direction="out")
    assert [device.id for device in capture] == ["alsa:usb-cardid:Device:pcm0"]
    assert capture[0].backend == "alsa"
    assert capture[0].direction == "in"
    assert capture[0].hw == "plughw:2,0"
    assert [device.id for device in playback] == [
        "plughw:0,0",
        "plughw:1,0",
        "alsa:usb-cardid:Device:pcm0",
    ]
    assert all(device.kind == "hdmi" for device in playback[:2])
    assert all(not device.id.startswith("blue") for device in capture + playback)
    source = Path("soundcore/audio/detect.py").read_text(encoding="utf-8")
    assert '"lsusb"' not in source and "'lsusb'" not in source


def test_pipewire_sink_and_source_discovery():
    def runner(command):
        if command[:4] == ["pactl", "list", "short", "sinks"]:
            return SINKS
        if command[:4] == ["pactl", "list", "short", "sources"]:
            return SOURCES
        if command[0] == "wpctl":
            return WPCTL
        raise AudioBackendError(f"unexpected {command}")

    devices = PipeWireBackend(runner=runner).list_devices()
    sinks = [device for device in devices if device.direction == "out"]
    sources = [device for device in devices if device.direction == "in"]
    assert [device.name for device in sinks] == ["pTron BT SPEAKER", "USB Audio Device"]
    assert sinks[0].kind == "bluetooth"
    assert sinks[0].backend == "pipewire"
    assert sinks[0].id == "pipewire:bluez_output.AA_BB_CC_DD_EE_FF.1"
    assert sinks[0].available is True
    assert [device.id for device in sources] == [
        "pipewire:alsa_input.usb-C-Media_USB_Audio_Device-00.analog-stereo"
    ]
    assert all(".monitor" not in device.id for device in sources)


def test_missing_pipewire_server_is_reported():
    def runner(_command):
        raise AudioBackendError("Cannot run pactl: No such file")

    backend = PipeWireBackend(runner=runner)
    assert backend.list_devices() == []
    assert "pactl" in backend.last_error


def test_input_and_output_backends_are_chosen_separately():
    hybrid = HybridBackend()
    capture = hybrid.open_capture("plughw:1,0", 16000, 0, backend_name="alsa")
    pipe = hybrid.open_playback("pipewire:bluez_output.AA.1", 48000, backend_name="pipewire")
    usb = hybrid.open_playback("plughw:2,0", 16000, backend_name="alsa")
    assert isinstance(capture, AlsaCapture)
    assert isinstance(pipe, PipeWirePlayback)
    assert pipe.sink == "bluez_output.AA.1"
    assert isinstance(usb, AlsaPlayback)
    assert usb.device_id == "plughw:2,0"


def test_alsa_capture_is_written_to_pipewire_playback():
    pcm = b"\x01\x00\x02\x00\x03\x00"
    procs = []

    def popen(_command, **_kwargs):
        proc = FakeProc(alive=True)
        procs.append(proc)
        return proc

    capture = ScriptedCapture(pcm, sample_rate=16000)
    capture.start()
    playback = PipeWirePlayback("bluez_output.AA.1", 16000, popen=popen, which=_which_paplay, retry_interval=0)
    playback.start()
    fanout(capture.read(3), None, [playback], None, 4, 16000)
    assert bytes(procs[0].stdin.buf) == pcm
    assert playback.last_error == ""
    assert playback.status()["running"] is True


def test_bluetooth_sink_loss_and_reconnect_without_replacing_the_player():
    procs = []

    def popen(_command, **_kwargs):
        proc = FakeProc(alive=True, stderr=b"")
        procs.append(proc)
        return proc

    playback = PipeWirePlayback(
        "bluez_output.AA_BB_CC_DD_EE_FF.1",
        16000,
        popen=popen,
        which=_which_paplay,
        retry_interval=0,
    )
    playback.start()
    procs[0].alive = False
    procs[0].stderr = io.BytesIO(b"No such entity\n")
    playback.write(b"\x01\x00")
    assert playback is playback
    assert len(procs) == 2
    assert procs[1].stdin.buf == b"\x01\x00"
    assert playback.status()["running"] is True


def test_missing_bluetooth_sink_is_reported_and_does_not_raise():
    def popen(_command, **_kwargs):
        return FakeProc(alive=False, stderr=b"No such entity\n")

    playback = PipeWirePlayback("bluez_output.missing.1", 8000, popen=popen, which=_which_paplay, retry_interval=0)
    playback.start()
    playback.write(b"\x00\x00" * 50)
    assert "No such entity" in playback.last_error
    assert playback.status()["running"] is False


def test_missing_pipewire_client_bounds_the_buffer():
    playback = PipeWirePlayback(None, 16000, which=lambda _name: None, retry_interval=0)
    playback.start()
    chunk = b"\x00\x00" * 8000
    playback.write(chunk)
    playback.write(chunk)
    assert "paplay" in playback.last_error
    assert len(playback._pending) <= playback._max_pending


def test_route_plan_keeps_a_disconnected_pipewire_output():
    usb_in = SimpleNamespace(id=1)
    speaker = SimpleNamespace(
        id=2,
        name="pTron BT SPEAKER",
        enabled=True,
        alsa_device="pipewire:bluez_output.AA.1",
        device_id="pipewire:bluez_output.AA.1",
        audio_backend="pipewire",
        sample_rate=48000,
    )
    usb_out = SimpleNamespace(
        id=3,
        name="USB card",
        enabled=True,
        alsa_device="plughw:1,0",
        device_id="plughw:1,0",
        audio_backend="alsa",
        sample_rate=16000,
    )
    routes = [
        SimpleNamespace(input_channel_id=1, output_channel_id=2),
        SimpleNamespace(input_channel_id=1, output_channel_id=3),
        SimpleNamespace(input_channel_id=4, output_channel_id=2),
    ]
    available = {"plughw:1,0"}
    plan, notices = plan_routes(routes, {2: speaker, 3: usb_out}, available)
    assert [target.backend for target in plan[1]] == ["pipewire", "alsa"]
    assert plan[1][0].connected is False
    assert plan[1][1].connected is True
    assert plan[4][0].output_name == "pTron BT SPEAKER"
    assert any(item["reason"] == "disconnected" and "pTron" in item["detail"] for item in notices)
    assert usb_in.id == 1


def test_legacy_alsa_channel_still_resolves():
    legacy = SimpleNamespace(alsa_device="plughw:2,0", device_id="", audio_backend="")
    assert channel_endpoint(legacy) == ("plughw:2,0", "alsa")


def test_simulated_backend_is_unchanged():
    backend = get_backend("simulated")
    assert isinstance(backend, SimulatedBackend)
    assert [device.id for device in backend.list_devices()] == [
        "sim:radio-1",
        "sim:fm",
        "sim:handheld",
        "sim:ptt",
        "sim:jack",
        "sim:speaker",
        "sim:headset",
    ]
    assert all(device.backend == "simulated" for device in backend.list_devices())

    def no_cards(self):
        return []

    original = AlsaBackend.list_devices
    AlsaBackend.list_devices = no_cards
    try:
        assert isinstance(get_backend("auto"), SimulatedBackend)
    finally:
        AlsaBackend.list_devices = original

    def usb_card(self):
        return [DeviceInfo("plughw:1,0", "USB Audio", "in", "usb", None, "alsa")]

    AlsaBackend.list_devices = usb_card
    try:
        chosen = get_backend("auto")
        assert isinstance(chosen, HybridBackend)
        assert all(not device.id.startswith("sim:") for device in [DeviceInfo("plughw:1,0", "USB", "in", "usb")])
    finally:
        AlsaBackend.list_devices = original


def test_sqlite_migration_keeps_existing_rows(tmp_path):
    url = f"sqlite:///{tmp_path / 'legacy.db'}"
    init_engine(url, force=True)
    from soundcore.db import get_engine

    engine = get_engine()
    with engine.begin() as conn:
        conn.exec_driver_sql(
            """
            CREATE TABLE channels (
                id INTEGER PRIMARY KEY,
                name VARCHAR(120) NOT NULL,
                slug VARCHAR(64) NOT NULL,
                direction VARCHAR(8) NOT NULL,
                alsa_device VARCHAR(160) NOT NULL,
                usb_id VARCHAR(160),
                device_kind VARCHAR(16) NOT NULL,
                sample_rate INTEGER NOT NULL,
                format VARCHAR(16) NOT NULL,
                gain_db FLOAT NOT NULL,
                enabled BOOLEAN NOT NULL,
                created_at DATETIME,
                updated_at DATETIME
            )
            """
        )
        conn.exec_driver_sql(
            """
            INSERT INTO channels (
                id, name, slug, direction, alsa_device, usb_id, device_kind,
                sample_rate, format, gain_db, enabled
            ) VALUES (1, 'Radio', 'radio', 'in', 'plughw:1,0', 'card1:Device', 'usb', 16000, 's16le', 0, 1)
            """
        )
        conn.exec_driver_sql("CREATE TABLE recordings (id INTEGER PRIMARY KEY, note TEXT)")
        conn.exec_driver_sql("INSERT INTO recordings (id, note) VALUES (7, 'keep-me')")
    create_all()
    migrate_schema(engine)
    with engine.connect() as conn:
        row = conn.exec_driver_sql(
            "SELECT name, alsa_device, audio_backend, device_id FROM channels WHERE id = 1"
        ).one()
        kept = conn.exec_driver_sql("SELECT note FROM recordings WHERE id = 7").one()
    assert row == ("Radio", "plughw:1,0", "alsa", "plughw:1,0")
    assert kept[0] == "keep-me"


def test_worker_unit_uses_soundcore_and_the_pulse_socket():
    unit = Path("deploy/soundcore-worker.service").read_text(encoding="utf-8")
    install = Path("install.sh").read_text(encoding="utf-8")
    assert "User=soundcore" in unit
    assert "User=root" not in unit
    assert "PULSE_SERVER=unix:/run/soundcore/pulse.sock" in unit
    assert "soundcore-audio" in unit
    assert "wait_for_pulse_socket.sh" in unit
    assert "pipewire-pulse" in install
    assert "pulseaudio-utils" in install
    assert "enable-linger" in install
    assert "soundcore-audio" in install
