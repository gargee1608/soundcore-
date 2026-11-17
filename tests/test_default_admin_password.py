from pathlib import Path

from soundcore.config import DEFAULT_ADMIN_PASSWORD, Settings, load_settings


def test_settings_defaults_to_the_shipped_admin_password(tmp_path):
    settings = Settings(
        secret_key="test-secret",
        database_url="sqlite://",
        data_dir=tmp_path,
    )
    assert settings.admin_password == "C1nb3rr1"
    assert DEFAULT_ADMIN_PASSWORD == "C1nb3rr1"


def test_load_settings_uses_the_shipped_admin_password_when_unset(tmp_path):
    settings = load_settings(
        environ={
            "SOUNDCORE_DATA_DIR": str(tmp_path / "data"),
            "SOUNDCORE_SECRET_KEY": "test-secret",
            "SOUNDCORE_CONFIG": str(tmp_path / "missing.json"),
        }
    )
    assert settings.admin_password == "C1nb3rr1"


def test_env_example_ships_the_admin_password():
    text = Path(".env.example").read_text(encoding="utf-8")
    assert "SOUNDCORE_ADMIN_PASSWORD=C1nb3rr1\n" in text
