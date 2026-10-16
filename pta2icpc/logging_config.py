"""Central logging configuration shared by every pta2icpc entry point.

All runtime logging — our own module loggers plus uvicorn's — goes through
one formatter to BOTH stderr and ``manual/pta2icpc.log`` (size-rotated).
Only entry points call :func:`configure_logging`; importing a module must
never reconfigure logging (it would pollute tests).
"""

from __future__ import annotations

import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path

from . import paths

LOG_FORMAT = "%(asctime)s %(levelname)-8s %(name)s: %(message)s"
DATE_FORMAT = "%Y-%m-%d %H:%M:%S"
LOG_FILE_MAX_BYTES = 5 * 1024 * 1024
LOG_FILE_BACKUPS = 3


def configure_logging(level: int = logging.INFO, log_file=None) -> None:
    """Route root logging to stderr + a size-rotating file.

    ``log_file`` defaults to ``paths.LOG_FILE`` resolved at CALL time (never
    bound as a default argument, so monkeypatching ``paths`` works). The
    function is idempotent: handlers installed by a previous call are
    removed before the fresh pair is added, so repeated calls never stack.

    uvicorn's loggers are left untouched on purpose — with
    ``uvicorn.run(..., log_config=None)`` they keep propagating to root, so
    uvicorn and our own records share this one format.
    """
    if log_file is None:
        log_file = paths.LOG_FILE
    log_file = Path(log_file)

    root = logging.getLogger()
    for handler in list(root.handlers):
        if getattr(handler, "_pta2icpc", False):
            root.removeHandler(handler)
            handler.close()

    formatter = logging.Formatter(LOG_FORMAT, DATE_FORMAT)

    # Console handler goes to stderr (uvicorn's default too). Under
    # pythonw.exe there is no console and sys.stderr is None — the stream
    # handler then silently no-ops, but the file handler still records.
    stream_handler = logging.StreamHandler()
    stream_handler.setFormatter(formatter)
    stream_handler._pta2icpc = True

    log_file.parent.mkdir(parents=True, exist_ok=True)
    file_handler = RotatingFileHandler(
        log_file, maxBytes=LOG_FILE_MAX_BYTES, backupCount=LOG_FILE_BACKUPS,
        encoding="utf-8")
    file_handler.setFormatter(formatter)
    file_handler._pta2icpc = True

    root.addHandler(stream_handler)
    root.addHandler(file_handler)
    root.setLevel(level)
