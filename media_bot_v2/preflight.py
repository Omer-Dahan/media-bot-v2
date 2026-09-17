"""Cutover readiness checks for media-bot-v2 (see docs/DEPLOY.md).

Every check below is read-only against production, with one explicit
exception: `check_provider_health_table` creates the v2-only
`provider_health` table if it does not exist yet, using `checkfirst=True`
scoped to that single table - it never touches `users`, `settings`,
`payments`, or `video_cache`, the tables shared with the old bot. Nothing
here downloads media, sends Telegram messages, or writes to a Telethon
session file.

Run as:

    uv run python -m media_bot_v2.preflight

Exit code is 0 if every check passed or was skipped, 1 if any check failed.
"""

from __future__ import annotations

import contextlib
import re
import shutil
import subprocess
import sys
import urllib.parse
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import requests
from pydantic import ValidationError
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine import make_url

from media_bot_v2.config import OLD_BOT_SESSION_NAME, Settings, load_settings
from media_bot_v2.db.models import ProviderHealth

Status = Literal["pass", "fail", "skip"]

EXPECTED_LEGACY_TABLES = ("users", "settings", "payments", "video_cache")
_JS_RUNTIME_BINARIES = ("deno", "node", "bun")
_VERSION_TIMEOUT_SECONDS = 10
_HTTP_TIMEOUT_SECONDS = 5.0


@dataclass
class CheckResult:
    name: str
    status: Status
    detail: str


def _mask_url(url_str: str) -> str:
    """Render a URL or DSN with credentials redacted, for safe printing."""
    with contextlib.suppress(Exception):
        url = make_url(url_str)
        return url.render_as_string(hide_password=True)
    with contextlib.suppress(Exception):
        parsed = urllib.parse.urlsplit(url_str)
        if parsed.password:
            user = parsed.username or ""
            port_part = f":{parsed.port}" if parsed.port is not None else ""
            host_part = parsed.hostname or ""
            netloc = f"{user}:***@{host_part}{port_part}"
            return urllib.parse.urlunsplit(
                (parsed.scheme, netloc, parsed.path, parsed.query, parsed.fragment)
            )
    return re.sub(r"://([^:@/\s]*):([^@/\s]+)@", r"://\1:***@", url_str)


def _mask_dsn(dsn: str) -> str:
    """Render a DB DSN with the password redacted, for safe printing."""
    return _mask_url(dsn)


def _extract_credentials(url_str: str) -> list[str]:
    secrets: list[str] = []
    with contextlib.suppress(Exception):
        url = make_url(url_str)
        if url.password:
            secrets.append(url.password)
    with contextlib.suppress(Exception):
        parsed = urllib.parse.urlsplit(url_str)
        if parsed.password and parsed.password not in secrets:
            secrets.append(parsed.password)
    m = re.search(r"://[^:@/\s]*:([^@/\s]+)@", url_str)
    if m and m.group(1) not in secrets:
        secrets.append(m.group(1))
    return secrets


def _sanitize_text(text: str, *source_urls: str | None) -> str:
    """Redact credentials and full URLs from any error or report string."""
    result = text
    for url_str in source_urls:
        if not url_str:
            continue
        if url_str in result:
            result = result.replace(url_str, _mask_url(url_str))
        for secret in _extract_credentials(url_str):
            if secret and secret in result:
                result = result.replace(secret, "***")
    result = re.sub(r"://([^:@/\s]*):([^@/\s]+)@", r"://\1:***@", result)
    return result


def check_database(settings: Settings) -> CheckResult:
    """Connect to the configured DB (no writes) and confirm the legacy
    tables the old bot created are present, so we know we're pointed at
    the real production schema rather than an empty database."""
    masked = _mask_dsn(settings.db_dsn)
    engine = None
    try:
        engine = create_engine(settings.db_dsn)
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
            tables = set(inspect(conn).get_table_names())
    except Exception as exc:  # noqa: BLE001 - any DB driver error is a readiness failure, not a crash
        safe_exc = _sanitize_text(str(exc), settings.db_dsn)
        return CheckResult("database", "fail", f"Could not connect to {masked}: {safe_exc}")
    finally:
        if engine is not None:
            engine.dispose()

    missing = [t for t in EXPECTED_LEGACY_TABLES if t not in tables]
    if missing:
        return CheckResult(
            "database",
            "fail",
            f"Connected to {masked}, but missing expected legacy table(s): "
            f"{', '.join(missing)}. Found: {', '.join(sorted(tables)) or '(none)'}",
        )
    return CheckResult(
        "database",
        "pass",
        f"Connected to {masked}; found legacy tables {', '.join(EXPECTED_LEGACY_TABLES)}",
    )


