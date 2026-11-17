from datetime import datetime

import pytest

from soundcore.audio.naming import (
    build_recording_filename,
    build_recording_relative_path,
    is_unique_id,
    new_unique_id,
    slugify,
)


def test_slug_strips_unsafe_characters():
    assert slugify("Radio 1 / North") == "radio-1-north"
    assert slugify("   ") == "channel"
    assert slugify("A" * 80) == "a" * 48


def test_filename_uses_channel_timestamp_and_id():
    started = datetime(2026, 11, 17, 6, 30, 4)
    relative = build_recording_relative_path("Radio 1", started, "ab12cd34")
    assert relative == "recordings/radio-1/2026/11/17/radio-1_2026-11-17T06-30-04_ab12cd34.wav"
    assert build_recording_filename("radio-1", started, "ab12cd34").endswith("_ab12cd34.wav")


def test_unique_ids_are_distinct_and_safe():
    first = new_unique_id()
    second = new_unique_id()
    assert first != second
    assert is_unique_id(first)
    with pytest.raises(ValueError):
        build_recording_filename("radio-1", datetime(2026, 1, 1, 0, 0, 0), "../etc")
