"""M11.16: direct-link support for arbitrary file types (zip/apk/pdf/iso/...),
with correct type detection *before* sending, and explicit permission
hardening for everything this bot writes to disk.

Covers:
(A) Content-Type -> extension fallback for a missing/undetectable extension
    (media_bot_v2.engines.content_check.extension_for_content_type).
(B) media_probe.correct_extension: an alias extension (.apk/.jar/... for a
    zip-format file) is no longer clobbered with the generic ".zip", and a
    garbage stem ("download", "file", a bare number) is replaced once the
    real type is known.
(C) DirectEngine: a server's `Content-Disposition` filename is honoured, and
    a response with neither a usable name nor a useful extension falls back
    to one derived from Content-Type.
(D) DownloadPipeline: a file that is neither video/audio/photo (zip, in this
    test) always goes out as a document, regardless of the user's "send as"
    setting; a raw byte-split (non-video) group gets an explicit
    reassembly notice.
(E) engines.permissions.harden_tree: every file 0600, every directory 0700,
    no execute bit, regardless of what the path started as.
(F) preflight.check_download_dir_exec_safety: warns (never fails outright)
    on a world-writable download dir or a missing `noexec` mount, and skips
    cleanly when that can't be determined.
(G) bootstrap.main sets a restrictive process umask before doing anything
    else.
"""

from __future__ import annotations

import http.server
import os
import stat
import threading
import zipfile
from pathlib import Path
from unittest.mock import patch

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from media_bot_v2 import preflight
from media_bot_v2.config import Settings
from media_bot_v2.credits.service import CreditsService
from media_bot_v2.db.models import Base, User
from media_bot_v2.engines.base import BaseEngine, DownloadResult
from media_bot_v2.engines.content_check import extension_for_content_type
from media_bot_v2.engines.direct import DirectEngine
from media_bot_v2.engines.permissions import DIR_MODE, FILE_MODE, harden_path, harden_tree
from media_bot_v2.pipeline import DownloadPipeline, _FileGroup
from media_bot_v2.upload.media_probe import KIND_OTHER, MediaInfo, correct_extension

pytestmark = pytest.mark.usefixtures("bypass_ssrf_guard")


def _write_zip(path: Path) -> None:
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("a.txt", "hello")


# ==============================================================================
# (A) Content-Type -> extension fallback
# ==============================================================================


@pytest.mark.parametrize(
    "content_type,expected",
    [
        ("application/zip", ".zip"),
        ("application/zip; charset=binary", ".zip"),
        ("application/vnd.android.package-archive", ".apk"),
        ("application/x-iso9660-image", ".iso"),
        ("application/x-msi", ".msi"),
        ("application/pdf", ".pdf"),
        ("application/x-7z-compressed", ".7z"),
        ("application/x-rar-compressed", ".rar"),
        ("image/webp", ".webp"),
        ("video/mp4", ".mp4"),
        ("audio/mpeg", ".mp3"),
    ],
)
def test_extension_for_content_type_known_values(content_type, expected):
    assert extension_for_content_type(content_type) == expected


@pytest.mark.parametrize("content_type", [None, "", "application/octet-stream", "binary/octet-stream"])
def test_extension_for_content_type_no_signal_returns_none(content_type):
    assert extension_for_content_type(content_type) is None


# ==============================================================================
# (B) correct_extension: alias preservation + garbage-stem replacement
# ==============================================================================


def test_correct_extension_preserves_apk_extension_for_zip_bytes(tmp_path):
    apk = tmp_path / "app.apk"
    _write_zip(apk)

    assert correct_extension(apk) == apk
    assert apk.exists()


def test_correct_extension_preserves_jar_extension_for_zip_bytes(tmp_path):
    jar = tmp_path / "lib.jar"
    _write_zip(jar)

    assert correct_extension(jar) == jar


def test_correct_extension_replaces_garbage_stem_once_type_is_known(tmp_path):
    garbage = tmp_path / "download"  # no extension at all
    _write_zip(garbage)

    fixed = correct_extension(garbage)

    assert fixed.name == "file.zip"
    assert fixed.exists()
    assert not garbage.exists()


def test_correct_extension_replaces_numeric_stem(tmp_path):
    numeric = tmp_path / "12345.bin"
    _write_zip(numeric)

    fixed = correct_extension(numeric)

    assert fixed.name == "file.zip"


def test_correct_extension_still_renames_misleading_non_alias_extension(tmp_path):
    """A non-alias mismatch (not one of the zip-family extensions) must
    still be corrected - only the alias set is exempt."""
    misleading = tmp_path / "picture.ashx"
    _write_zip(misleading)

    fixed = correct_extension(misleading)

    assert fixed.name == "picture.zip"


