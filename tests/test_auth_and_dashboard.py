from soundcore.app import create_app
from soundcore.auth import ensure_admin
from soundcore.config import Settings
from soundcore.db import session_scope


def _settings(tmp_path, **overrides):
    data = tmp_path / "data"
    data.mkdir(parents=True, exist_ok=True)
    values = dict(
        secret_key="test-secret",
        database_url=f"sqlite:///{tmp_path / 'app.db'}",
        data_dir=data,
        admin_email="admin@example.com",
        admin_password="desk-secret",
        audio_backend="simulated",
        embed_worker=False,
        seed_demo=True,
        stream_cap=8,
        live_pcm_port=0,
        host="127.0.0.1",
        port=8741,
    )
    values.update(overrides)
    return Settings(**values)


def test_admin_is_not_created_without_a_password(data_dir):
    with session_scope() as session:
        assert ensure_admin(session, "admin@example.com", "") is None
        assert ensure_admin(session, "", "something") is None


def test_dashboard_requires_login_and_renders(tmp_path):
    app = create_app(_settings(tmp_path))
    client = app.test_client()
    blocked = client.get("/")
    assert blocked.status_code in (302, 401)
    page = client.get("/login")
    assert page.status_code == 200
    html = page.get_data(as_text=True)
    token = html.split('name="csrf_token" value="', 1)[1].split('"', 1)[0]
    bad = client.post("/login", data={"email": "admin@example.com", "password": "nope", "csrf_token": token})
    assert bad.status_code == 401
    ok = client.post(
        "/login",
        data={"email": "admin@example.com", "password": "desk-secret", "csrf_token": token},
        follow_redirects=True,
    )
    body = ok.get_data(as_text=True)
    assert ok.status_code == 200
    assert "Channels on the air" in body
    assert "Radio 1" in body
    assert "Performance" in body or "Active streams" in body
    status = client.get("/api/status")
    payload = status.get_json()
    assert payload["capacity"]["cap"] == 8
    assert payload["capacity"]["active"] == 3
    assert payload["disk"]["level"] in {"ok", "warning", "critical"}
    health = client.get("/healthz")
    assert health.status_code == 200
    assert health.get_json()["audio_backend"] == "simulated"
