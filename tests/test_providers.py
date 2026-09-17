"""Unit tests for external extraction providers with mocked HTTP requests.

Guaranteed zero network calls - all external API responses use recorded fixtures.
"""

from unittest.mock import MagicMock, patch

import pytest

from media_bot_v2.providers.base import ProviderFetchError, ProviderUnavailableError
from media_bot_v2.providers.cobalt import CobaltProvider
from media_bot_v2.providers.musicaldown import MusicalDownProvider
from media_bot_v2.providers.tikdownloader import TikDownloaderProvider
from media_bot_v2.providers.tikwm import TikWMProvider
from media_bot_v2.providers.ytmp3 import YTmp3Provider

# ---------------------------------------------------------------------------
# TikWM tests
# ---------------------------------------------------------------------------


async def test_tikwm_parses_video_response_correctly():
    provider = TikWMProvider()
    mock_payload = {
        "code": 0,
        "msg": "success",
        "data": {
            "id": "7123456789",
            "title": "Funny Cat Video",
            "play": "https://www.tikwm.com/video/media/play/123.mp4",
            "hdplay": "https://www.tikwm.com/video/media/hdplay/123.mp4",
            "size": 1048576,
            "hd_size": 2097152,
        },
    }

    mock_resp = MagicMock()
    mock_resp.json.return_value = mock_payload
    mock_resp.raise_for_status.return_value = None

    with patch("media_bot_v2.providers.tikwm.requests.get", return_value=mock_resp) as mock_get:
        result = await provider.fetch("https://www.tiktok.com/@user/video/7123456789")

    mock_get.assert_called_once()
    assert result.provider == "tikwm"
    assert result.media_type == "video"
    assert result.primary_url == "https://www.tikwm.com/video/media/hdplay/123.mp4"
    assert result.title == "Funny Cat Video"
    assert result.size_hint == 2097152


async def test_tikwm_parses_photo_slideshow_correctly():
    provider = TikWMProvider()
    mock_payload = {
        "code": 0,
        "msg": "success",
        "data": {
            "id": "7123456789",
            "title": "Photo Slideshow",
            "images": [
                "https://p16-sign.tiktokcdn.com/obj/photo1.jpg",
                "https://p16-sign.tiktokcdn.com/obj/photo2.jpg",
            ],
        },
    }

    mock_resp = MagicMock()
    mock_resp.json.return_value = mock_payload
    mock_resp.raise_for_status.return_value = None

    with patch("media_bot_v2.providers.tikwm.requests.get", return_value=mock_resp):
        result = await provider.fetch("https://www.tiktok.com/@user/video/7123456789")

    assert result.provider == "tikwm"
    assert result.media_type == "photo"
    assert len(result.media_urls) == 2
    assert result.media_urls[0] == "https://p16-sign.tiktokcdn.com/obj/photo1.jpg"
    assert result.title == "Photo Slideshow"


async def test_tikwm_raises_fetch_error_on_non_zero_code():
    provider = TikWMProvider()
    mock_payload = {"code": -1, "msg": "Video not found"}

    mock_resp = MagicMock()
    mock_resp.json.return_value = mock_payload
    mock_resp.raise_for_status.return_value = None

    with (
        patch("media_bot_v2.providers.tikwm.requests.get", return_value=mock_resp),
        pytest.raises(ProviderFetchError, match="TikWM API error: Video not found"),
    ):
        await provider.fetch("https://www.tiktok.com/@user/video/7123456789")


# ---------------------------------------------------------------------------
# tikdownloader tests
# ---------------------------------------------------------------------------


async def test_tikdownloader_parses_snapcdn_jwt_links():
    provider = TikDownloaderProvider()
    mock_html = """
    <div class="video-info">
      <h3 class="title">My Awesome TikTok</h3>
      <div class="download-links">
        <a href="https://dl.snapcdn.app/get?token=jwt_watermark" class="btn">Download with Watermark</a>
        <a href="https://dl.snapcdn.app/get?token=jwt_hd" class="btn">Download MP4 HD</a>
      </div>
    </div>
    """
    mock_resp = MagicMock()
    mock_resp.json.return_value = {"status": "ok", "data": mock_html}
    mock_resp.raise_for_status.return_value = None

    with patch("media_bot_v2.providers.tikdownloader.requests.post", return_value=mock_resp) as mock_post:
        result = await provider.fetch("https://www.tiktok.com/@user/video/123")

    mock_post.assert_called_once()
    assert result.provider == "tikdownloader"
    assert result.media_type == "video"
    assert result.primary_url == "https://dl.snapcdn.app/get?token=jwt_hd"
    assert result.title == "My Awesome TikTok"