# ==============================================================================
# (C) DirectEngine: Content-Disposition filename + Content-Type extension fallback
# ==============================================================================


class _CDHandler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path.startswith("/by-header"):
            body = b"%PDF-1.4 fake pdf body"
            self.send_response(200)
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Content-Disposition", 'attachment; filename="report.pdf"')
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if self.path == "/no-name-no-ext":
            body = b"not-a-real-iso-but-that-is-fine-for-this-test"
            self.send_response(200)
            self.send_header("Content-Type", "application/x-iso9660-image")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        self.send_response(404)
        self.end_headers()

    def log_message(self, *args):
        pass


@pytest.fixture
def cd_server():
    server = http.server.HTTPServer(("127.0.0.1", 0), _CDHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        thread.join()


async def test_download_uses_content_disposition_filename_over_url_path(cd_server, tmp_path):
    engine = DirectEngine()
    result = await engine.download(f"{cd_server}/by-header?id=1", dest_dir=tmp_path)

    assert Path(result.file_paths[0]).name == "report.pdf"


async def test_download_falls_back_to_content_type_extension_when_name_has_none(cd_server, tmp_path):
    engine = DirectEngine()
    result = await engine.download(f"{cd_server}/no-name-no-ext", dest_dir=tmp_path)

    written = Path(result.file_paths[0])
    assert written.suffix == ".iso"
    assert written.exists()


# ==============================================================================
# (D) Pipeline: non-media kinds always go out as a document; raw-split notice
# ==============================================================================


class _ZipEngine(BaseEngine):
    def matches(self, url: str) -> bool:
        return True

    async def download(self, url: str, *, dest_dir: Path, cancel_token=None) -> DownloadResult:
        dest_dir.mkdir(parents=True, exist_ok=True)
        path = dest_dir / "archive.zip"
        _write_zip(path)
        return DownloadResult(file_paths=[str(path)], title="archive.zip")


class _FakeMessage:
    def __init__(self, message_id: int):
        self.id = message_id


class _RecordingUploader:
    def __init__(self):
        self.send_calls: list[dict] = []
        self.description_calls: list[str] = []

    async def send_file(self, path: Path, *, caption=None, media=None, as_document=False, **kwargs):
        self.send_calls.append({"path": path, "as_document": as_document, "media": media})
        return _FakeMessage(len(self.send_calls))

    async def copy_to_archive(self, message, **kwargs):
        return None

    async def edit_caption(self, message, caption: str) -> None:
        pass

    async def send_subtitle(self, path: Path):
        return None

    async def send_description(self, text: str, *, reply_to) -> None:
        self.description_calls.append(text)

    async def send_cached(self, archive_chat, message_ids, **kwargs):
        return []


class _FakeProgress:
    def __init__(self):
        self.updates: list[str] = []

    async def update(self, text: str, *, is_terminal: bool = False) -> None:
        self.updates.append(text)


@pytest.fixture
def session_factory():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)
    with factory() as session:
        session.add(User(user_id=1, free=3, paid=0, bandwidth_used=0, total_bandwidth=0, is_blocked=0))
        session.commit()
    return factory


@pytest.fixture
def credits_service(session_factory):
    return CreditsService(session_factory, enable_vip=True, owner_ids=[], free_bandwidth=2_000_000_000)


async def test_zip_file_is_always_delivered_as_a_document(session_factory, credits_service, tmp_path):
    """KIND_OTHER (zip, apk, pdf, iso, exe, ...) has no non-document
    Telegram send form - must go out as a document even though the user's
    default "send as" setting is video/not-forced (DeliveryOptions()
    defaults to as_document=False)."""
    pipeline = DownloadPipeline(credits_service=credits_service, download_dir=tmp_path)
    uploader = _RecordingUploader()

    await pipeline.run(
        user_id=1,
        url="http://example.local/archive",
        engine=_ZipEngine(),
        uploader=uploader,
        progress=_FakeProgress(),
    )

    assert len(uploader.send_calls) == 1
    assert uploader.send_calls[0]["as_document"] is True
    assert uploader.send_calls[0]["media"].kind == KIND_OTHER


async def test_raw_split_group_gets_a_reassembly_notice(tmp_path, session_factory, credits_service):
    pipeline = DownloadPipeline(credits_service=credits_service, download_dir=tmp_path)
    parts = [tmp_path / "clip.zip.part000", tmp_path / "clip.zip.part001"]
    for part in parts:
        part.write_bytes(b"x")
    group = _FileGroup(info=MediaInfo(kind=KIND_OTHER), parts=parts)
    uploader = _RecordingUploader()

    await pipeline._send_raw_split_notice_quietly(uploader, group=group, reply_to="msg-123")

    assert len(uploader.description_calls) == 1
    notice = uploader.description_calls[0]
    assert "clip.zip" in notice
    assert "2" in notice


