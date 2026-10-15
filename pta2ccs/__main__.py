import sys

import uvicorn

from .app import _build_default_app
from .config import load_connection, load_settings


def main() -> None:
    s = load_settings()
    conn = load_connection(s.manual_dir)
    if conn is None:
        print("connection not configured — run 'python -m pta2ccs.setup' first",
              file=sys.stderr)
        raise SystemExit(1)
    uvicorn.run(_build_default_app(conn), host=s.host, port=s.port)


if __name__ == "__main__":
    main()
