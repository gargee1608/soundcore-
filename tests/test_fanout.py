from soundcore.audio.capture import ScriptedCapture, SimulatedPlayback
from soundcore.audio.livestream import InProcessBus
from soundcore.audio.recorder import WavRecorder, fanout


def test_record_and_relay_share_the_same_buffer(tmp_path):
    pcm = b"\x01\x00\x02\x00\xff\x7f"
    capture = ScriptedCapture(pcm, sample_rate=16000)
    capture.start()
    writer = WavRecorder(tmp_path / "clip.wav", 16000)
    writer.open()
    playback = SimulatedPlayback("sim:speaker", 16000)
    playback.start()
    heard = []
    bus = InProcessBus()
    bus.subscribe(7, "test", lambda rate, chunk: heard.append((rate, chunk)))
    chunk = capture.read(3)
    fanout(chunk, writer, [playback], bus, 7, 16000)
    stats = writer.close()
    assert stats["frames"] == 3
    assert bytes(playback.received) == pcm
    assert heard == [(16000, pcm)]
    assert (tmp_path / "clip.wav").stat().st_size > 44
