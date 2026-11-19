from datetime import datetime


class CreditsExhaustedException(Exception):
    """Raised when a user has run out of download credits."""

    def __init__(
        self,
        message: str,
        *,
        used_bytes: int | None = None,
        quota_bytes: int | None = None,
        next_reset_at: datetime | None = None,
    ) -> None:
        super().__init__(message)
        self.used_bytes = used_bytes
        self.quota_bytes = quota_bytes
        self.next_reset_at = next_reset_at


class BandwidthExhaustedException(Exception):
    """Raised when a free user has reached their weekly bandwidth limit."""


class UserBlockedException(Exception):
    """Raised when a blocked user attempts a download.

    Old bot raised a bare Exception here (model.py:244-245); this rewrite
    gives it a dedicated type so callers can catch it distinctly from other
    quota failures.
    """