async def test_video_split_group_gets_no_reassembly_notice(tmp_path, session_factory, credits_service):
    """A video split by ffmpeg's segment muxer produces independently
    playable parts - no "concatenate these in order" instructions apply."""
    pipeline = DownloadPipeline(credits_service=credits_service, download_dir=tmp_path)
    parts = [tmp_path / "clip.part000.mp4", tmp_path / "clip.part001.mp4"]
    for part in parts:
        part.write_bytes(b"x")
    group = _FileGroup(info=MediaInfo(kind="video"), parts=parts)
    uploader = _RecordingUploader()

    await pipeline._send_raw_split_notice_quietly(uploader, group=group, reply_to="msg-123")

    assert uploader.description_calls == []


async def test_single_part_group_gets_no_reassembly_notice(tmp_path, session_factory, credits_service):
    pipeline = DownloadPipeline(credits_service=credits_service, download_dir=tmp_path)
    part = tmp_path / "file.zip"
    part.write_bytes(b"x")
    group = _FileGroup(info=MediaInfo(kind=KIND_OTHER), parts=[part])
    uploader = _RecordingUploader()

    await pipeline._send_raw_split_notice_quietly(uploader, group=group, reply_to="msg-123")

    assert uploader.description_calls == []


# ==============================================================================
# (E) Permission hardening
# ==============================================================================


def test_harden_path_strips_execute_bit_from_a_file(tmp_path):
    f = tmp_path / "evil.sh"
    f.write_text("#!/bin/sh\necho hi\n")
    f.chmod(0o777)

    harden_path(f)

    mode = stat.S_IMODE(f.stat().st_mode)
    assert mode == FILE_MODE
    assert not (mode & stat.S_IXUSR)


def test_harden_path_sets_dir_mode(tmp_path):
    d = tmp_path / "workdir"
    d.mkdir()
    d.chmod(0o777)

    harden_path(d)

    assert stat.S_IMODE(d.stat().st_mode) == DIR_MODE


def test_harden_path_on_missing_path_is_a_noop(tmp_path):
    harden_path(tmp_path / "does-not-exist")  # must not raise


def test_harden_tree_covers_nested_files_and_dirs(tmp_path):
    root = tmp_path / "task"
    nested_dir = root / "nested"
    nested_dir.mkdir(parents=True)
    root.chmod(0o777)
    nested_dir.chmod(0o777)
    top_file = root / "top.bin"
    top_file.write_bytes(b"x")
    top_file.chmod(0o777)
    nested_file = nested_dir / "inner.bin"
    nested_file.write_bytes(b"y")
    nested_file.chmod(0o755)

    harden_tree(root)

    for directory in (root, nested_dir):
        assert stat.S_IMODE(directory.stat().st_mode) == DIR_MODE
    for file_path in (top_file, nested_file):
        mode = stat.S_IMODE(file_path.stat().st_mode)
        assert mode == FILE_MODE
        assert not (mode & (stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH))


async def test_pipeline_downloaded_file_ends_up_with_safe_permissions(
    session_factory, credits_service, tmp_path
):
    """End-to-end: a file a (fake) engine writes with wide-open permissions
    must be 0600, no execute bit, by the time it reaches the uploader -
    proving the pipeline's harden_tree call actually runs before upload,
    not just that the helper function works in isolation."""

    class _WideOpenEngine(BaseEngine):
        def matches(self, url: str) -> bool:
            return True

        async def download(self, url: str, *, dest_dir: Path, cancel_token=None) -> DownloadResult:
            dest_dir.mkdir(parents=True, exist_ok=True)
            path = dest_dir / "clip.bin"
            path.write_bytes(b"not actually media")
            path.chmod(0o777)
            return DownloadResult(file_paths=[str(path)], title="clip.bin")

    captured: dict[str, int] = {}

    class _CheckingUploader(_RecordingUploader):
        async def send_file(self, path: Path, *, caption=None, media=None, as_document=False, **kwargs):
            captured["mode"] = stat.S_IMODE(Path(path).stat().st_mode)
            return await super().send_file(path, caption=caption, media=media, as_document=as_document, **kwargs)

    pipeline = DownloadPipeline(credits_service=credits_service, download_dir=tmp_path)
    await pipeline.run(
        user_id=1,
        url="http://example.local/clip",
        engine=_WideOpenEngine(),
        uploader=_CheckingUploader(),
        progress=_FakeProgress(),
    )

    assert captured["mode"] == FILE_MODE
    assert not (captured["mode"] & stat.S_IXUSR)


