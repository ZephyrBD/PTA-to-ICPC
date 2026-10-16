"""Central logging configuration shared by every pta2icpc entry point.

All runtime logging — our own module loggers plus uvicorn's — goes through
one formatter to BOTH stderr and ``manual/pta2icpc.log`` (size-rotated).
Only entry points call :func:`configure_logging`; importing a module must
never reconfigure logging (it would pollute tests).

The console is colorized in the xcpc-tools style (scheme B): a gray
timestamp, an uncolored short logger name right-aligned to
``_NAME_WIDTH`` columns, and a level-colored ``[X]`` tag::

    {dd} {hh:mm:ss} {shortname:>9} [{X}] {message}

The log FILE uses the exact same layout but is always plain ASCII — no ANSI
escape sequences ever reach ``manual/pta2icpc.log``. The startup credentials
banner goes through :func:`banner`, which marks the record ``console_only``
so the file handler's filter drops it (the ``api_password`` must never hit
disk).
"""

from __future__ import annotations

import logging
import os
import sys
import time
from logging.handlers import RotatingFileHandler
from pathlib import Path

from . import paths

LOG_FILE_MAX_BYTES = 5 * 1024 * 1024
LOG_FILE_BACKUPS = 3

_NAME_WIDTH = 9
_TIME_FORMAT = "%d %H:%M:%S"

_LEVEL_TAG = {
    logging.DEBUG: "D",
    logging.INFO: "I",
    logging.WARNING: "W",
    logging.ERROR: "E",
    logging.CRITICAL: "C",
}
_LEVEL_COLOR = {
    logging.DEBUG: "\x1b[90m",
    logging.INFO: "\x1b[36m",
    logging.WARNING: "\x1b[33m",
    logging.ERROR: "\x1b[31m",
    logging.CRITICAL: "\x1b[1;91m",
}
_TIMESTAMP_COLOR = "\x1b[90m"
_RESET = "\x1b[0m"
_ESC = "\x1b"

_ENABLE_VT_SEQUENCE = 0x0004  # ENABLE_VIRTUAL_TERMINAL_PROCESSING

_SHORT_NAMES = {
    "pta2icpc.pta.client": "pta",
    "pta2icpc.logging_config": "logging",
    "pta2icpc.__main__": "main",
    "pta2icpc.init": "init",
    "uvicorn": "server",
    "uvicorn.error": "server",
    "uvicorn.access": "access",
}


def short_name(name: str) -> str:
    """Map a logger name to its short display name (centralized here)."""
    if name in _SHORT_NAMES:
        return _SHORT_NAMES[name]
    if name.startswith("pta2icpc."):
        return name[len("pta2icpc."):]
    return name


class LogFormatter(logging.Formatter):
    """xcpc-tools-style formatter; optional 16-color ANSI on the console."""

    def __init__(self, fmt: str | None = None, *, use_color: bool) -> None:
        super().__init__(fmt or "%(message)s")
        self.use_color = use_color

    def format(self, record: logging.LogRecord) -> str:
        try:
            return self._render(record)
        except Exception:  # never lose a log line to a formatting bug
            try:
                return logging.Formatter().format(record)
            except Exception:
                return f"{record.levelname}: {record.getMessage()}"

    def _render(self, record: logging.LogRecord) -> str:
        message = record.getMessage()
        if record.exc_info and not record.exc_text:
            record.exc_text = self.formatException(record.exc_info)
        if record.exc_text:
            message = message + "\n" + record.exc_text
        if record.stack_info:
            message = message + "\n" + self.formatStack(record.stack_info)

        timestamp = time.strftime(_TIME_FORMAT, time.localtime(record.created))
        name = short_name(record.name)
        padded = f"{name:>{_NAME_WIDTH}}"
        tag = _LEVEL_TAG.get(record.levelno)
        if tag is None:
            tag = record.levelname[:1] if record.levelname else "?"
        message = message.replace(_ESC, "")
        plain_prefix = f"{timestamp} {padded} [{tag}] "

        if self.use_color:
            tag_color = _LEVEL_COLOR.get(record.levelno, "")
            colored_tag = (f"{tag_color}[{tag}]{_RESET}" if tag_color
                           else f"[{tag}]")
            prefix = (f"{_TIMESTAMP_COLOR}{timestamp}{_RESET} "
                      f"{padded} {colored_tag} ")
        else:
            prefix = plain_prefix

        lines = message.split("\n")
        out = prefix + lines[0]
        indent = " " * len(plain_prefix)
        for line in lines[1:]:
            out += "\n" + indent + line
        return out


