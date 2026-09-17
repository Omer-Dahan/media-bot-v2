class CreditsExhaustedException(Exception):
    """Raised when a user has run out of download credits."""


class BandwidthExhaustedException(Exception):
    """Raised when a free user has reached their daily bandwidth limit."""


class UserBlockedException(Exception):
    """Raised when a blocked user attempts a download.

    Old bot raised a bare Exception here (model.py:244-245); this rewrite
    gives it a dedicated type so callers can catch it distinctly from other
    quota failures.
    """
