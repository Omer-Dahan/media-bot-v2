"""Make a downloaded video playable inside Telegram clients.

Telegram's own apps and third-party ones (Plus Messenger's ExoPlayer, ...)
only reliably play an MP4 whose video is 8-bit 4:2:0 H.264, whose audio is AAC
(or MP3) and whose `moov` atom sits before `mdat` so playback can start while
the file is still arriving. yt-dlp happily produces `mp4` files holding AV1 or
VP9 video and Opus audio (that is what `bestvideo+bestaudio` resolves to on
YouTube), and those show "cannot play this video ... ERROR_CODE_IO_UNSPECIFIED"
on such clients even though Telegram accepted the upload.

`ensure_streamable` fixes the file with the cheapest operation that works:

* already fine                      -> untouched
* right codecs, wrong container or
  `moov` at the end                 -> `-c copy -movflags +faststart` (no re-encode)
* anything else                     -> only the offending stream(s) re-encoded
                                       (H.264 / AAC), the other one copied

If the fix cannot be made (no ffmpeg, timeout, ...) the original path is
returned and the caller decides how to send it.
"""

from __future__ import annotations

import json
import logging
import os
import struct
import subprocess
import uuid
from dataclasses import dataclass
from pathlib import Path

import filetype

logger = logging.getLogger(__name__)

_PROBE_TIMEOUT_SECONDS = 60
DEFAULT_FIX_TIMEOUT_SECONDS = 900.0

_MP4_SUFFIXES = {".mp4", ".m4v", ".mov"}
_OK_AUDIO_CODECS = {"aac", "mp3"}


@dataclass(frozen=True)
class StreamProfile:
    """What a file actually contains, as far as playability is concerned."""

    video_codec: str | None
    pix_fmt: str | None
    audio_codec: str | None
    is_mp4_container: bool
    faststart: bool

    @property
    def video_ok(self) -> bool:
        return self.video_codec == "h264" and self.pix_fmt in (None, "yuv420p")

    @property
    def audio_ok(self) -> bool:
        return self.audio_codec is None or self.audio_codec in _OK_AUDIO_CODECS

    @property
    def streamable(self) -> bool:
        return self.video_ok and self.audio_ok and self.is_mp4_container and self.faststart


def moov_before_mdat(path: Path) -> bool:
    """True when the top-level `moov` atom precedes `mdat` (progressive
    playback works). Walks only the top-level atom headers, never the payload.
    A file without a `moov` at all, or one that cannot be read, is False."""
    try:
        with path.open("rb") as fh:
            size_total = os.fstat(fh.fileno()).st_size
            offset = 0
            while offset + 8 <= size_total:
                fh.seek(offset)
                header = fh.read(8)
                if len(header) < 8:
                    return False
                size, kind = struct.unpack(">I4s", header)
                if kind == b"moov":
                    return True
                if kind == b"mdat":
                    return False
                if size == 1:
                    extended = fh.read(8)
                    if len(extended) < 8:
                        return False
                    size = struct.unpack(">Q", extended)[0]
                elif size == 0:
                    return False
                if size < 8:
                    return False
                offset += size
    except OSError:
        return False
    return False


def inspect_streams(path: Path) -> StreamProfile | None:
    """Codec/container profile of `path`; None if ffprobe cannot read it."""
    try:
        result = subprocess.run(
            [
                "ffprobe",
                "-v",
                "error",
                "-show_entries",
                "stream=codec_type,codec_name,pix_fmt:stream_disposition=attached_pic",
                "-of",
                "json",
                str(path),
            ],
            check=True,
            capture_output=True,
            text=True,
            timeout=_PROBE_TIMEOUT_SECONDS,
        )
        streams = json.loads(result.stdout or "{}").get("streams") or []
    except (subprocess.SubprocessError, OSError, ValueError):
        logger.warning("ffprobe failed for %s", path, exc_info=True)
        return None

    video = next(
        (s for s in streams if s.get("codec_type") == "video" and not (s.get("disposition") or {}).get("attached_pic")),
        None,
    )
    audio = next((s for s in streams if s.get("codec_type") == "audio"), None)
    is_mp4 = path.suffix.lower() in _MP4_SUFFIXES
    return StreamProfile(
        video_codec=(video or {}).get("codec_name"),
        pix_fmt=(video or {}).get("pix_fmt"),
        audio_codec=(audio or {}).get("codec_name"),
        is_mp4_container=is_mp4,
        faststart=is_mp4 and moov_before_mdat(path),
    )


def build_fix_command(source: Path, target: Path, profile: StreamProfile) -> list[str]:
    """The ffmpeg command that turns `source` into a streamable MP4 at `target`,
    re-encoding only the streams that need it."""
    cmd = ["ffmpeg", "-y", "-loglevel", "error", "-i", str(source), "-map", "0:v:0", "-map", "0:a:0?"]
    if profile.video_ok:
        cmd += ["-c:v", "copy"]
    else:
        # libx264 refuses odd width/height outright ("width not divisible by
        # 2"); this scale filter rounds both dimensions down to even without
        # visibly cropping anything, so a 343x274 source re-encodes instead
        # of failing and falling back to a non-streamable file.
        cmd += [
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
            "-vf", "scale=trunc(iw/2)*2:trunc(ih/2)*2", "-pix_fmt", "yuv420p",
        ]  # fmt: skip
    if profile.audio_ok:
        cmd += ["-c:a", "copy"]
    else:
        cmd += ["-c:a", "aac", "-b:a", "128k"]
    cmd += ["-sn", "-dn", "-map_metadata", "0", "-movflags", "+faststart", "-f", "mp4", str(target)]
    return cmd


