"""Flask application factory."""

from __future__ import annotations

import base64
import logging
import secrets
from datetime import timedelta
from pathlib import Path

from flask import Flask, g, jsonify, request, session
from flask_socketio import SocketIO, emit
from werkzeug.middleware.proxy_fix import ProxyFix

from soundcore import __version__
from soundcore.audio.detect import get_backend
from soundcore.audio.livestream import InProcessBus, UdpSubscriber
from soundcore.audio.service import AudioService
from soundcore.auth import current_user, require_csrf
from soundcore.auth import ensure_admin
from soundcore.config import Settings, load_settings
from soundcore.db import create_all, get_session, init_engine, session_scope
from soundcore.maintenance import start_background
from soundcore.models import User
from soundcore.seed import seed_channels
from soundcore.settings_store import ensure_settings
from soundcore.storage.scheduler import build_scheduler

socketio = SocketIO(async_mode="threading", cors_allowed_origins="*", manage_session=True)
logger = logging.getLogger(__name__)


def configure_logging(log_dir: Path) -> None:
    log_dir.mkdir(parents=True, exist_ok=True)
    root = logging.getLogger()
    if any(getattr(handler, "_soundcore", False) for handler in root.handlers):
        return
    root.setLevel(logging.INFO)
    formatter = logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s")
    stream = logging.StreamHandler()
    stream.setFormatter(formatter)
    stream._soundcore = True
    file_handler = logging.FileHandler(log_dir / "soundcore.log")
    file_handler.setFormatter(formatter)
    file_handler._soundcore = True
    root.addHandler(stream)
    root.addHandler(file_handler)


def create_app(settings: Settings | None = None) -> Flask:
    settings = settings or load_settings(dotenv=True)
    configure_logging(settings.log_dir)
    package_root = Path(__file__).resolve().parent
    app = Flask(
        __name__,
        template_folder=str(package_root / "web" / "templates"),
        static_folder=str(package_root / "web" / "static"),
    )
    app.config["SETTINGS"] = settings
    app.config["SECRET_KEY"] = settings.secret_key
    app.config["PERMANENT_SESSION_LIFETIME"] = timedelta(hours=12)
    app.config["SESSION_COOKIE_HTTPONLY"] = True
    app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
    app.config["SESSION_COOKIE_SECURE"] = settings.cookie_secure
    app.config["MAX_CONTENT_LENGTH"] = 2 * 1024 * 1024 * 1024
    app.config["TEMPLATES_AUTO_RELOAD"] = True
    if settings.behind_proxy:
        app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)

    init_engine(settings.database_url)
    create_all()
    backend = get_backend(settings.audio_backend)
    app.extensions["audio_backend"] = backend
    with session_scope() as db:
        ensure_admin(db, settings.admin_email, settings.admin_password, reset=settings.reset_admin)
        ensure_settings(db, settings)
        seed_channels(
            db,
            backend,
            enable_demo=settings.seed_demo,
            cap=settings.stream_cap,
            sample_rate=settings.default_sample_rate,
        )

    bus = InProcessBus()
    app.extensions["audio_bus"] = bus
    audio = None
    subscriber = None
    if settings.embed_worker:
        audio = AudioService(settings, backend, bus)
        audio.start()
        scheduler = build_scheduler(settings.timezone)
        app.extensions["scheduler"] = scheduler
        app.extensions["worker_stop"] = start_background(settings, scheduler)
    else:
        subscriber = UdpSubscriber(bus, settings.live_pcm_host, settings.live_pcm_port)
        try:
            subscriber.start()
        except OSError:
            logger.exception("live audio subscriber failed to bind")
            subscriber = None
    app.extensions["audio"] = audio
    app.extensions["udp_subscriber"] = subscriber

    from soundcore.api.activity import bp as activity_bp
    from soundcore.api.admin import bp as admin_bp
    from soundcore.api.channels import bp as channels_bp
    from soundcore.api.export_import import bp as transfer_bp
    from soundcore.api.recordings import bp as recordings_bp
    from soundcore.api.routing import bp as routing_bp
    from soundcore.api.status import bp as status_bp
    from soundcore.api.storage import bp as storage_bp
    from soundcore.api.streams import bp as streams_bp
    from soundcore.api.transcriptions import bp as transcriptions_bp
    from soundcore.web.pages import bp as pages_bp

    app.register_blueprint(pages_bp)
    app.register_blueprint(status_bp)
    app.register_blueprint(channels_bp)
    app.register_blueprint(streams_bp)
    app.register_blueprint(recordings_bp)
    app.register_blueprint(transcriptions_bp)
    app.register_blueprint(routing_bp)
    app.register_blueprint(storage_bp)
    app.register_blueprint(transfer_bp)
    app.register_blueprint(admin_bp)
    app.register_blueprint(activity_bp)
    _register_socket(bus)
    socketio.init_app(app)

    @app.before_request
    def _open_request():
        if request.endpoint in {"static", "healthz"}:
            return None
        g.db = get_session()
        if "csrf" not in session:
            session["csrf"] = secrets.token_urlsafe(32)
        user_id = session.get("user_id")
        g.current_user = g.db.get(User, user_id) if user_id else None
        if request.method == "POST":
            require_csrf()
        return None

    @app.teardown_request
    def _close_request(exc):
        db = g.pop("db", None)
        if db is None:
            return
        try:
            if exc is None:
                db.commit()
            else:
                db.rollback()
        finally:
            db.close()

    @app.context_processor
    def _inject():
        return {
            "csrf_token": session.get("csrf", ""),
            "current_user": current_user(),
            "version": __version__,
            "audio_backend_name": backend.name,
        }

    @app.template_filter("hm")
    def _hm(value):
        if not value:
            return "—"
        try:
            return value.strftime("%d %b %H:%M")
        except Exception:
            return str(value)

    @app.get("/healthz")
    def healthz():
        return jsonify({"status": "ok", "version": __version__, "audio_backend": backend.name})

    return app


def _register_socket(bus: InProcessBus) -> None:
    @socketio.on("connect")
    def _connect():
        if not session.get("user_id"):
            return False
        return True

    @socketio.on("listen")
    def _listen(data):
        if not session.get("user_id"):
            return {"ok": False, "error": "Sign in required"}
        try:
            channel_id = int((data or {}).get("channel_id"))
        except (TypeError, ValueError):
            return {"ok": False, "error": "Missing channel"}
        sid = request.sid

        def _send(sample_rate, pcm, _sid=sid, _channel_id=channel_id):
            socketio.emit(
                "pcm",
                {
                    "channel_id": _channel_id,
                    "sample_rate": sample_rate,
                    "data": base64.b64encode(pcm).decode("ascii"),
                },
                to=_sid,
            )

        bus.subscribe(channel_id, sid, _send)
        emit("listening", {"channel_id": channel_id})
        return {"ok": True, "channel_id": channel_id}

    @socketio.on("unlisten")
    def _unlisten(_data=None):
        bus.unsubscribe(request.sid)
        return {"ok": True}

    @socketio.on("disconnect")
    def _disconnect():
        bus.unsubscribe(request.sid)