async def test_tikdownloader_raises_on_status_error():
    provider = TikDownloaderProvider()
    mock_resp = MagicMock()
    mock_resp.json.return_value = {"status": "error"}
    mock_resp.raise_for_status.return_value = None

    with (
        patch("media_bot_v2.providers.tikdownloader.requests.post", return_value=mock_resp),
        pytest.raises(ProviderFetchError, match="tikdownloader returned error status"),
    ):
        await provider.fetch("https://www.tiktok.com/@user/video/123")


# ---------------------------------------------------------------------------
# musicaldown tests
# ---------------------------------------------------------------------------


async def test_musicaldown_handles_dynamic_form_and_returns_fastdl_link():
    provider = MusicalDownProvider()

    # GET response containing dynamically named fields and hidden token
    mock_page_html = """
    <form action="/download" method="post">
      <input type="hidden" name="token_xyz_987" value="secret_token_123">
      <input type="hidden" name="verify" value="1">
      <input type="text" name="random_url_field_456" placeholder="Paste link here">
      <button type="submit">Download</button>
    </form>
    """

    # POST response containing download links
    mock_download_html = """
    <div class="card">
      <h2 class="video-desc">Cool Dance Trend</h2>
      <a href="https://fastdl.muscdn.app/file/12345/video.mp4" class="btn">Download MP4 Now</a>
    </div>
    """

    get_resp = MagicMock()
    get_resp.text = mock_page_html
    get_resp.raise_for_status.return_value = None

    post_resp = MagicMock()
    post_resp.text = mock_download_html
    post_resp.raise_for_status.return_value = None

    mock_session = MagicMock()
    mock_session.get.return_value = get_resp
    mock_session.post.return_value = post_resp

    with patch("media_bot_v2.providers.musicaldown.requests.Session", return_value=mock_session):
        result = await provider.fetch("https://www.tiktok.com/@user/video/999")

    # Verify POST received the parsed hidden token and target URL in dynamic field
    _, kwargs = mock_session.post.call_args
    assert kwargs["data"]["token_xyz_987"] == "secret_token_123"
    assert kwargs["data"]["random_url_field_456"] == "https://www.tiktok.com/@user/video/999"

    assert result.provider == "musicaldown"
    assert result.primary_url == "https://fastdl.muscdn.app/file/12345/video.mp4"
    assert result.title == "Cool Dance Trend"


async def test_musicaldown_raises_if_dynamic_field_missing():
    provider = MusicalDownProvider()
    mock_page_html = "<form action='/download'></form>"

    get_resp = MagicMock()
    get_resp.text = mock_page_html
    get_resp.raise_for_status.return_value = None

    mock_session = MagicMock()
    mock_session.get.return_value = get_resp

    with (
        patch("media_bot_v2.providers.musicaldown.requests.Session", return_value=mock_session),
        pytest.raises(ProviderFetchError, match="failed to locate dynamic URL input"),
    ):
        await provider.fetch("https://www.tiktok.com/@user/video/999")


# ---------------------------------------------------------------------------
# ytmp3 tests
# ---------------------------------------------------------------------------


async def test_ytmp3_four_step_conversion_flow():
    provider = YTmp3Provider(api_key="test_api_key")

    auth_resp = MagicMock()
    auth_resp.json.return_value = {"error": 0, "key": "bearer_token_abc"}
    auth_resp.raise_for_status.return_value = None

    init_resp = MagicMock()
    init_resp.json.return_value = {"error": 0, "convertURL": "https://gamma.gammacloud.net/api/v1/convert?id=1"}
    init_resp.raise_for_status.return_value = None

    conv_resp = MagicMock()
    conv_resp.json.return_value = {
        "error": 0,
        "title": "Acoustic Guitar Track",
        "downloadURL": "https://gamma.gammacloud.net/dl/file?token=123",
    }
    conv_resp.raise_for_status.return_value = None

    def fake_get(url, *args, **kwargs):
        if "/api/v1/auth" in url:
            return auth_resp
        if "/api/v1/init" in url:
            return init_resp
        if "/api/v1/convert" in url:
            return conv_resp
        raise AssertionError(f"Unexpected URL called: {url}")

    with patch("media_bot_v2.providers.ytmp3.requests.get", side_effect=fake_get):
        result = await provider.fetch("https://www.youtube.com/watch?v=dQw4w9WgXcQ")

    assert result.provider == "ytmp3"
    assert result.media_type == "video"
    assert "gamma.gammacloud.net/dl/file" in result.primary_url
    assert "v=dQw4w9WgXcQ" in result.primary_url
    assert "r=ytmp3.gl" in result.primary_url
    assert result.title == "Acoustic Guitar Track"
    assert result.headers.get("Referer") == "https://ytmp3.gl/"


