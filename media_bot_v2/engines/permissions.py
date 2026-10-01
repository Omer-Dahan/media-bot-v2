"""Filesystem permission hardening for files this process writes to disk.

Defense-in-depth alongside the process-wide `umask(0o077)` set at startup
(see `bootstrap.main`): umask only caps the *requested* mode a creator asks
for (`requested_mode & ~umask`), so a tool that explicitly requests a wide
mode (e.g. `0o755`) still keeps its owner-execute bit even under a strict
umask (`0o755 & ~0o077 == 0o700`). Every path `harden_tree` touches is
forced to a fixed, non-executable mode instead, regardless of what created
it or what mode it originally asked for - the real guarantee that a
downloaded, converted, or split file on disk can never carry an execute bit.
"""

from __future__ import annotations

import os
from pathlib import Path

FILE_MODE = 0o600
DIR_MODE = 0o700


def harden_path(path: Path) -> None:
    """chmod a single file or directory to its safe default mode. A path
    that no longer exists (already cleaned up, or a race with a concurrent
    unlink) is a no-op, not an error - nothing is left to secure."""
    try:
        mode = DIR_MODE if path.is_dir() else FILE_MODE
        os.chmod(path, mode)
    except OSError:
        pass


def harden_tree(root: Path) -> None:
    """Force every directory under `root` (root included) to `DIR_MODE` and
    every file to `FILE_MODE`, regardless of how it was created - covers
    bytes written directly by this project's own code, and everything a
    subprocess (ffmpeg/ffprobe) or a third-party library (yt-dlp,
    gallery-dl, instaloader) wrote into the same task directory."""
    if not root.exists():
        return
    harden_path(root)
    for dirpath, dirnames, filenames in os.walk(root):
        current = Path(dirpath)
        for name in dirnames:
            harden_path(current / name)
        for name in filenames:
            harden_path(current / name)
