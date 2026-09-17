"""Settings toggle logic, ported from the old bot's toggle_*_callback
handlers (src/main.py:903-1000). No Telethon events involved - apply_toggle
and get_or_create_user are plain functions over a SQLAlchemy session."""

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from media_bot_v2.db.models import Base, Setting
from media_bot_v2.telegram import settings_menu


@pytest.fixture
def session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)
    with factory() as s:
        yield s


def test_get_or_create_user_creates_user_with_free_download_credits(session):
    user = settings_menu.get_or_create_user(
        session, 1, first_name="Agy", username="agy", free_download=3
    )
    session.commit()
    assert user.free == 3
    assert user.paid == 0
    assert user.settings is not None
    assert user.settings.quality == "high"


def test_get_or_create_user_is_idempotent_and_updates_name(session):
    settings_menu.get_or_create_user(session, 1, first_name="Old", username="old", free_download=3)
    session.commit()
    user = settings_menu.get_or_create_user(session, 1, first_name="New", username="new", free_download=3)
    session.commit()
    assert user.first_name == "New"
    assert user.free == 3  # untouched on second call


def test_toggle_quality_cycles_high_medium_low_high():
    setting = Setting(quality="high", format="video", subtitles=0, title_length=500)
    assert "720p" in settings_menu.apply_toggle(setting, settings_menu.TOGGLE_QUALITY)
    assert setting.quality == "medium"
    assert "480p" in settings_menu.apply_toggle(setting, settings_menu.TOGGLE_QUALITY)
    assert setting.quality == "low"
    assert "1080p" in settings_menu.apply_toggle(setting, settings_menu.TOGGLE_QUALITY)
    assert setting.quality == "high"


def test_toggle_format_flips_video_and_document():
    setting = Setting(quality="high", format="video", subtitles=0, title_length=500)
    settings_menu.apply_toggle(setting, settings_menu.TOGGLE_FORMAT)
    assert setting.format == "document"
    settings_menu.apply_toggle(setting, settings_menu.TOGGLE_FORMAT)
    assert setting.format == "video"


def test_toggle_subtitles_flips_on_and_off():
    setting = Setting(quality="high", format="video", subtitles=0, title_length=500)
    settings_menu.apply_toggle(setting, settings_menu.TOGGLE_SUBTITLES)
    assert setting.subtitles == 1
    settings_menu.apply_toggle(setting, settings_menu.TOGGLE_SUBTITLES)
    assert setting.subtitles == 0


def test_toggle_title_len_cycles_through_all_values():
    setting = Setting(quality="high", format="video", subtitles=0, title_length=100)
    expected = [250, 500, 1000, 4000, 0, 100]
    for value in expected:
        settings_menu.apply_toggle(setting, settings_menu.TOGGLE_TITLE_LEN)
        assert setting.title_length == value


def test_build_settings_buttons_reflects_current_values():
    setting = Setting(quality="medium", format="document", subtitles=1, title_length=0)
    rows = settings_menu.build_settings_buttons(setting)
    texts = [button.text for row in rows for button in row]
    assert any("720p" in t for t in texts)
    assert any("קובץ" in t for t in texts)
    assert any("פעיל" in t for t in texts)
    assert any("ללא הגבלה" in t for t in texts)
