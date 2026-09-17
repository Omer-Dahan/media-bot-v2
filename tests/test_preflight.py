"""Preflight checks, all offline: sqlite instead of MySQL, a local HTTP
server instead of a real PO token provider, and monkeypatched binaries
instead of real ffmpeg/deno - never touches production or the network."""

import http.server
import sys
import threading
import types
from pathlib import Path

from sqlalchemy import create_engine

from media_bot_v2 import preflight
from media_bot_v2.config import Settings
from media_bot_v2.db.models import Base, ProviderHealth


def _settings(tmp_path: Path, **overrides) -> Settings:
    kwargs = {
        "app_id": 1,
        "app_hash": "h",
        "bot_token": "t",
        "db_dsn": f"sqlite:///{tmp_path / 'test.sqlite3'}",
        "download_dir": str(tmp_path / "downloads"),
        "log_file": str(tmp_path / "logs" / "bot.log"),
    }
    kwargs.update(overrides)
    return Settings(_env_file=None, **kwargs)


def test_check_database_passes_when_legacy_tables_present(tmp_path):
    settings = _settings(tmp_path)
    engine = create_engine(settings.db_dsn)
    Base.metadata.create_all(engine)
    engine.dispose()

    result = preflight.check_database(settings)
    assert result.status == "pass"
    assert "users" in result.detail


def test_check_database_fails_when_tables_missing(tmp_path):
    settings = _settings(tmp_path)  # sqlite file doesn't exist yet -> no tables

    result = preflight.check_database(settings)
    assert result.status == "fail"
    assert "missing expected legacy table" in result.detail


def test_check_database_masks_password_in_report():
    settings = types.SimpleNamespace(db_dsn="mysql+pymysql://user:secret@localhost/dbname")
    result = preflight.check_database(settings)
    assert result.status == "fail"  # no real MySQL server here
    assert "secret" not in result.detail
    assert "***" in result.detail


def test_check_session_name_passes_for_v2():
    settings = types.SimpleNamespace(session_name="v2")
    result = preflight.check_session_name(settings)
    assert result.status == "pass"


def test_check_session_name_fails_for_old_bot_name():
    """Settings itself already rejects SESSION_NAME=main at load time; this
    exercises check_session_name's own independent guard against a
    duck-typed settings-like object that bypassed that validator."""
    settings = types.SimpleNamespace(session_name="main")
    result = preflight.check_session_name(settings)
    assert result.status == "fail"


def test_check_js_runtime_passes_when_binary_found(monkeypatch):
    monkeypatch.setattr(preflight.shutil, "which", lambda name: "/usr/bin/node" if name == "node" else None)
    result = preflight.check_js_runtime(None)
    assert result.status == "pass"
    assert "node" in result.detail


def test_check_js_runtime_fails_when_nothing_found(monkeypatch):
    monkeypatch.setattr(preflight.shutil, "which", lambda name: None)
    result = preflight.check_js_runtime(None)
    assert result.status == "fail"


def test_check_yt_dlp_ejs_passes_when_importable():
    result = preflight.check_yt_dlp_ejs(None)
    assert result.status == "pass"


def test_check_yt_dlp_ejs_fails_when_not_importable(monkeypatch):
    monkeypatch.setitem(sys.modules, "yt_dlp_ejs", None)
    result = preflight.check_yt_dlp_ejs(None)
    assert result.status == "fail"


def test_check_potoken_provider_skips_when_not_configured():
    settings = types.SimpleNamespace(potoken_provider_url=None)
    result = preflight.check_potoken_provider(settings)
    assert result.status == "skip"


def test_check_potoken_provider_passes_when_server_responds():
    class _Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.end_headers()

        def log_message(self, *args):
            pass

    server = http.server.HTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        settings = types.SimpleNamespace(
            potoken_provider_url=f"http://127.0.0.1:{server.server_port}"
        )
        result = preflight.check_potoken_provider(settings)
    finally:
        server.shutdown()
        thread.join()
    assert result.status == "pass"


def test_check_potoken_provider_fails_when_unreachable():
    settings = types.SimpleNamespace(potoken_provider_url="http://127.0.0.1:1")
    result = preflight.check_potoken_provider(settings)
    assert result.status == "fail"


