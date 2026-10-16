import sys

import uvicorn

from .app import _build_default_app
from .auth import credentials_banner
from .config import load_connection, load_settings
from .paths import ensure_dirs


def main() -> None:
    ensure_dirs()
    s = load_settings()
    conn = load_connection()
    if conn is None:
        print("connection not configured — run 'python -m pta2icpc.setup' first "
              "(manual/contest/setup-connection.json)", file=sys.stderr)
        raise SystemExit(1)
    if not s.api_password.strip():
        print("api_password not configured — run 'python -m pta2icpc.setup' first "
              "(manual/config/config.json)", file=sys.stderr)
        raise SystemExit(1)
    print(credentials_banner(s.api_password), flush=True)
    uvicorn.run(_build_default_app(conn), host=s.host, port=s.port)


if __name__ == "__main__":
    main()
