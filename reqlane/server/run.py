"""`python -m reqlane.server.run` — start the daemon (used by `reqlane serve` and by autostart)."""
from __future__ import annotations

import uvicorn

from .. import config
from .app import create_app


def main() -> None:
    import os
    pidf = config.home() / "daemon.pid"
    pidf.write_text(str(os.getpid()), encoding="utf-8")
    try:
        uvicorn.run(create_app(), host="127.0.0.1", port=config.port(), log_level="warning")
    finally:
        try:
            pidf.unlink()
        except OSError:
            pass


if __name__ == "__main__":
    main()
