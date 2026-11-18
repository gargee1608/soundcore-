"""A short tone written to a real playback device. Nothing is invented if the open fails."""

from __future__ import annotations

import json
from pathlib import Path

from soundcore.audio.pcm import tone

_REQUEST = "test_tone_request.json"


def render_tone(sample_rate: int, *, seconds: float = 0.6, freq: float = 440.0) -> bytes:
    frames = max(1, int(sample_rate * seconds))
    pcm, _phase = tone(sample_rate, frames, freq, phase=0.0, amplitude=0.2)
    return pcm


def play_test_tone(backend, device_id: str, sample_rate: int, backend_name: str = "alsa") -> dict:
    try:
        playback = backend.open_playback(device_id, sample_rate, backend_name=backend_name)
        playback.start()
    except Exception as exc:
        return {"ok": False, "error": str(exc), "bytes": 0}
    pcm = render_tone(sample_rate)
    try:
        playback.write(pcm)
    except Exception as exc:
        try:
            playback.stop()
        except Exception:
            pass
        return {"ok": False, "error": str(exc), "bytes": 0}
    error = getattr(playback, "last_error", "") or ""
    try:
        playback.stop()
    except Exception:
        pass
    if error:
        return {"ok": False, "error": error, "bytes": 0}
    return {"ok": True, "error": "", "bytes": len(pcm)}


def queue_test_tone(data_dir, channel_id: int) -> None:
    path = Path(data_dir) / "logs" / _REQUEST
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"channel_id": int(channel_id)}), encoding="utf-8")


def take_test_tone(data_dir) -> int | None:
    path = Path(data_dir) / "logs" / _REQUEST
    if not path.is_file():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        channel_id = int(payload.get("channel_id") or 0)
    except (OSError, json.JSONDecodeError, TypeError, ValueError):
        channel_id = 0
    try:
        path.unlink()
    except OSError:
        pass
    return channel_id or None
