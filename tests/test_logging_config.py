import logging
import re
from logging.handlers import RotatingFileHandler

import pytest

from pta2icpc import paths
from pta2icpc.logging_config import (LOG_FILE_BACKUPS, LOG_FILE_MAX_BYTES,
                                     LogFormatter, _auto_color,
                                     _enable_windows_vt, _file_only_filter,
                                     banner, configure_logging, short_name)

_ANSI_RE = re.compile("\x1b\\[[0-9;]*m")


def _strip_ansi(text: str) -> str:
    return _ANSI_RE.sub("", text)


def _normalize(text: str) -> str:
    return text.replace("\r\n", "\n").rstrip("\n")


@pytest.fixture
def clean_root_logging():
    root = logging.getLogger()
    level = root.level
    yield
    for handler in list(root.handlers):
        if getattr(handler, "_pta2icpc", False):
            root.removeHandler(handler)
            handler.close()
    root.setLevel(level)


def _our_handlers():
    return [h for h in logging.getLogger().handlers
            if getattr(h, "_pta2icpc", False)]


def _console_handler():
    return next(h for h in _our_handlers()
                if isinstance(h, logging.StreamHandler)
                and not isinstance(h, RotatingFileHandler))


def _file_handler():
    return next(h for h in _our_handlers() if isinstance(h, RotatingFileHandler))


def test_installs_one_stream_and_one_rotating_file(tmp_path,
                                                   clean_root_logging):
    configure_logging(log_file=tmp_path / "pta2icpc.log", color=True)
    ours = _our_handlers()
    streams = [h for h in ours
               if isinstance(h, logging.StreamHandler)
               and not isinstance(h, RotatingFileHandler)]
    rotators = [h for h in ours if isinstance(h, RotatingFileHandler)]
    assert len(streams) == 1
    assert len(rotators) == 1
    assert rotators[0].maxBytes == LOG_FILE_MAX_BYTES == 5 * 1024 * 1024
    assert rotators[0].backupCount == LOG_FILE_BACKUPS == 3
    assert all(isinstance(h.formatter, LogFormatter) for h in ours)
    assert _console_handler().formatter.use_color is True
    assert _file_handler().formatter.use_color is False


def test_console_colored_has_ansi_tag_and_timestamp(tmp_path, capsys,
                                                    clean_root_logging):
    configure_logging(log_file=tmp_path / "pta2icpc.log", color=True)
    logging.getLogger("pta2icpc.poller").warning("hello world")
    for handler in _our_handlers():
        handler.flush()
    err = capsys.readouterr().err
    assert "\x1b[" in err
    assert "[W]" in err
    plain = _strip_ansi(err)
    assert re.search(r"^\d\d \d\d:\d\d:\d\d\s+poller \[W\] hello world$",
                     _normalize(plain))


def test_name_right_aligned_to_width_nine(tmp_path, capsys,
                                          clean_root_logging):
    configure_logging(log_file=tmp_path / "pta2icpc.log", color=True)
    logging.getLogger("pta2icpc.pta.client").info("x")
    for handler in _our_handlers():
        handler.flush()
    plain = _strip_ansi(capsys.readouterr().err)
    assert re.search(r"^\d\d \d\d:\d\d:\d\d {7}pta \[I\] x$", _normalize(plain))


def test_file_log_plain_ascii_and_same_layout_as_console(tmp_path, capsys,
                                                         clean_root_logging):
    log_file = tmp_path / "pta2icpc.log"
    configure_logging(log_file=log_file, color=True)
    logging.getLogger("pta2icpc.poller").warning("hello world")
    for handler in _our_handlers():
        handler.flush()
    err = capsys.readouterr().err
    text = log_file.read_text(encoding="utf-8")
    assert "\x1b" not in text
    assert _normalize(text) == _normalize(_strip_ansi(err))


def test_multiline_message_continuation_indented(tmp_path, capsys,
                                                 clean_root_logging):
    configure_logging(log_file=tmp_path / "pta2icpc.log", color=True)
    logging.getLogger("pta2icpc.poller").info("first\nsecond")
    for handler in _our_handlers():
        handler.flush()
    plain = _normalize(_strip_ansi(capsys.readouterr().err))
    first, second = plain.split("\n")
    assert first.endswith("first")
    prefix_width = len(first) - len("first")
    assert second == " " * prefix_width + "second"


def test_explicit_color_false_is_plain(tmp_path, capsys, clean_root_logging):
    configure_logging(log_file=tmp_path / "pta2icpc.log", color=False)
    logging.getLogger("pta2icpc.poller").info("hello")
    for handler in _our_handlers():
        handler.flush()
    assert "\x1b" not in capsys.readouterr().err


class _FakeTty:
    def isatty(self):
        return True


