"""Restore a SoundCore export zip. Usage: python scripts/import_data.py export.zip"""

from __future__ import annotations

import sys
from pathlib import Path

from soundcore.config import load_settings
from soundcore.db import create_all, init_engine, session_scope
from soundcore.transfer import import_zip


def main() -> None:
    if len(sys.argv) != 2:
        print("Usage: python scripts/import_data.py export.zip", file=sys.stderr)
        raise SystemExit(2)
    settings = load_settings(dotenv=True)
    payload = Path(sys.argv[1]).read_bytes()
    init_engine(settings.database_url)
    create_all()
    with session_scope() as session:
        summary = import_zip(session, payload, settings.data_dir)
    print(summary)


if __name__ == "__main__":
    main()
