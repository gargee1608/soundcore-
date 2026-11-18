from soundcore.audio.mixer import mix_pcm


def test_mix_pcm_sums_two_sources_with_limiting():
    a = b"\x00\x40" + b"\x00\x00" * 3
    b = b"\x00\x40" + b"\x00\x00" * 3
    mixed = mix_pcm([a, b])
    samples = [mixed[i : i + 2] for i in range(0, len(mixed), 2)]
    assert samples[0] == b"\x00\x80"
    assert samples[1] == b"\x00\x00"


def test_mix_pcm_empty():
    assert mix_pcm([]) == b""