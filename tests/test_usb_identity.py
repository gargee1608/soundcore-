"""USB output identity does not follow ALSA card numbers."""

from soundcore.audio.capture import SimulatedPlayback
from soundcore.audio.detect import DeviceInfo, parse_alsa_listing
from soundcore.audio.identity import (
    USB_OUTPUT_SLOTS,
    UsbIdentity,
    assign_tokens,
    parse_snd_links,
)
from soundcore.audio.tone import play_test_tone, render_tone
from soundcore.db import session_scope
from soundcore.models import ActivityEvent, Channel
from soundcore.ops import sync_usb_output_slots

LISTING_A = """
card 2: Device [USB Audio], device 0: USB Audio [USB Audio]
card 0: Headphones [bcm2835 Headphones], device 0: bcm2835 Headphones [bcm2835 Headphones]
"""
LISTING_B = """
card 5: Device [USB Audio], device 0: USB Audio [USB Audio]
card 1: Headphones [bcm2835 Headphones], device 0: bcm2835 Headphones [bcm2835 Headphones]
"""


def _identities():
    return parse_snd_links(
        [("usb-C-Media_Electronics_Inc._USB_Audio_Device_SN1234-00", "../controlC2")],
        [("platform-xhci-hcd.1-usb-0:2:1.0", "../controlC2")],
        serial_by_card={2: "SN1234"},
        vendor_by_card={2: "0d8c"},
        product_by_card={2: "0014"},
    )


def test_serial_id_survives_a_card_number_change():
    first = parse_alsa_listing(LISTING_A, direction="out", identities=_identities())
    moved = parse_snd_links(
        [("usb-C-Media_Electronics_Inc._USB_Audio_Device_SN1234-00", "../controlC5")],
        [("platform-xhci-hcd.1-usb-0:2:1.0", "../controlC5")],
        serial_by_card={5: "SN1234"},
    )
    second = parse_alsa_listing(LISTING_B, direction="out", identities=moved)
    usb_first = next(device for device in first if device.kind == "usb")
    usb_second = next(device for device in second if device.kind == "usb")
    assert usb_first.id == usb_second.id == "alsa:usb-serial:SN1234:pcm0"
    assert usb_first.hw == "plughw:2,0"
    assert usb_second.hw == "plughw:5,0"
    assert all(device.kind != "usb" or not device.id.startswith("plughw:") for device in first)


def test_missing_serial_pins_the_physical_port():
    rows = [
        (2, "Device", 0, UsbIdentity(by_id="usb-C-Media_USB_Audio", by_path="pci-0000:00:14.0-usb-0:2:1.0")),
        (4, "Device", 0, UsbIdentity(by_id="usb-C-Media_USB_Audio", by_path="pci-0000:00:14.0-usb-0:4:1.0")),
    ]
    tokens = assign_tokens(rows)
    assert tokens[(2, 0)] == "usb-path:pci-0000:00:14.0-usb-0:2:1.0:pcm0"
    assert tokens[(4, 0)] == "usb-path:pci-0000:00:14.0-usb-0:4:1.0:pcm0"
    assert "2" not in tokens[(2, 0)].split(":")[1]


def test_identical_adapters_without_a_port_are_not_slotted(data_dir):
    ambiguous = assign_tokens(
        [
            (1, "Device", 0, UsbIdentity(by_id="usb-C-Media_USB_Audio")),
            (3, "Device", 0, UsbIdentity(by_id="usb-C-Media_USB_Audio")),
        ]
    )
    assert ambiguous[(1, 0)].startswith("usb-ambiguous:")
    devices = [
        DeviceInfo("alsa:usb-serial:ONE:pcm0", "USB Audio", "out", "usb", "usb-serial:ONE:pcm0", "alsa", True, "plughw:3,0"),
        DeviceInfo("plughw:0,0", "vc4-hdmi-0", "out", "hdmi", None, "alsa", True, "plughw:0,0"),
        DeviceInfo("alsa:usb-cardid:Device:pcm0", "USB Audio", "in", "usb", "usb-cardid:Device:pcm0", "alsa", True, "plughw:4,0"),
        DeviceInfo("alsa:" + ambiguous[(1, 0)], "USB Audio", "out", "usb", ambiguous[(1, 0)], "alsa", True, "plughw:1,0"),
    ]
    with session_scope() as db:
        seen = sync_usb_output_slots(db, devices)
        rows = db.query(Channel).filter(Channel.slug.like("usb-audio-%")).order_by(Channel.slug).all()
    assert [row.name for row in rows] == list(USB_OUTPUT_SLOTS)
    pinned = next(row for row in rows if row.usb_id == "usb-serial:ONE:pcm0")
    assert pinned.slug == "usb-audio-1"
    assert pinned.device_id == "alsa:usb-serial:ONE:pcm0"
    assert pinned.alsa_device == "plughw:3,0"
    assert pinned.enabled is True
    assert all(row.usb_id != ambiguous[(1, 0)] for row in rows)
    assert seen == [{"usb_id": "usb-serial:ONE:pcm0", "name": "USB Audio", "hw": "plughw:3,0", "id": "alsa:usb-serial:ONE:pcm0"}]