def check_session_name(settings: Settings) -> CheckResult:
    """Confirm SESSION_NAME does not collide with the old bot's live
    "main" session. Settings already rejects this at load time; this check
    surfaces the same fact in the report and also flags whether local
    session files for either bot are present, for operator visibility."""
    if settings.session_name == OLD_BOT_SESSION_NAME:
        return CheckResult(
            "session_name",
            "fail",
            f"SESSION_NAME={settings.session_name!r} collides with the old bot's session file.",
        )
    detail = f"SESSION_NAME={settings.session_name!r} (old bot uses {OLD_BOT_SESSION_NAME!r})"
    session_file = Path(f"{settings.session_name}.session")
    old_session_file = Path(f"{OLD_BOT_SESSION_NAME}.session")
    if session_file.exists():
        detail += f"; {session_file} already exists locally"
    if old_session_file.exists():
        detail += f"; old bot's {old_session_file} is present alongside it, untouched"
    return CheckResult("session_name", "pass", detail)


def check_js_runtime(settings: Settings) -> CheckResult:
    """yt-dlp needs Deno, Node.js, or Bun in PATH to solve YouTube's
    n-challenge signatures via yt-dlp-ejs."""
    del settings
    for binary in _JS_RUNTIME_BINARIES:
        path = shutil.which(binary)
        if path:
            return CheckResult("js_runtime", "pass", f"Found {binary} at {path}")
    return CheckResult(
        "js_runtime",
        "fail",
        f"No JavaScript runtime found in PATH (checked: {', '.join(_JS_RUNTIME_BINARIES)}). "
        "See docs/DEPLOY.md for install commands.",
    )


def check_yt_dlp_ejs(settings: Settings) -> CheckResult:
    """The yt-dlp-ejs package bundles the JS solver scripts yt-dlp runs
    under the runtime found above; without it yt-dlp falls back to
    fetching solver code remotely on every run."""
    del settings
    try:
        import yt_dlp_ejs  # noqa: F401
    except ImportError as exc:
        return CheckResult("yt_dlp_ejs", "fail", f"yt-dlp-ejs is not importable: {exc}")
    return CheckResult("yt_dlp_ejs", "pass", "yt-dlp-ejs package is importable")


def check_potoken_provider(settings: Settings) -> CheckResult:
    """If POTOKEN_PROVIDER_URL is configured (a self-hosted PO token
    provider such as bgutil-ytdlp-pot-provider, see docs/DEPLOY.md), verify
    its HTTP server actually responds. Skipped when not configured, since a
    static POTOKEN with no provider server is also a valid setup."""
    if not settings.potoken_provider_url:
        return CheckResult(
            "potoken_provider",
            "skip",
            "POTOKEN_PROVIDER_URL is not set; skipping (a static POTOKEN, if set, is not "
            "verified by this check)",
        )
    masked_url = _mask_url(settings.potoken_provider_url)
    url = settings.potoken_provider_url.rstrip("/") + "/ping"
    try:
        response = requests.get(url, timeout=_HTTP_TIMEOUT_SECONDS)
        response.raise_for_status()
    except Exception as exc:  # noqa: BLE001 - any connection/HTTP error means the provider isn't ready
        safe_exc = _sanitize_text(str(exc), settings.potoken_provider_url)
        return CheckResult(
            "potoken_provider",
            "fail",
            f"PO token provider at {masked_url} did not respond: {safe_exc}",
        )
    return CheckResult(
        "potoken_provider",
        "pass",
        f"PO token provider at {masked_url} responded",
    )


def _binary_version(binary: str) -> tuple[bool, str]:
    path = shutil.which(binary)
    if not path:
        return False, f"{binary} not found in PATH"
    try:
        result = subprocess.run(
            [path, "-version"],
            capture_output=True,
            text=True,
            timeout=_VERSION_TIMEOUT_SECONDS,
            check=False,
        )
    except Exception as exc:  # noqa: BLE001 - any launch failure (perms, missing libs) is a readiness failure
        return False, f"{binary} found at {path} but failed to run: {exc}"
    if result.returncode != 0:
        return False, f"{binary} at {path} exited {result.returncode}: {result.stderr.strip()[:200]}"
    output = (result.stdout or result.stderr or "").strip()
    first_line = output.splitlines()[0] if output else "(no version output)"
    return True, f"{path} ({first_line})"