def test_check_ffmpeg_fails_when_missing(monkeypatch):
    monkeypatch.setattr(preflight.shutil, "which", lambda name: None)
    result = preflight.check_ffmpeg(None)
    assert result.status == "fail"


def test_check_ffprobe_passes_when_binary_runs(monkeypatch):
    monkeypatch.setattr(preflight.shutil, "which", lambda name: "/usr/bin/ffprobe")

    class _FakeCompletedProcess:
        returncode = 0
        stdout = "ffprobe version 7.1\n"
        stderr = ""

    monkeypatch.setattr(preflight.subprocess, "run", lambda *a, **k: _FakeCompletedProcess())
    result = preflight.check_ffprobe(None)
    assert result.status == "pass"
    assert "7.1" in result.detail


def test_check_writable_directories_passes(tmp_path):
    settings = _settings(tmp_path)
    result = preflight.check_writable_directories(settings)
    assert result.status == "pass"
    assert (tmp_path / "downloads").is_dir()


def test_check_writable_directories_fails_on_permission_error(tmp_path):
    readonly_parent = tmp_path / "readonly"
    readonly_parent.mkdir()
    readonly_parent.chmod(0o500)
    settings = _settings(
        tmp_path,
        download_dir=str(readonly_parent / "downloads"),
        log_file=str(tmp_path / "logs" / "bot.log"),
    )
    try:
        result = preflight.check_writable_directories(settings)
    finally:
        readonly_parent.chmod(0o700)
    assert result.status == "fail"


def test_check_provider_health_table_creates_missing_table(tmp_path):
    settings = _settings(tmp_path)
    engine = create_engine(settings.db_dsn)
    Base.metadata.tables[ProviderHealth.__tablename__].drop(engine, checkfirst=True)
    # Create the other legacy tables but not provider_health, proving this
    # check only touches its own table.
    for table in Base.metadata.tables.values():
        if table.name != ProviderHealth.__tablename__:
            table.create(engine, checkfirst=True)
    engine.dispose()

    result = preflight.check_provider_health_table(settings)
    assert result.status == "pass"
    assert "was missing and has been created" in result.detail

    engine = create_engine(settings.db_dsn)
    from sqlalchemy import inspect as sa_inspect

    assert ProviderHealth.__tablename__ in sa_inspect(engine).get_table_names()
    engine.dispose()


def test_check_provider_health_table_reports_existing(tmp_path):
    settings = _settings(tmp_path)
    engine = create_engine(settings.db_dsn)
    Base.metadata.create_all(engine)
    engine.dispose()

    result = preflight.check_provider_health_table(settings)
    assert result.status == "pass"
    assert "already exists" in result.detail


def test_run_all_and_format_report(tmp_path):
    settings = _settings(tmp_path)
    engine = create_engine(settings.db_dsn)
    Base.metadata.create_all(engine)
    engine.dispose()

    results = preflight.run_all(settings)
    names = {r.name for r in results}
    assert names == {c.__name__.removeprefix("check_") for c in preflight.ALL_CHECKS}

    report = preflight.format_report(results)
    assert "media-bot-v2 preflight report" in report


def test_main_returns_nonzero_exit_code_on_failure(tmp_path, monkeypatch, capsys):
    settings = _settings(tmp_path)  # no tables -> database check fails
    monkeypatch.setattr(preflight, "load_settings", lambda: settings)

    exit_code = preflight.main()

    assert exit_code == 1
    assert "FAIL" in capsys.readouterr().out


def test_main_returns_zero_exit_code_when_all_pass(tmp_path, monkeypatch, capsys):
    settings = _settings(tmp_path)
    engine = create_engine(settings.db_dsn)
    Base.metadata.create_all(engine)
    engine.dispose()
    monkeypatch.setattr(preflight, "load_settings", lambda: settings)
    monkeypatch.setattr(preflight.shutil, "which", lambda name: f"/usr/bin/{name}")

    class _FakeCompletedProcess:
        returncode = 0
        stdout = "version 1.0\n"
        stderr = ""

    monkeypatch.setattr(preflight.subprocess, "run", lambda *a, **k: _FakeCompletedProcess())

    exit_code = preflight.main()

    assert exit_code == 0
    assert "FAIL" not in capsys.readouterr().out
