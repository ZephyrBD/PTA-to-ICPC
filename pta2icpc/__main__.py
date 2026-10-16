import logging

import uvicorn

from .app import _build_default_app
from .auth import credentials_banner
from .config import load_connection, load_settings
from .logging_config import banner, configure_logging
from .paths import ensure_dirs

log = logging.getLogger(__name__)


def main() -> None:
    ensure_dirs()
    configure_logging()
    s = load_settings()
    conn = load_connection()
    if conn is None:
        log.error("connection not configured — run 'python -m pta2icpc.setup' first "
                  "(manual/contest/setup-connection.json)")
        raise SystemExit(1)
    if not s.api_password.strip():
        log.error("api_password not configured — run 'python -m pta2icpc.setup' first "
                  "(manual/config/config.json)")
        raise SystemExit(1)
    banner(credentials_banner(s.api_password))
    uvicorn.run(_build_default_app(conn), host=s.host, port=s.port,
                log_config=None)


if __name__ == "__main__":
    main()
