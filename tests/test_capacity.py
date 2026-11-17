from soundcore.capacity import can_start_another, evaluate_capacity
from soundcore.config import Settings
from soundcore.db import session_scope
from soundcore.ops import OpsError, save_channel
from soundcore.settings_store import ensure_settings, set_setting


def test_capacity_levels():
    assert evaluate_capacity(6, 8).level == "ok"
    warning = evaluate_capacity(7, 8, warn_ratio=0.8)
    assert warning.level == "warning"
    assert "Performance warning" in warning.message
    blocked = evaluate_capacity(8, 8)
    assert blocked.level == "blocked"
    assert blocked.can_start is False
    assert can_start_another(7, 8) is True
    assert can_start_another(8, 8) is False
    assert evaluate_capacity(1, 0).level == "blocked"


def test_enabling_past_the_cap_is_refused(data_dir):
    settings = Settings(
        secret_key="test",
        database_url="sqlite://",
        data_dir=data_dir,
        stream_cap=2,
        admin_email="",
        admin_password="",
    )
    with session_scope() as session:
        ensure_settings(session, settings)
        set_setting(session, "stream_cap", "2")
        save_channel(
            session, settings, channel_id=None, name="Radio 1", alsa_device="sim:a",
            sample_rate=16000, gain_db=0, enabled=True, direction="in",
        )
        save_channel(
            session, settings, channel_id=None, name="Radio 2", alsa_device="sim:b",
            sample_rate=16000, gain_db=0, enabled=True, direction="in",
        )
        with pytest_raises():
            save_channel(
                session, settings, channel_id=None, name="Radio 3", alsa_device="sim:c",
                sample_rate=16000, gain_db=0, enabled=True, direction="in",
            )


def pytest_raises():
    import pytest

    return pytest.raises(OpsError, match="Stream cap")