def _enable_windows_vt() -> bool:
    """Enable VT processing on the Windows STDERR console.

    Zero-dependency ``ctypes`` only. Returns True when the current console
    processes escape sequences and False otherwise. We target the STDERR
    handle (``-12``) because the console handler writes to ``sys.stderr`` —
    probing STDOUT would colorize a stream that may not render escapes. On
    non-Windows terminals escapes just work, so no probe is needed.
    """
    if sys.platform != "win32":
        return True
    try:
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.GetStdHandle.argtypes = [wintypes.DWORD]
        kernel32.GetStdHandle.restype = wintypes.HANDLE
        kernel32.GetConsoleMode.argtypes = [wintypes.HANDLE,
                                            ctypes.POINTER(wintypes.DWORD)]
        kernel32.GetConsoleMode.restype = wintypes.BOOL
        kernel32.SetConsoleMode.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        kernel32.SetConsoleMode.restype = wintypes.BOOL

        invalid = ctypes.c_void_p(-1).value
        handle = kernel32.GetStdHandle(-12)  # STDERR
        if not handle or handle == invalid:
            return False
        mode = wintypes.DWORD()
        if not kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
            return False
        return bool(kernel32.SetConsoleMode(handle,
                                            mode.value | _ENABLE_VT_SEQUENCE))
    except Exception:
        return False


def _auto_color(stream) -> bool:
    """Decide whether the console stream should be colorized."""
    if stream is None or not getattr(stream, "isatty", lambda: False)():
        return False
    if os.environ.get("NO_COLOR") or os.environ.get("PTA2ICPC_NO_COLOR"):
        return False
    if os.environ.get("TERM") == "dumb":
        return False
    if sys.platform == "win32":
        return _enable_windows_vt()
    return True


def _file_only_filter(record: logging.LogRecord) -> bool:
    """Handler filter: drop ``console_only`` records from the log file."""
    return not getattr(record, "console_only", False)


def configure_logging(level: int = logging.INFO, log_file=None,
                      color: bool | None = None) -> None:
    """Route root logging to stderr + a size-rotating file.

    ``log_file`` defaults to ``paths.LOG_FILE`` resolved at CALL time (never
    bound as a default argument, so monkeypatching ``paths`` works). The
    function is idempotent: handlers installed by a previous call are
    removed before the fresh pair is added, so repeated calls never stack.

    ``color`` defaults to auto-detection (a real stderr TTY, no ``NO_COLOR``
    / ``PTA2ICPC_NO_COLOR``, and — on Windows — a successful VT enable);
    tests inject ``True``/``False`` explicitly. The console handler is the
    only colored one; the file handler always writes plain text.

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

    # Console handler goes to stderr (uvicorn's default too). Under
    # pythonw.exe there is no console and sys.stderr is None — the stream
    # handler then silently no-ops, but the file handler still records.
    stream_handler = logging.StreamHandler()
    if color is None:
        color = _auto_color(stream_handler.stream)
    stream_handler.setFormatter(LogFormatter(use_color=color))
    stream_handler._pta2icpc = True

    log_file.parent.mkdir(parents=True, exist_ok=True)
    file_handler = RotatingFileHandler(
        log_file, maxBytes=LOG_FILE_MAX_BYTES, backupCount=LOG_FILE_BACKUPS,
        encoding="utf-8")
    file_handler.setFormatter(LogFormatter(use_color=False))
    file_handler.addFilter(_file_only_filter)
    file_handler._pta2icpc = True

    root.addHandler(stream_handler)
    root.addHandler(file_handler)
    root.setLevel(level)


def banner(text: str) -> None:
    """Log the startup credentials banner to the console ONLY.

    The record carries ``console_only`` so the file handler's filter drops
    it — this is the one deliberate place the ``api_password`` is shown, and
    it must never reach ``manual/pta2icpc.log``.
    """
    logging.getLogger("pta2icpc.init").info(
        text, extra={"console_only": True})
