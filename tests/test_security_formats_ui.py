from __future__ import annotations

import asyncio
import json
import os
import subprocess
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from aiogram.exceptions import TelegramRetryAfter
from aiogram.methods import SendMessage

from admin import db
from admin.auth import has_permission, permission_matrix
from admin.buttons.db import create_button
from admin.buttons.service import build_video_keyboard, ensure_add_to_group_entry, is_valid_button_url
from admin.keyboards import SECTIONS, main_menu_kb
from app.context import AppContext, set_context
from app.i18n import SUPPORTED_LOCALES, TRANSLATIONS
from app.security import UnsafeURLError, is_obviously_private_url, validate_public_url
from app.services.cache import get_content_id, normalized_url
from app.services.cleanup import cleanup_orphan_temp
from app.services import downloader as downloader_module
from app.services.downloader import Downloader, build_format_selector
from app.services.formats import available_qualities, choose_quality
from app.services.storage import StorageService
from app.services.telegram import telegram_retry
from app.services.video_note import VIDEO_NOTE_MAX_DURATION, VideoNoteService, is_video_note_eligible


def test_url_normalization_only_removes_tracking_data():
    first = normalized_url("https://Example.com/watch?v=abc&utm_source=x&list=keep")
    second = normalized_url("https://example.com/watch?v=abc&fbclid=123&list=keep")
    assert first == second
    assert "list=keep" in first and "utm_" not in first
    assert get_content_id(first) == get_content_id(second)
    # Generic-looking parameters may be content-bearing on arbitrary yt-dlp
    # extractors and therefore must not be stripped globally.
    preserved = normalized_url("https://example.com/v?ref=episode&feature=clip&si=part&spm=chapter")
    assert "ref=episode" in preserved and "feature=clip" in preserved
    assert "si=part" in preserved and "spm=chapter" in preserved


@pytest.mark.asyncio
@pytest.mark.parametrize("url", [
    "http://127.0.0.1/admin", "http://10.0.0.5/x", "http://169.254.169.254/latest",
    "http://[::1]/", "http://localhost/x", "file:///etc/passwd",
    "http://user:pass@example.com/x",
])
async def test_ssrf_private_and_credential_urls_are_rejected(url):
    with pytest.raises(UnsafeURLError):
        await validate_public_url(url)


@pytest.mark.asyncio
async def test_extracted_media_hosts_are_dns_validated(monkeypatch):
    async def fake_validate(url):
        if "private.invalid" in url:
            raise UnsafeURLError("private")
        return url

    monkeypatch.setattr(downloader_module, "validate_public_url", fake_validate)
    info = {
        "formats": [
            {"format_id": "public", "url": "https://cdn.example/video"},
            {"format_id": "private", "url": "https://private.invalid/video"},
        ]
    }
    filtered = await Downloader._filter_public_endpoints(info)
    assert [fmt["format_id"] for fmt in filtered["formats"]] == ["public"]


def test_extracted_private_media_endpoint_is_detected():
    assert is_obviously_private_url("http://192.168.1.2/video.mp4")
    assert is_obviously_private_url("file:///etc/passwd")
    assert not is_obviously_private_url("https://cdn.example.com/video.mp4")


def test_quality_selection_available_and_sensible_fallback():
    formats = [
        {"height": 720, "width": 1280, "vcodec": "avc1", "acodec": "none", "filesize": 20_000_000},
        {"height": 1080, "width": 1920, "vcodec": "vp9", "acodec": "none", "filesize": 50_000_000},
        {"height": 2160, "width": 3840, "vcodec": "av01", "acodec": "none", "filesize": 500_000_000},
        {"vcodec": "none", "acodec": "opus", "filesize": 5_000_000},
    ]
    assert available_qualities(formats) == [720, 1080, 2160]
    assert choose_quality(formats, 100_000_000, "auto")[0] == 1080
    assert choose_quality(formats, 100_000_000, 1440)[0] == 1080
    assert choose_quality(formats, 30_000_000, 1080)[0] == 720
    selector = build_format_selector({"width": 3840, "height": 2160}, 2160)
    assert selector.startswith("bestvideo[height>=2160][height<=2160][vcodec^=avc1]")
    assert "bestvideo[height<=2160][vcodec^=avc1]" not in selector


def test_permissions_matrix_and_role_visibility():
    matrix = permission_matrix()
    assert has_permission("owner", "manage_admins")
    assert not has_permission("admin", "manage_admins")
    assert has_permission("moderator", "moderation")
    assert not has_permission("moderator", "broadcast")
    assert has_permission("broadcaster", "broadcast")
    assert not has_permission("broadcaster", "users")
    for role, permissions in matrix.items():
        visible = {button.callback_data for row in main_menu_kb(role).inline_keyboard for button in row}
        expected = {callback for permission, _, callback in SECTIONS if permission in permissions}
        assert visible == expected
        assert all(len(value.encode()) <= 64 for value in visible)


def test_admin_inline_button_url_validation():
    assert is_valid_button_url("https://example.com/path?q=1")
    assert not is_valid_button_url("https://")
    assert not is_valid_button_url("https://example.com/bad path")
    assert not is_valid_button_url("https://user:secret@example.com/")
    assert not is_valid_button_url("javascript:alert(1)")