def check_ffmpeg(settings: Settings) -> CheckResult:
    del settings
    ok, detail = _binary_version("ffmpeg")
    return CheckResult("ffmpeg", "pass" if ok else "fail", detail)


def check_ffprobe(settings: Settings) -> CheckResult:
    del settings
    ok, detail = _binary_version("ffprobe")
    return CheckResult("ffprobe", "pass" if ok else "fail", detail)


def check_writable_directories(settings: Settings) -> CheckResult:
    """Prove the download and log directories are actually writable by the
    user this process will run as, rather than assuming from permissions."""
    problems: list[str] = []
    checked = {
        "download_dir": Path(settings.download_dir),
        "log_dir": Path(settings.log_file).parent,
    }
    for label, directory in checked.items():
        try:
            directory.mkdir(parents=True, exist_ok=True)
            probe = directory / ".preflight_write_test"
            probe.write_text("ok")
            probe.unlink()
        except OSError as exc:
            problems.append(f"{label} ({directory}): {exc}")
    if problems:
        return CheckResult("writable_directories", "fail", "; ".join(problems))
    paths = ", ".join(f"{label}={path}" for label, path in checked.items())
    return CheckResult("writable_directories", "pass", f"Writable: {paths}")


def check_provider_health_table(settings: Settings) -> CheckResult:
    """Create the v2-only provider_health table if missing. Scoped to this
    one table (not Base.metadata.create_all) so it can never attempt DDL
    against the shared legacy tables."""
    masked = _mask_dsn(settings.db_dsn)
    engine = None
    try:
        engine = create_engine(settings.db_dsn)
        existed_before = ProviderHealth.__tablename__ in inspect(engine).get_table_names()
        ProviderHealth.__table__.create(bind=engine, checkfirst=True)
    except Exception as exc:  # noqa: BLE001 - any DDL/driver error is a readiness failure, not a crash
        safe_exc = _sanitize_text(str(exc), settings.db_dsn)
        return CheckResult(
            "provider_health_table",
            "fail",
            f"Could not create/verify provider_health on {masked}: {safe_exc}",
        )
    finally:
        if engine is not None:
            engine.dispose()
    if existed_before:
        return CheckResult("provider_health_table", "pass", "provider_health table already exists")
    return CheckResult(
        "provider_health_table", "pass", "provider_health table was missing and has been created"
    )


ALL_CHECKS = (
    check_database,
    check_session_name,
    check_js_runtime,
    check_yt_dlp_ejs,
    check_potoken_provider,
    check_ffmpeg,
    check_ffprobe,
    check_writable_directories,
    check_provider_health_table,
)


def run_all(settings: Settings) -> list[CheckResult]:
    return [check(settings) for check in ALL_CHECKS]


def format_report(results: list[CheckResult]) -> str:
    label = {"pass": "PASS", "fail": "FAIL", "skip": "SKIP"}
    lines = ["media-bot-v2 preflight report", "=" * 30]
    lines.extend(f"[{label[r.status]}] {r.name}: {r.detail}" for r in results)
    failed = [r for r in results if r.status == "fail"]
    lines.append("")
    if failed:
        lines.append(f"{len(failed)} check(s) failed - not ready for cutover.")
    else:
        lines.append("All checks passed (or were intentionally skipped) - ready for cutover.")
    return "\n".join(lines)


def format_config_error(exc: Exception) -> str:
    """Format a configuration error into a human-readable report without tracebacks."""
    problems: list[str] = []
    if isinstance(exc, ValidationError):
        for error in exc.errors():
            loc = ".".join(str(part) for part in error.get("loc", ())) or "settings"
            msg = error.get("msg", "Invalid value")
            if msg.startswith("Value error, "):
                msg = msg.removeprefix("Value error, ")
            problems.append(f"{loc}: {msg}")
    else:
        problems.append(str(exc))

    formatted_problems = "\n".join(f"  - {p}" for p in problems)
    detail = (
        f"Missing or invalid configuration:\n{formatted_problems}\n\n"
        "Set the required variables in .env (see .env.example) or the environment."
    )
    result = CheckResult("configuration", "fail", detail)
    return format_report([result])


def main() -> int:
    try:
        settings = load_settings()
    except Exception as exc:  # noqa: BLE001 - user-facing CLI error, not an unhandled crash
        print(format_config_error(exc))
        return 1

    results = run_all(settings)
    print(format_report(results))
    return 1 if any(r.status == "fail" for r in results) else 0


if __name__ == "__main__":
    sys.exit(main())
