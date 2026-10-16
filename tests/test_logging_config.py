import logging
import re
from logging.handlers import RotatingFileHandler

import pytest

from pta2icpc import paths
from pta2icpc.logging_config import (DATE_FORMAT, LOG_FILE_BACKUPS,
                                     LOG_FILE_MAX_BYTES, LOG_FORMAT,
                                     configure_logging)


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


def test_installs_one_stream_and_one_rotating_file(tmp_path,
                                                   clean_root_logging):
    configure_logging(log_file=tmp_path / "pta2icpc.log")
    ours = _our_handlers()
    streams = [h for h in ours
               if isinstance(h, logging.StreamHandler)
               and not isinstance(h, RotatingFileHandler)]
    rotators = [h for h in ours if isinstance(h, RotatingFileHandler)]
    assert len(streams) == 1
    assert len(rotators) == 1
    assert rotators[0].maxBytes == LOG_FILE_MAX_BYTES == 5 * 1024 * 1024
    assert rotators[0].backupCount == LOG_FILE_BACKUPS == 3
    for handler in ours:
        assert handler.formatter._fmt == LOG_FORMAT
        assert handler.formatter.datefmt == DATE_FORMAT


def test_writes_record_with_date_level_and_source(tmp_path, clean_root_logging):
    log_file = tmp_path / "pta2icpc.log"
    configure_logging(log_file=log_file)
    logging.getLogger("pta2icpc.probe").warning("hello world")
    for handler in _our_handlers():
        handler.flush()
    text = log_file.read_text(encoding="utf-8")
    assert "WARNING" in text
    assert "pta2icpc.probe: hello world" in text
    assert re.search(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}", text)


def test_default_log_file_resolved_from_paths(tmp_path, monkeypatch,
                                              clean_root_logging):
    workdir = tmp_path / "workdir"
    workdir.mkdir()
    target = tmp_path / "probe" / "log.txt"
    monkeypatch.chdir(workdir)
    monkeypatch.setattr(paths, "LOG_FILE", target)
    configure_logging()
    assert target.exists()
    assert not (workdir / "manual" / "pta2icpc.log").exists()


def test_idempotent_no_stacked_handlers(tmp_path, clean_root_logging):
    log_file = tmp_path / "pta2icpc.log"
    configure_logging(log_file=log_file)
    configure_logging(log_file=log_file)
    assert len(_our_handlers()) == 2


def test_configure_does_not_own_uvicorn_loggers(tmp_path, clean_root_logging):
    configure_logging(log_file=tmp_path / "pta2icpc.log")
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
    assert "Server View User Info: admin / s3cr3t-pw" in capsys.readouterr().out
    for handler in _our_handlers():
        handler.flush()
    log_text = (tmp_path / "manual" / "pta2icpc.log").read_text(encoding="utf-8")
    assert "s3cr3t-pw" not in log_text


def test_sets_root_level_to_info(tmp_path, clean_root_logging):
    configure_logging(log_file=tmp_path / "pta2icpc.log")
    assert logging.getLogger().level == logging.INFO


def test_chinese_message_round_trips_utf8(tmp_path, clean_root_logging):
    log_file = tmp_path / "pta2icpc.log"
    configure_logging(log_file=log_file)
    logging.getLogger("pta2icpc.probe").info("从缓存恢复 42 条提交")
    for handler in _our_handlers():
        handler.flush()
    assert "从缓存恢复 42 条提交" in log_file.read_text(encoding="utf-8")
