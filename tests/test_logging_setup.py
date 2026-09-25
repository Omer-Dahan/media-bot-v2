"""configure_logging: stdout handler, idempotency, level handling."""

import io
import json
import logging
import logging.handlers

import pytest

from media_bot_v2 import logging_setup
from media_bot_v2.logging_setup import JsonFormatter, configure_logging


@pytest.fixture(autouse=True)
def _restore_root():
    root = logging.getLogger()
    saved_handlers, saved_level = root.handlers[:], root.level
    saved_levels = {n: logging.getLogger(n).level for n in logging_setup._NOISY_LOGGERS}
    yield
    for h in root.handlers[:]:
        root.removeHandler(h)
        h.close()
    root.handlers[:] = saved_handlers
    root.setLevel(saved_level)
    for n, lvl in saved_levels.items():
        logging.getLogger(n).setLevel(lvl)


def _owned(root):
    return [h for h in root.handlers if getattr(h, logging_setup._OWNED_ATTR, False)]


def _stdout_handlers(root):
    return [
        h for h in _owned(root)
        if type(h) is logging.StreamHandler and getattr(h.stream, "write", None)
    ]


def test_adds_stdout_handler_with_file_format_and_level(tmp_path, capsys):
    root = logging.getLogger()
    before = len(root.handlers)
    configure_logging(str(tmp_path / "bot.log"), 1024, 1)
    assert len(root.handlers) == before + 2

    console = _stdout_handlers(root)[0]
    file_handler = next(h for h in _owned(root) if isinstance(h, logging.handlers.RotatingFileHandler))
    assert isinstance(console.formatter, JsonFormatter)
    assert type(console.formatter) is type(file_handler.formatter)
    assert console.level == file_handler.level == logging.INFO

    logging.getLogger("media_bot_v2.test").info("hello")
    out = capsys.readouterr().out
    assert "\x1b[" not in out
    assert json.loads(out.strip().splitlines()[-1])["message"] == "hello"
    assert "hello" in (tmp_path / "bot.log").read_text()


def test_repeat_call_does_not_duplicate_handlers_or_lines(tmp_path, capsys):
    root = logging.getLogger()
    configure_logging(str(tmp_path / "bot.log"), 1024, 1)
    count = len(root.handlers)
    configure_logging(str(tmp_path / "bot.log"), 1024, 1)
    assert len(root.handlers) == count

    logging.getLogger("media_bot_v2.test").info("once")
    assert capsys.readouterr().out.count('"message": "once"') == 1
    assert (tmp_path / "bot.log").read_text().count('"message": "once"') == 1


def test_console_can_be_disabled(tmp_path):
    root = logging.getLogger()
    before = len(root.handlers)
    configure_logging(str(tmp_path / "bot.log"), 1024, 1, log_to_console=False)
    assert len(root.handlers) == before + 1
    assert not _stdout_handlers(root)


def test_third_party_quiet_by_default_verbose_on_debug(tmp_path):
    configure_logging(str(tmp_path / "bot.log"), 1024, 1)
    assert logging.getLogger("telethon").getEffectiveLevel() == logging.WARNING
    assert logging.getLogger("media_bot_v2.x").getEffectiveLevel() == logging.INFO

    configure_logging(str(tmp_path / "bot.log"), 1024, 1, level=logging.DEBUG)
    assert logging.getLogger("telethon").getEffectiveLevel() == logging.DEBUG


def test_stdout_without_reconfigure_is_tolerated(tmp_path, monkeypatch):
    monkeypatch.setattr(logging_setup.sys, "stdout", io.StringIO())
    configure_logging(str(tmp_path / "bot.log"), 1024, 1)
    logging.getLogger("media_bot_v2.test").info("x")
