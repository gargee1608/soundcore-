"""Output mix: sum, per-route gain, soft limit, and no feedback route."""

import struct

from soundcore.audio.mixer import OutputMixer, mix_s16le
from soundcore.audio.service import same_device
from soundcore.db import session_scope
from soundcore.models import Route
from soundcore.ops import OpsError, save_channel, save_route
from soundcore.config import Settings
import pytest


def _pcm(values):
    return struct.pack("<" + "h" * len(values), *values)


def test_mix_sums_and_limits():
    quiet, peak = mix_s16le([(_pcm([1000, -1000]), 0.0), (_pcm([1000, -1000]), 0.0)], 2)
    assert struct.unpack("<2h", quiet) == (2000, -2000)
    loud, limited = mix_s16le([(_pcm([30000]), 0.0), (_pcm([30000]), 0.0)], 1)
    assert struct.unpack("<h", loud)[0] == 32767
    assert limited > 0.99
    gained, _level = mix_s16le([(_pcm([1000]), -6.0)], 1)
    assert struct.unpack("<h", gained)[0] < 1000


def test_member_change_does_not_reopen_a_healthy_writer():
    opened = []

    class Playback:
        def __init__(self):
            self.opened_hw = "plughw:1,0"
            self.last_error = ""
            self.proc = None
            self.writes = 0

        def start(self):
            return None

        def stop(self):
            return None

        def write(self, pcm):
            self.writes += 1

    def open_playback(device_id, rate, backend):
        opened.append(device_id)
        return Playback()

    mixer = OutputMixer("alsa:usb-serial:A:pcm0", 16000, "alsa", open_playback)
    mixer.set_members({1: 0.0})
    mixer.ensure_writer("plughw:1,0")
    mixer.set_members({1: -3.0, 2: 0.0})
    mixer.ensure_writer("plughw:1,0")
    assert opened == ["alsa:usb-serial:A:pcm0"]
    mixer.push(1, _pcm([100] * 320))
    mixer.push(9, _pcm([100]))
    assert 9 not in mixer._latest


def test_feedback_route_is_refused(data_dir):
    settings = Settings(secret_key="x", database_url="sqlite://", data_dir=data_dir)
    with session_scope() as db:
        src = save_channel(
            db, settings, channel_id=None, name="Mic", alsa_device="alsa:usb-serial:A:pcm0",
            device_id="alsa:usb-serial:A:pcm0", usb_id="usb-serial:A:pcm0", audio_backend="alsa",
            sample_rate=16000, gain_db=0, enabled=True, direction="in", device_kind="usb",
        )
        dst = save_channel(
            db, settings, channel_id=None, name="USB Audio 1", alsa_device="plughw:1,0",
            device_id="alsa:usb-serial:A:pcm0", usb_id="usb-serial:A:pcm0", audio_backend="alsa",
            sample_rate=48000, gain_db=0, enabled=True, direction="out", device_kind="usb",
        )
        other = save_channel(
            db, settings, channel_id=None, name="USB Audio 2", alsa_device="plughw:2,0",
            device_id="alsa:usb-serial:B:pcm0", usb_id="usb-serial:B:pcm0", audio_backend="alsa",
            sample_rate=48000, gain_db=0, enabled=True, direction="out", device_kind="usb",
        )
        with pytest.raises(OpsError, match="back"):
            save_route(db, src.id, dst.id, enabled=True, gain_db=0)
        route = save_route(db, src.id, other.id, enabled=True, gain_db=-6)
        assert route.gain_db == -6
        third = save_channel(
            db, settings, channel_id=None, name="Line", alsa_device="alsa:local:Headphones:pcm0",
            device_id="alsa:local:Headphones:pcm0", usb_id="", audio_backend="alsa",
            sample_rate=16000, gain_db=0, enabled=True, direction="in", device_kind="local",
        )
        save_route(db, src.id, other.id, enabled=False, gain_db=-6)
        save_route(db, third.id, other.id, enabled=True, gain_db=0)
        save_route(db, third.id, dst.id, enabled=True, gain_db=0)
        pairs = {(row.input_channel_id, row.output_channel_id) for row in db.query(Route).all()}
        assert (src.id, other.id) in pairs
        assert (third.id, other.id) in pairs
        assert (third.id, dst.id) in pairs
    assert same_device(src, dst) is True