async def test_ytmp3_refuses_copyright_commercial_music():
    provider = YTmp3Provider()

    auth_resp = MagicMock()
    auth_resp.json.return_value = {"error": 0, "key": "bearer_token_abc"}
    auth_resp.raise_for_status.return_value = None

    init_resp = MagicMock()
    init_resp.json.return_value = {"error": 0, "convertURL": "https://gamma.gammacloud.net/api/v1/convert?id=1"}
    init_resp.raise_for_status.return_value = None

    # Error code 215 or 403 signifies copyright/commercial content rejection
    conv_resp = MagicMock()
    conv_resp.json.return_value = {"error": 215}
    conv_resp.raise_for_status.return_value = None

    def fake_get(url, *args, **kwargs):
        if "/api/v1/auth" in url:
            return auth_resp
        if "/api/v1/init" in url:
            return init_resp
        if "/api/v1/convert" in url:
            return conv_resp
        raise AssertionError(f"Unexpected URL: {url}")

    with (
        patch("media_bot_v2.providers.ytmp3.requests.get", side_effect=fake_get),
        pytest.raises(ProviderFetchError, match="commercial music or copyright restriction"),
    ):
        await provider.fetch("https://www.youtube.com/watch?v=dQw4w9WgXcQ")


# ---------------------------------------------------------------------------
# Cobalt tests
# ---------------------------------------------------------------------------


async def test_cobalt_unconfigured_raises_unavailable():
    provider = CobaltProvider(instance_url=None)
    assert not provider.is_configured
    assert not provider.matches("https://www.youtube.com/watch?v=dQw4w9WgXcQ")

    with pytest.raises(ProviderUnavailableError, match="Cobalt instance URL is not configured"):
        await provider.fetch("https://www.youtube.com/watch?v=dQw4w9WgXcQ")


async def test_cobalt_parses_tunnel_status():
    provider = CobaltProvider(instance_url="https://cobalt.internal")
    assert provider.is_configured
    assert provider.matches("https://www.youtube.com/watch?v=dQw4w9WgXcQ")

    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {
        "status": "tunnel",
        "url": "https://cobalt.internal/tunnel/123.mp4",
        "filename": "YouTube Video.mp4",
    }
    mock_resp.raise_for_status.return_value = None

    with patch("media_bot_v2.providers.cobalt.requests.post", return_value=mock_resp):
        result = await provider.fetch("https://www.youtube.com/watch?v=dQw4w9WgXcQ")

    assert result.provider == "cobalt"
    assert result.primary_url == "https://cobalt.internal/tunnel/123.mp4"
    assert result.title == "YouTube Video.mp4"


async def test_cobalt_parses_picker_status():
    provider = CobaltProvider(instance_url="https://cobalt.internal")

    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {
        "status": "picker",
        "picker": [
            {"type": "photo", "url": "https://cobalt.internal/photo1.jpg"},
            {"type": "photo", "url": "https://cobalt.internal/photo2.jpg"},
        ],
    }
    mock_resp.raise_for_status.return_value = None

    with patch("media_bot_v2.providers.cobalt.requests.post", return_value=mock_resp):
        result = await provider.fetch("https://www.tiktok.com/@user/video/123")

    assert result.provider == "cobalt"
    assert result.media_type == "photo"
    assert len(result.media_urls) == 2


async def test_cobalt_raises_on_error_status():
    provider = CobaltProvider(instance_url="https://cobalt.internal")

    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {
        "status": "error",
        "error": {"code": "error.api.turnstile_failed"},
    }
    mock_resp.raise_for_status.return_value = None

    with (
        patch("media_bot_v2.providers.cobalt.requests.post", return_value=mock_resp),
        pytest.raises(ProviderFetchError, match="Cobalt instance error: error.api.turnstile_failed"),
    ):
        await provider.fetch("https://www.tiktok.com/@user/video/123")