@pytest.mark.parametrize("level, sgr", [
    (logging.DEBUG, "\x1b[90m"),
    (logging.INFO, "\x1b[36m"),
    (logging.WARNING, "\x1b[33m"),
    (logging.ERROR, "\x1b[31m"),
    (logging.CRITICAL, "\x1b[1;91m"),
])
def test_console_tag_uses_design_color_per_level(level, sgr, tmp_path, capsys,
                                                 clean_root_logging):
    configure_logging(log_file=tmp_path / "pta2icpc.log", color=True)
    record = logging.LogRecord("pta2icpc.probe", level, __file__, 1, "m", (),
                               None)
    for handler in _our_handlers():
        handler.handle(record)
        handler.flush()
    err = capsys.readouterr().err
    tag = logging.getLevelName(level)[0]
    assert f"{sgr}[{tag}]\x1b[0m" in err
    assert _strip_ansi(err).endswith(f"[{tag}] m\n")


def test_console_timestamp_is_gray(tmp_path, capsys, clean_root_logging):
    configure_logging(log_file=tmp_path / "pta2icpc.log", color=True)
    logging.getLogger("pta2icpc.probe").info("m")
    for handler in _our_handlers():
        handler.flush()
    assert re.search(r"\x1b\[90m\d\d \d\d:\d\d:\d\d\x1b\[0m", capsys.readouterr().err)


def test_unknown_level_falls_back_to_first_letter(tmp_path, capsys,
                                                  clean_root_logging):
    configure_logging(log_file=tmp_path / "pta2icpc.log", color=True)
    logging.getLogger("pta2icpc.probe").log(25, "custom")
    for handler in _our_handlers():
        handler.flush()
    err = _strip_ansi(capsys.readouterr().err)
    assert re.search(r"\d\d \d\d:\d\d:\d\d\s+probe \[L\] custom", err)


def test_message_control_bytes_stripped_from_console_and_file(tmp_path,
                                                              capsys,
                                                              clean_root_logging):
    log_file = tmp_path / "pta2icpc.log"
    configure_logging(log_file=log_file, color=True)
    logging.getLogger("pta2icpc.probe").info("evil \x1b[999m url")
    for handler in _our_handlers():
        handler.flush()
    err = capsys.readouterr().err
    assert "\x1b[999m" not in err
    assert "\x1b[999m" not in log_file.read_text(encoding="utf-8")


def test_file_multiline_continuation_indented(tmp_path, clean_root_logging):
    log_file = tmp_path / "pta2icpc.log"
    configure_logging(log_file=log_file, color=False)
    logging.getLogger("pta2icpc.probe").info("first\nsecond")
    for handler in _our_handlers():
        handler.flush()
    first, second = _normalize(log_file.read_text(encoding="utf-8")).split("\n")
    assert first.endswith("first")
    assert second == " " * (len(first) - len("first")) + "second"


def test_enable_windows_vt_non_windows_is_true(monkeypatch):
    monkeypatch.setattr("pta2icpc.logging_config.sys.platform", "linux")
    assert _enable_windows_vt() is True


def test_enable_windows_vt_returns_bool(monkeypatch):
    assert isinstance(_enable_windows_vt(), bool)


def test_auto_color_rejects_dumb_terminal(monkeypatch):
    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.delenv("PTA2ICPC_NO_COLOR", raising=False)
    monkeypatch.setattr("pta2icpc.logging_config.sys.platform", "linux")
    monkeypatch.setenv("TERM", "dumb")
    assert _auto_color(_FakeTty()) is False


def test_auto_color_detection(monkeypatch):
    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.delenv("PTA2ICPC_NO_COLOR", raising=False)
    monkeypatch.delenv("TERM", raising=False)
    monkeypatch.setattr("pta2icpc.logging_config.sys.platform", "linux")
    assert _auto_color(_FakeTty()) is True
    assert _auto_color(None) is False
    monkeypatch.setenv("NO_COLOR", "")
    assert _auto_color(_FakeTty()) is True
    monkeypatch.setenv("NO_COLOR", "1")
    assert _auto_color(_FakeTty()) is False
    monkeypatch.delenv("NO_COLOR")
    monkeypatch.setenv("PTA2ICPC_NO_COLOR", "1")
    assert _auto_color(_FakeTty()) is False


def test_auto_color_false_when_not_a_tty(monkeypatch):
    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.delenv("PTA2ICPC_NO_COLOR", raising=False)

    class _NotTty:
        def isatty(self):
            return False

    assert _auto_color(_NotTty()) is False