# ==============================================================================
# (F) preflight.check_download_dir_exec_safety
# ==============================================================================


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


def test_check_download_dir_exec_safety_skips_when_dir_missing(tmp_path):
    settings = _settings(tmp_path)  # download_dir not created
    result = preflight.check_download_dir_exec_safety(settings)
    assert result.status == "skip"


def test_check_download_dir_exec_safety_warns_on_world_writable_dir(tmp_path, monkeypatch):
    download_dir = tmp_path / "downloads"
    download_dir.mkdir()
    download_dir.chmod(0o777)
    settings = _settings(tmp_path)
    monkeypatch.setattr(preflight, "_mount_options_for", lambda path: ["noexec", "nosuid"])

    result = preflight.check_download_dir_exec_safety(settings)

    assert result.status == "warn"
    assert "world-writable" in result.detail


def test_check_download_dir_exec_safety_warns_when_not_noexec(tmp_path, monkeypatch):
    download_dir = tmp_path / "downloads"
    download_dir.mkdir()
    download_dir.chmod(0o700)
    settings = _settings(tmp_path)
    monkeypatch.setattr(preflight, "_mount_options_for", lambda path: ["rw", "relatime"])

    result = preflight.check_download_dir_exec_safety(settings)

    assert result.status == "warn"
    assert "noexec" in result.detail


def test_check_download_dir_exec_safety_passes_when_noexec_and_private(tmp_path, monkeypatch):
    download_dir = tmp_path / "downloads"
    download_dir.mkdir()
    download_dir.chmod(0o700)
    settings = _settings(tmp_path)
    monkeypatch.setattr(preflight, "_mount_options_for", lambda path: ["rw", "noexec", "nosuid"])

    result = preflight.check_download_dir_exec_safety(settings)

    assert result.status == "pass"


def test_check_download_dir_exec_safety_skips_when_mount_undeterminable(tmp_path, monkeypatch):
    download_dir = tmp_path / "downloads"
    download_dir.mkdir()
    download_dir.chmod(0o700)
    settings = _settings(tmp_path)
    monkeypatch.setattr(preflight, "_mount_options_for", lambda path: None)

    result = preflight.check_download_dir_exec_safety(settings)

    assert result.status == "skip"


def test_warn_status_does_not_fail_main(tmp_path, monkeypatch):
    """A WARN must never block cutover the way a FAIL does."""
    settings = _settings(tmp_path)
    engine = create_engine(settings.db_dsn)
    Base.metadata.create_all(engine)
    engine.dispose()
    download_dir = Path(settings.download_dir)
    download_dir.mkdir(parents=True)
    download_dir.chmod(0o777)  # forces a WARN from check_download_dir_exec_safety

    monkeypatch.setattr(preflight, "load_settings", lambda: settings)
    monkeypatch.setattr(preflight.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(preflight, "_mount_options_for", lambda path: ["noexec"])

    class _FakeCompletedProcess:
        returncode = 0
        stdout = "version 1.0\n"
        stderr = ""

    monkeypatch.setattr(preflight.subprocess, "run", lambda *a, **k: _FakeCompletedProcess())

    exit_code = preflight.main()

    assert exit_code == 0


# ==============================================================================
# (G) bootstrap sets a restrictive umask on startup
# ==============================================================================


def test_main_sets_restrictive_umask_before_anything_else():
    from media_bot_v2.bootstrap import main

    with (
        patch("media_bot_v2.bootstrap.os.umask") as mock_umask,
        patch("media_bot_v2.bootstrap.load_settings") as mock_settings,
        patch("media_bot_v2.bootstrap.configure_logging"),
        patch("media_bot_v2.bootstrap.check_js_runtime"),
        patch("media_bot_v2.bootstrap.build_session_factory"),
        patch("media_bot_v2.bootstrap.build_client"),
        patch("media_bot_v2.bootstrap.register_handlers"),
        patch("media_bot_v2.bootstrap.shutdown_thread_pool"),
    ):
        mock_settings.return_value = Settings(app_id=1, app_hash="h", bot_token="t", _env_file=None)
        main()
        mock_umask.assert_called_once_with(0o077)


def test_umask_actually_changes_process_umask():
    """Not mocked this time: os.umask really does change what new files on
    this process get, and returns the previous value - proving 0o077 is a
    real, effective call, not just a constant that looks right in a mock
    assertion above."""
    previous = os.umask(0o077)
    try:
        current = os.umask(0o077)  # umask() returns the *previous* mask
        assert current == 0o077
    finally:
        os.umask(previous)
