class CreditsExhaustedException(Exception):
    """Raised when a user has run out of download credits."""


class BandwidthExhaustedException(Exception):
    """Raised when a free user has reached their daily bandwidth limit."""
