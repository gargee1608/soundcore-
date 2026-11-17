"""Write a SoundCore export zip. Usage: python scripts/export_data.py [dest.zip]"""

from __future__ import annotations

import sys
from pathlib import Path

from soundcore.config import load_settings
from soundcore.db import create_all, init_engine, session_scope
from soundcore.transfer import build_export_zip


def main() -> None:
    settings = load_settings(dotenv=True)
    dest = Path(sys.argv[1] if len(sys.argv) > 1 else "soundcore-export.zip")
    init_engine(settings.database_url)
    create_all()
    with session_scope() as session:
        dest.write_bytes(build_export_zip(session, settings.data_dir))
    print(dest.resolve())


if __name__ == "__main__":
    main()