@pytest.mark.parametrize("name, expected", [
    ("pta2icpc.poller", "poller"),
    ("pta2icpc.pta.client", "pta"),
    ("pta2icpc.logging_config", "logging"),
    ("pta2icpc.__main__", "main"),
    ("pta2icpc.init", "init"),
    ("pta2icpc.balloons", "balloons"),
    ("pta2icpc.store", "store"),
    ("pta2icpc.translate", "translate"),
    ("pta2icpc.printjob", "printjob"),
    ("pta2icpc.config", "config"),
    ("pta2icpc.app", "app"),
    ("pta2icpc.setup", "setup"),
    ("uvicorn", "server"),
    ("uvicorn.error", "server"),
    ("uvicorn.access", "access"),
    ("something.else", "something.else"),
])
def test_short_name_mapping(name, expected):
    assert short_name(name) == expected


def test_file_only_filter_drops_console_only():
    normal = logging.LogRecord("x", logging.INFO, __file__, 1, "m", (), None)
    secret = logging.LogRecord("x", logging.INFO, __file__, 1, "m", (), None)
    secret.console_only = True
    assert _file_only_filter(normal) is True
    assert _file_only_filter(secret) is False


def test_banner_console_only_not_in_file(tmp_path, capsys, clean_root_logging):
    log_file = tmp_path / "pta2icpc.log"
    configure_logging(log_file=log_file, color=True)
    banner("Server View User Info: admin / s3cr3t-pw")
    for handler in _our_handlers():
        handler.flush()
    err = capsys.readouterr().err
    assert "Server View User Info: admin / s3cr3t-pw" in _strip_ansi(err)
    log_text = log_file.read_text(encoding="utf-8")
    assert "s3cr3t-pw" not in log_text
    assert "Server View User Info" not in log_text


def test_writes_record_with_date_level_and_short_name(tmp_path,
                                                      clean_root_logging):
    log_file = tmp_path / "pta2icpc.log"
    configure_logging(log_file=log_file, color=False)
    logging.getLogger("pta2icpc.probe").warning("hello world")
    for handler in _our_handlers():
        handler.flush()
    text = log_file.read_text(encoding="utf-8")
    assert "[W]" in text
    assert re.search(r"\d\d \d\d:\d\d:\d\d\s+probe \[W\] hello world", text)


def test_default_log_file_resolved_from_paths(tmp_path, monkeypatch,
                                              clean_root_logging):
    workdir = tmp_path / "workdir"
    workdir.mkdir()
    target = tmp_path / "probe" / "log.txt"
    monkeypatch.chdir(workdir)
    monkeypatch.setattr(paths, "LOG_FILE", target)
    configure_logging(color=False)
    assert target.exists()
    assert not (workdir / "manual" / "pta2icpc.log").exists()


def test_idempotent_no_stacked_handlers(tmp_path, clean_root_logging):
    log_file = tmp_path / "pta2icpc.log"
    configure_logging(log_file=log_file, color=False)
    configure_logging(log_file=log_file, color=False)
    assert len(_our_handlers()) == 2


def test_configure_does_not_own_uvicorn_loggers(tmp_path, clean_root_logging):
    configure_logging(log_file=tmp_path / "pta2icpc.log", color=False)
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        logger = logging.getLogger(name)
        assert logger.handlers == []
        assert logger.propagate is True


def test_main_disables_uvicorn_config_and_keeps_password_out_of_file(
        monkeypatch, tmp_path, capsys, clean_root_logging):
    from pta2icpc import __main__ as entry
    from pta2icpc.config import Settings

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(entry, "load_settings",
                        lambda: Settings(api_password="s3cr3t-pw"))
    monkeypatch.setattr(entry, "load_connection",
                        lambda: {"contest_id": "1000000000000000000",
                                 "pta_cookie": "cookie"})
    monkeypatch.setattr(entry, "_build_default_app", lambda conn: object())
    captured: dict = {}
    monkeypatch.setattr(entry.uvicorn, "run",
                        lambda app, **kw: captured.update(kw))

    entry.main()

    assert captured.get("log_config") is None
    err = _strip_ansi(capsys.readouterr().err)
    assert "Server View User Info: admin / s3cr3t-pw" in err
    for handler in _our_handlers():
        handler.flush()
    log_text = (tmp_path / "manual" / "pta2icpc.log").read_text(encoding="utf-8")
    assert "s3cr3t-pw" not in log_text


def test_sets_root_level_to_info(tmp_path, clean_root_logging):
    configure_logging(log_file=tmp_path / "pta2icpc.log", color=False)
    assert logging.getLogger().level == logging.INFO


def test_chinese_message_round_trips_utf8(tmp_path, clean_root_logging):
    log_file = tmp_path / "pta2icpc.log"
    configure_logging(log_file=log_file, color=False)
    logging.getLogger("pta2icpc.probe").info("从缓存恢复 42 条提交")
    for handler in _our_handlers():
        handler.flush()
    assert "从缓存恢复 42 条提交" in log_file.read_text(encoding="utf-8")