def ensure_streamable(path: Path, *, timeout: float = DEFAULT_FIX_TIMEOUT_SECONDS) -> Path:
    """Return a path to a streamable MP4 version of `path` (which may be `path`
    itself). Never raises: on any failure the original path comes back.

    Callers are expected to route images through media_probe's KIND_PHOTO
    check and never reach this function with one - this is a backstop, not
    the primary guard. ffprobe reports a single-frame WebP/PNG/JPEG as a
    "video" stream too (codec_name=webp/png/mjpeg, no duration), which used
    to send every photo through a doomed H.264 re-encode attempt (production
    incident 2026-10-01: a 343x274 WebP failed with "width not divisible by
    2" and was then sent as a raw, extension-less file with no preview)."""
    guessed = filetype.guess(str(path))
    if guessed is not None and guessed.mime.startswith("image/"):
        logger.debug("%s is an image (%s), not a video - skipping streamable conversion", path.name, guessed.mime)
        return path

    profile = inspect_streams(path)
    if profile is None or profile.video_codec is None or profile.streamable:
        return path

    target = path.parent / f"{path.stem}.{uuid.uuid4().hex[:8]}.streamable.mp4"
    cmd = build_fix_command(path, target, profile)
    logger.info(
        "Making %s streamable (video=%s/%s audio=%s mp4=%s faststart=%s): %s",
        path.name,
        profile.video_codec,
        profile.pix_fmt,
        profile.audio_codec,
        profile.is_mp4_container,
        profile.faststart,
        "re-encode" if not (profile.video_ok and profile.audio_ok) else "remux",
    )
    try:
        subprocess.run(cmd, check=True, capture_output=True, timeout=timeout)
    except subprocess.CalledProcessError as exc:
        logger.warning("Could not make %s streamable (%s), sending it as-is", path.name, _short_ffmpeg_error(exc))
        logger.debug("ffmpeg conversion failure for %s", path, exc_info=True)
        target.unlink(missing_ok=True)
        return path
    except (subprocess.SubprocessError, OSError) as exc:
        logger.warning("Could not make %s streamable (%s), sending it as-is", path.name, exc)
        logger.debug("ffmpeg conversion failure for %s", path, exc_info=True)
        target.unlink(missing_ok=True)
        return path

    final = path.with_suffix(".mp4")
    try:
        os.replace(target, final)
        if final != path:
            path.unlink(missing_ok=True)
    except OSError:
        logger.warning("Could not move %s into place", target, exc_info=True)
        target.unlink(missing_ok=True)
        return path
    return final


def _short_ffmpeg_error(exc: subprocess.CalledProcessError) -> str:
    """The last non-empty line of ffmpeg's stderr - where libx264 and friends
    put the actual reason ("width not divisible by 2 (343x274)"), instead of
    a multi-megabyte wall of per-frame output or a Python traceback."""
    stderr = (exc.stderr or b"").decode("utf-8", "replace").strip()
    for line in reversed(stderr.splitlines()):
        if line.strip():
            return line.strip()
    return f"ffmpeg exited with status {exc.returncode}"


def convert_animated_webp(path: Path, *, timeout: float = DEFAULT_FIX_TIMEOUT_SECONDS) -> Path | None:
    """Transcode an animated WebP into a short, streamable H.264 MP4, named
    after `path`'s own stem (`clip.webp` -> `clip.mp4`) with `path` removed -
    same in-place-replacement contract as `ensure_streamable`. Returns None
    (never raises, and `path` is left untouched) if it cannot be done - some
    ffmpeg builds' bundled WebP decoder does not support the ANIM/ANMF
    chunks at all ("skipping unsupported chunk: ANIM"), in which case the
    caller is expected to fall back to sending the original file as a
    document."""
    target = path.parent / f"{path.stem}.{uuid.uuid4().hex[:8]}.anim.mp4"
    cmd = [
        "ffmpeg", "-y", "-loglevel", "error", "-i", str(path),
        "-c:v", "libx264", "-pix_fmt", "yuv420p",
        "-vf", "scale=trunc(iw/2)*2:trunc(ih/2)*2",
        "-movflags", "+faststart", "-f", "mp4", str(target),
    ]  # fmt: skip
    try:
        subprocess.run(cmd, check=True, capture_output=True, timeout=timeout)
    except subprocess.CalledProcessError as exc:
        logger.warning(
            "Could not convert animated WebP %s to MP4 (%s), sending it as a document",
            path.name,
            _short_ffmpeg_error(exc),
        )
        logger.debug("ffmpeg conversion failure for %s", path, exc_info=True)
        target.unlink(missing_ok=True)
        return None
    except (subprocess.SubprocessError, OSError) as exc:
        logger.warning(
            "Could not convert animated WebP %s to MP4 (%s), sending it as a document", path.name, exc
        )
        logger.debug("ffmpeg conversion failure for %s", path, exc_info=True)
        target.unlink(missing_ok=True)
        return None

    final = path.with_suffix(".mp4")
    try:
        os.replace(target, final)
        if final != path:
            path.unlink(missing_ok=True)
    except OSError:
        logger.warning("Could not move %s into place", target, exc_info=True)
        target.unlink(missing_ok=True)
        return None
    return final