def test_replug_keeps_the_same_slot(data_dir):
    first = [
        DeviceInfo("alsa:usb-serial:ONE:pcm0", "USB Audio", "out", "usb", "usb-serial:ONE:pcm0", "alsa", True, "plughw:2,0"),
    ]
    second = [
        DeviceInfo("alsa:usb-serial:ONE:pcm0", "USB Audio", "out", "usb", "usb-serial:ONE:pcm0", "alsa", True, "plughw:6,0"),
        DeviceInfo("alsa:usb-path:port-b:pcm0", "USB Audio", "out", "usb", "usb-path:port-b:pcm0", "alsa", True, "plughw:1,0"),
    ]
    with session_scope() as db:
        sync_usb_output_slots(db, first)
        sync_usb_output_slots(db, second)
        one = db.query(Channel).filter_by(slug="usb-audio-1").one()
        two = db.query(Channel).filter_by(slug="usb-audio-2").one()
        one.enabled = False
        db.flush()
        sync_usb_output_slots(db, second)
        db.refresh(one)
        db.refresh(two)
    assert one.usb_id == "usb-serial:ONE:pcm0"
    assert one.alsa_device == "plughw:6,0"
    assert one.device_id == "alsa:usb-serial:ONE:pcm0"
    assert one.enabled is False
    assert two.usb_id == "usb-path:port-b:pcm0"


def test_local_capture_is_pinned_without_inventing_a_jack(data_dir):
    from soundcore.ops import sync_local_inputs

    moved = """
card 4: Headphones [bcm2835 Headphones], device 0: bcm2835 Headphones [bcm2835 Headphones]
"""
    first = next(device for device in parse_alsa_listing(LISTING_A, direction="in") if device.kind == "jack")
    second = next(device for device in parse_alsa_listing(moved, direction="in") if device.kind == "jack")
    assert first.id == second.id == "alsa:local:Headphones:pcm0"
    assert first.hw == "plughw:0,0"
    assert second.hw == "plughw:4,0"
    with session_scope() as db:
        assert sync_local_inputs(db, []) == []
        assert db.query(Channel).count() == 0
        found = sync_local_inputs(db, [second])
        row = db.query(Channel).one()
    assert found[0]["id"] == "alsa:local:Headphones:pcm0"
    assert row.device_kind == "local"
    assert row.direction == "in"
    assert "3.5" not in row.name


def test_connect_and_disconnect_are_logged(data_dir):
    from soundcore.audio.service import AudioService
    from soundcore.config import Settings

    class Backend:
        name = "alsa"

        def __init__(self):
            self.devices = []

        def list_devices(self):
            return list(self.devices)

        def resolve(self, device_id):
            for device in self.devices:
                if device.id == device_id:
                    return device.hw
            return ""

    device = DeviceInfo(
        "alsa:usb-serial:ONE:pcm0", "USB Audio", "out", "usb", "usb-serial:ONE:pcm0", "alsa", True, "plughw:2,0"
    )
    backend = Backend()
    backend.devices = [device]
    settings = Settings(secret_key="x", database_url="sqlite://", data_dir=data_dir)
    service = AudioService(settings, backend, None)
    service._reconcile()
    backend.devices = []
    service._reconcile()
    with session_scope() as db:
        kinds = [row.kind for row in db.query(ActivityEvent).order_by(ActivityEvent.id).all()]
    assert kinds == ["usb_device_connected", "usb_device_disconnected"]


def test_test_tone_writes_pcm_and_reports_a_missing_output():
    class Desk:
        def __init__(self):
            self.playback = SimulatedPlayback("sim:speaker", 16000)

        def open_playback(self, device_id, sample_rate, backend_name=None):
            if device_id.startswith("alsa:"):
                raise RuntimeError(f"{device_id} is not connected")
            return self.playback

    desk = Desk()
    played = play_test_tone(desk, "sim:speaker", 16000, "simulated")
    assert played["ok"] is True
    assert played["bytes"] == len(render_tone(16000))
    assert bytes(desk.playback.received) == render_tone(16000)
    missing = play_test_tone(desk, "alsa:usb-serial:GONE:pcm0", 16000, "alsa")
    assert missing["ok"] is False
    assert "not connected" in missing["error"]
    assert missing["bytes"] == 0


def test_replug_reopens_playback_on_the_new_card():
    from soundcore.audio.service import AudioService, RouteTarget
    from soundcore.config import Settings

    class Playback:
        def __init__(self, hw):
            self.device_id = hw
            self.opened_hw = hw
            self.proc = None
            self.last_error = ""

        def start(self):
            return None

        def stop(self):
            return None

        def write(self, _pcm):
            return None

    class Backend:
        name = "alsa"

        def __init__(self):
            self.hw = "plughw:2,0"
            self.opened = []

        def resolve(self, _device_id):
            return self.hw

        def open_playback(self, _device_id, _sample_rate, backend_name=None):
            self.opened.append(self.hw)
            return Playback(self.hw)

        def list_devices(self):
            return []

    backend = Backend()
    service = AudioService(Settings(secret_key="x", database_url="sqlite://", data_dir="data"), backend, None)
    target = RouteTarget(1, 2, "alsa:usb-serial:ONE:pcm0", 48000, "alsa", "USB Audio 1", True, "")
    service._routes = {1: [target]}
    spec = type("Spec", (), {"id": 1, "sample_rate": 48000, "slug": "in", "name": "Mic"})()
    playbacks = []
    open_ids = []
    service._sync_playbacks(spec, playbacks, open_ids)
    service._sync_playbacks(spec, playbacks, open_ids)
    assert backend.opened == ["plughw:2,0"]
    backend.hw = "plughw:6,0"
    service._sync_playbacks(spec, playbacks, open_ids)
    assert backend.opened == ["plughw:2,0", "plughw:6,0"]
    assert playbacks[0].opened_hw == "plughw:6,0"
    backend.hw = ""
    service._sync_playbacks(spec, playbacks, open_ids)
    assert playbacks[0].last_error == "not connected"
    service._sync_playbacks(spec, playbacks, open_ids)
    assert backend.opened == ["plughw:2,0", "plughw:6,0"]