class FakeBotIdentity:
    async def get_me(self):
        return SimpleNamespace(username="real_bot_name")


@pytest.mark.asyncio
async def test_video_keyboard_combines_system_and_custom_buttons(configured_db):
    ensure_add_to_group_entry()
    create_button("Website", "https://example.com")
    keyboard = await build_video_keyboard(FakeBotIdentity(), locale="en", video_note_token="token123")
    texts = [button.text for row in keyboard.inline_keyboard for button in row]
    callbacks = [button.callback_data for row in keyboard.inline_keyboard for button in row if button.callback_data]
    urls = [button.url for row in keyboard.inline_keyboard for button in row if button.url]
    assert "⭕ Make video note" in texts
    assert "➕ Add bot to group" in texts
    assert "Website" in texts
    assert callbacks == ["vn:token123"]
    assert "https://t.me/real_bot_name?startgroup=true" in urls


def test_no_user_translation_exposes_cache_or_infrastructure():
    prohibited = ("cache", "кэш", "file_id", "yt-dlp", "ffmpeg", "deno", "sqlite", "docker", "storage")
    for locale in SUPPORTED_LOCALES:
        combined = "\n".join(TRANSLATIONS[locale].values()).lower()
        assert not any(term in combined for term in prohibited), locale


def test_moderation_flags_really_persist(configured_db):
    db.upsert_user(99, "blocked", "Blocked", "en")
    db.upsert_group(-99, "group", "Blocked group", None, "en")
    db.set_user_blocked(99, True)
    db.set_group_blocked(-99, True)
    assert db.is_user_blocked(99)
    assert db.is_group_blocked(-99)
    db.set_user_blocked(99, False)
    db.set_group_blocked(-99, False)
    assert not db.is_user_blocked(99)
    assert not db.is_group_blocked(-99)


def test_video_note_eligibility_uses_real_duration_boundary():
    assert is_video_note_eligible(1)
    assert is_video_note_eligible(VIDEO_NOTE_MAX_DURATION)
    assert not is_video_note_eligible(VIDEO_NOTE_MAX_DURATION + 0.01)
    assert not is_video_note_eligible(None)
    assert not is_video_note_eligible(30, enabled=False)


def test_video_note_claim_is_idempotent_and_stale_safe(configured_db):
    bootstrap, runtime = configured_db
    service = VideoNoteService(runtime, Downloader(bootstrap.temp_dir), bootstrap.temp_dir)
    token = service.create_delivery(
        job_id="job", requester_id=1, chat_id=1, content_id="x", source_url="https://example.com/x",
        quality=720, telegram_file_id="FILE", duration=30,
    )
    assert token
    assert service.claim(token) == "claimed"
    assert service.claim(token) == "busy"
    assert service.claim("old-button-token") == "missing"
    assert service.recover_interrupted() == 1
    assert service.claim(token) == "claimed"


@pytest.mark.asyncio
async def test_video_note_ffmpeg_crop_is_square(configured_db, tmp_path):
    bootstrap, runtime = configured_db
    downloader = Downloader(bootstrap.temp_dir)
    service = VideoNoteService(runtime, downloader, bootstrap.temp_dir)
    source = tmp_path / "wide.mp4"
    output = tmp_path / "note.mp4"
    subprocess.run(
        ["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi",
         "-i", "testsrc=size=960x540:rate=15", "-t", "1", "-pix_fmt", "yuv420p", str(source)],
        check=True,
    )
    await service._convert(source, output)
    probe = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=width,height,codec_name", "-of", "json", str(output)],
        check=True, capture_output=True, text=True,
    )
    stream = json.loads(probe.stdout)["streams"][0]
    assert stream["width"] == 640 and stream["height"] == 640
    assert stream["codec_name"] == "h264"


def test_orphan_cleanup_is_scoped_and_age_guarded(configured_db):
    bootstrap, runtime = configured_db
    runtime.set("temp_retention_minutes", 5)
    old = bootstrap.temp_dir / "video_old"
    fresh = bootstrap.temp_dir / "video_fresh"
    unrelated = bootstrap.temp_dir / "keep_me"
    for path in (old, fresh, unrelated):
        path.mkdir()
        (path / "x").write_text("x")
    old_time = time.time() - 600
    os.utime(old, (old_time, old_time))
    assert cleanup_orphan_temp(bootstrap.temp_dir, runtime) == 1
    assert not old.exists() and fresh.exists() and unrelated.exists()


@pytest.mark.asyncio
async def test_telegram_retryafter_is_bounded_and_recovers(monkeypatch):
    calls = 0
    sleeps: list[float] = []
    method = SendMessage(chat_id=1, text="x")

    async def operation():
        nonlocal calls
        calls += 1
        if calls == 1:
            raise TelegramRetryAfter(method=method, message="flood", retry_after=1)
        return "ok"

    async def fake_sleep(value):
        sleeps.append(value)

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)
    assert await telegram_retry(operation) == "ok"
    assert calls == 2 and sleeps == [1.0]
