"""Web process entry: `python -m soundcore` or `python -m soundcore.server`."""

from __future__ import annotations

from soundcore.app import create_app, socketio
from soundcore.config import load_settings


def main() -> None:
    settings = load_settings(dotenv=True)
    app = create_app(settings)
    socketio.run(
        app,
        host=settings.host,
        port=settings.port,
        allow_unsafe_werkzeug=True,
        use_reloader=False,
        log_output=False,
    )


if __name__ == "__main__":
    main()
