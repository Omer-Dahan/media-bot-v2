"""Per-request delivery options, built from the user's saved settings.

The router reads the `Setting` row once per request and hands this immutable
snapshot to the pipeline, so a settings change made mid-download cannot
change how the file that is already being delivered gets sent.
"""

from __future__ import annotations

from dataclasses import dataclass

from media_bot_v2.db.models import Setting

SEND_AS_VIDEO = "video"
SEND_AS_DOCUMENT = "document"

# Setting.quality ("high"/"medium"/"low") -> the height used by the quality menu.
QUALITY_HEIGHT = {"high": "1080", "medium": "720", "low": "480"}


@dataclass(frozen=True)
class DeliveryOptions:
    send_as: str = SEND_AS_VIDEO
    subtitles: bool = False
    title_length: int = 500
    user_display: str = ""
    default_quality: str = "1080"

    @classmethod
    def from_setting(
        cls, setting: Setting | None, *, user_display: str = ""
    ) -> DeliveryOptions:
        if setting is None:
            return cls(user_display=user_display)
        return cls(
            send_as=SEND_AS_DOCUMENT if setting.format == SEND_AS_DOCUMENT else SEND_AS_VIDEO,
            subtitles=bool(setting.subtitles),
            title_length=int(setting.title_length) if setting.title_length is not None else 500,
            user_display=user_display,
            default_quality=QUALITY_HEIGHT.get(setting.quality, "1080"),
        )

    @property
    def as_document(self) -> bool:
        return self.send_as == SEND_AS_DOCUMENT
