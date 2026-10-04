from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError, TelegramRetryAfter
from aiogram.methods import SendMessage, SendVideo
from aiogram.types import FSInputFile

from admin import db
from admin.broadcast import db as broadcast_db
from admin.broadcast import sender as broadcast_sender
from admin.handlers import AdminFilter, _callback_permission
from admin.keyboards import SECTIONS
from app.context import AppContext, set_context
from app.handlers import links as links_handler
from app.services.downloader import DownloadResult
from app.services.jobs import create_job, get_job
from app.services.queue import QueueItem, download_queue
from app.services.storage import CacheWriteResult
from worker import worker


class FakeStatus:
    def __init__(self, message_id=10):
        self.message_id = message_id
        self.texts: list[str] = []
        self.deleted = False

    async def edit_text(self, text, **kwargs):
        self.texts.append(text)
        return self

    async def delete(self):
        self.deleted = True


class FakeMessage:
    def __init__(self, user_id=100, chat_id=100, chat_type="private", text="https://example.com/video"):
        self.from_user = SimpleNamespace(id=user_id, language_code="en", username="new", first_name="New")
        self.chat = SimpleNamespace(id=chat_id, type=chat_type)
        self.text = text
        self.answers: list[tuple[str, object]] = []

    async def answer(self, text, reply_markup=None, **kwargs):
        self.answers.append((text, reply_markup))
        return FakeStatus(100 + len(self.answers))


class FakeDownloader:
    def __init__(self, temp_dir: Path):
        self.temp_dir = temp_dir
        self.download_calls = 0

    async def extract_info(self, url):
        return {
            "id": "abc", "extractor_key": "Example", "title": "Clean title",
            "duration": 20, "width": 1280, "height": 720,
            "formats": [
                {"height": 720, "width": 1280, "vcodec": "avc1", "acodec": "none", "filesize": 1000},
                {"vcodec": "none", "acodec": "aac", "filesize": 100},
            ],
        }

    async def download(self, url, quality, max_filesize, **kwargs):
        self.download_calls += 1
        directory = self.temp_dir / f"video_fake_{self.download_calls}"
        directory.mkdir()
        path = directory / "video.mp4"
        path.write_bytes(b"media")
        return DownloadResult(str(path), "Clean title", quality, 5, 20, "Example", 5, 0)

    def cleanup(self, path):
        path = Path(path)
        path.unlink(missing_ok=True)
        path.parent.rmdir()


class FakeVideoNotes:
    def create_delivery(self, **kwargs):
        return None


class FakeStorage:
    def __init__(self, entries=None, stored_file_id=None):
        self.entries = list(entries or [])
        self.stored_file_id = stored_file_id
        self.deleted: list[int] = []
        self.store_calls = 0

    def cache_entries(self, content_id, quality):
        return self.entries

    def touch_entry(self, entry_id):
        pass

    def delete_permanently_stale(self, entry_id, reason):
        self.deleted.append(entry_id)

    async def store(self, bot, result, **kwargs):
        self.store_calls += 1
        if self.stored_file_id:
            self.entries.append({
                "id": 99,
                "telegram_file_id": self.stored_file_id,
                "duration": result.duration,
                "title": result.title,
                "filesize": result.filesize,
            })
            return CacheWriteResult(self.stored_file_id, 99, 1)
        return None


class FakeBot:
    def __init__(self, dead_file_id: str | None = None, temporary_file_id: str | None = None):
        self.dead_file_id = dead_file_id
        self.temporary_file_id = temporary_file_id
        self.sent_videos: list[object] = []
        self.statuses: list[FakeStatus] = []

    async def send_message(self, chat_id, text, **kwargs):
        status = FakeStatus()
        self.statuses.append(status)
        return status

    async def send_video(self, chat_id, video, **kwargs):
        if video == self.dead_file_id:
            raise TelegramBadRequest(method=SendVideo(chat_id=chat_id, video=video), message="wrong file identifier")
        if video == self.temporary_file_id:
            raise TelegramBadRequest(method=SendVideo(chat_id=chat_id, video=video), message="temporary upstream response")
        self.sent_videos.append(video)
        return SimpleNamespace(video=SimpleNamespace(file_id="DIRECT_FILE"), message_id=1)

    async def get_me(self):
        return SimpleNamespace(username="bot")


def setup_context(configured_db, storage):
    bootstrap, runtime = configured_db
    downloader = FakeDownloader(bootstrap.temp_dir)
    context = AppContext(bootstrap, runtime, downloader, storage, FakeVideoNotes())
    context.polling = True
    set_context(context)
    download_queue.configure(runtime)
    return context, downloader


@pytest.mark.asyncio
async def test_brand_new_user_can_enqueue_url_without_settings(configured_db, monkeypatch):
    context, _ = setup_context(configured_db, FakeStorage())
    db.upsert_user(100, "new", "New", "en-US")

    async def safe(url):
        return url

    monkeypatch.setattr(links_handler, "validate_public_url", safe)
    message = FakeMessage()
    before = download_queue.queued_count
    await links_handler.handle_video_links(message)
    assert download_queue.queued_count == before + 1
    queued = list(download_queue._queued_by_id.values())[-1]
    assert queued.quality == "auto"
    assert message.answers[0][0] == "⏳ Processing video…"
    assert await download_queue.cancel(queued.job_id, 100) == "queued"


@pytest.mark.asyncio
async def test_all_cache_storage_down_still_delivers_directly(configured_db, monkeypatch):
    storage = FakeStorage(stored_file_id=None)
    context, downloader = setup_context(configured_db, storage)
    db.upsert_user(101, "user", "User", "en")
    job_id = create_job(101, 101, "https://example.com/video", "auto")
    item = QueueItem(job_id, 101, 101, "https://example.com/video", "auto", "en")
    bot = FakeBot()

    async def no_keyboard(*args, **kwargs):
        return None

    monkeypatch.setattr(worker, "build_video_keyboard", no_keyboard)
    await worker.process_download(bot, item)
    assert get_job(job_id)["status"] == "success"
    assert downloader.download_calls == 1 and storage.store_calls == 1
    assert len(bot.sent_videos) == 1 and isinstance(bot.sent_videos[0], FSInputFile)


@pytest.mark.asyncio
async def test_dead_cache_is_deleted_redownloaded_and_replaced(configured_db, monkeypatch):
    entry = {"id": 7, "telegram_file_id": "DEAD", "duration": 20, "title": "Old", "filesize": 10}
    storage = FakeStorage([entry], stored_file_id="NEW_VALID")
    _, downloader = setup_context(configured_db, storage)
    db.upsert_user(102, "user", "User", "en")
    job_id = create_job(102, 102, "https://example.com/video", "1080")
    item = QueueItem(job_id, 102, 102, "https://example.com/video", "1080", "en")
    bot = FakeBot(dead_file_id="DEAD")

    async def no_keyboard(*args, **kwargs):
        return None

    monkeypatch.setattr(worker, "build_video_keyboard", no_keyboard)
    await worker.process_download(bot, item)
    assert storage.deleted == [7]
    assert downloader.download_calls == 1
    assert bot.sent_videos == ["NEW_VALID"]
    assert get_job(job_id)["status"] == "success"


@pytest.mark.asyncio
async def test_fresh_storage_reference_failure_falls_back_to_direct(configured_db, monkeypatch):
    storage = FakeStorage(stored_file_id="FRESH_DEAD")
    _, downloader = setup_context(configured_db, storage)
    db.upsert_user(104, "user", "User", "en")
    job_id = create_job(104, 104, "https://example.com/video", "720")
    item = QueueItem(job_id, 104, 104, "https://example.com/video", "720", "en")
    bot = FakeBot(dead_file_id="FRESH_DEAD")

    async def no_keyboard(*args, **kwargs):
        return None

    monkeypatch.setattr(worker, "build_video_keyboard", no_keyboard)
    await worker.process_download(bot, item)
    assert storage.deleted == [99]
    assert downloader.download_calls == 1
    assert len(bot.sent_videos) == 1 and isinstance(bot.sent_videos[0], FSInputFile)
    assert get_job(job_id)["status"] == "success"


@pytest.mark.asyncio
async def test_temporary_cache_failure_keeps_record(configured_db, monkeypatch):
    entry = {"id": 8, "telegram_file_id": "TEMP", "duration": 20, "title": "Old", "filesize": 10}
    storage = FakeStorage([entry], stored_file_id=None)
    _, downloader = setup_context(configured_db, storage)
    db.upsert_user(103, "user", "User", "en")
    job_id = create_job(103, 103, "https://example.com/video", "720")
    item = QueueItem(job_id, 103, 103, "https://example.com/video", "720", "en")
    bot = FakeBot(temporary_file_id="TEMP")

    async def no_keyboard(*args, **kwargs):
        return None

    monkeypatch.setattr(worker, "build_video_keyboard", no_keyboard)
    await worker.process_download(bot, item)
    assert storage.deleted == []
    assert downloader.download_calls == 1
    assert get_job(job_id)["status"] == "success"


@pytest.mark.asyncio
async def test_forged_admin_callback_is_rejected(configured_db):
    db.upsert_user(500, "ordinary", "Ordinary", "en")
    event = SimpleNamespace(from_user=SimpleNamespace(id=500))
    bot = SimpleNamespace()
    assert not await AdminFilter()(event, bot)
    assert _callback_permission("admin:storage") == "storage"
    assert _callback_permission("admin:confirm:admin_delete:1") == "manage_admins"


def test_every_visible_main_admin_section_has_implemented_handler():
    from admin.handlers import admins, broadcast, buttons, dashboard, groups, operations, storage, users

    handlers = {
        "admin:dashboard": dashboard.show_dashboard,
        "admin:downloads:all": operations.downloads_screen,
        "admin:users:0": users.show_users_list,
        "admin:groups:0": groups.show_groups_list,
        "admin:moderation": operations.moderation_screen,
        "admin:storage": storage.storage_screen,
        "admin:broadcast": broadcast.show_broadcast_menu,
        "admin:buttons": buttons.show_buttons_list,
        "admin:settings": operations.runtime_settings_screen,
        "admin:system": operations.system_health,
        "admin:log:0": operations.audit_log,
        "admin:admins": admins.admins_screen,
    }
    assert {callback for _, _, callback in SECTIONS} == set(handlers)
    assert all(callable(handler) for handler in handlers.values())

    preview = broadcast._preview_kb("pending")
    assert preview.inline_keyboard[0][0].callback_data == "bc:launch:pending"


def test_every_nested_admin_callback_contract_has_a_handler():
    from admin import handlers as handler_root
    from admin.handlers import admins, broadcast, buttons, dashboard, groups, operations, storage, users

    contracts = {
        "admin:main": dashboard.show_main_menu,
        "admin:fsm:cancel": handler_root.cancel_admin_fsm_callback,
        "adm:view:": admins.admin_card,
        "adm:add": admins.admin_add_start,
        "adm:create:": admins.admin_create,
        "adm:role:": admins.admin_change_role,
        "adm:toggle:": admins.admin_toggle,
        "adm:delete:": admins.admin_delete_confirm,
        "admin:confirm:admin_delete:": admins.admin_delete,
        "bc:list:": broadcast.show_broadcast_list,
        "bc:view:": broadcast.view_broadcast,
        "bc:new": broadcast.start_new_broadcast,
        "bc:audience:": broadcast.choose_audience,
        "bc:skip_buttons": broadcast.skip_buttons,
        "bc:launch:pending": broadcast.launch_broadcast,
        "bc:pause:": broadcast.pause_broadcast_handler,
        "bc:resume:": broadcast.resume_broadcast_handler,
        "bc:cancel:": broadcast.cancel_broadcast_handler,
        "admin:confirm:broadcast_cancel:": broadcast.confirm_cancel_broadcast,
        "bc:retry:": broadcast.retry_broadcast_handler,
        "btn:view:": buttons.view_button,
        "btn:add": buttons.start_add_button,
        "btn:edit_text:": buttons.start_edit_text,
        "btn:edit_url:": buttons.start_edit_url,
        "btn:toggle:": buttons.toggle_button,
        "btn:move:": buttons.move_button_handler,
        "btn:delete:": buttons.confirm_delete_button,
        "admin:confirm:button_delete:": buttons.do_delete_button,
        "admin:groups:card:": groups.show_group_card,
        "admin:groups:block:": groups.confirm_block_group,
        "admin:confirm:block_group:": groups.do_block_group,
        "admin:groups:unblock:": groups.do_unblock_group,
        "admin:groups:message:": groups.ask_group_message_text,
        "admin:groups:search": groups.start_group_search,
        "admin:groups:quality:": groups.group_quality_menu,
        "admin:groups:setquality:": groups.group_quality_set,
        "admin:downloads:maintenance": operations.toggle_maintenance,
        "admin:downloads:active": operations.active_downloads_screen,
        "jobadmin:view:": operations.active_job_card,
        "jobadmin:cancel:": operations.admin_cancel_job,
        "mod:users/groups:": operations.blocked_list,
        "mod:search": operations.moderation_search_start,
        "aset:view:": operations.runtime_setting_card,
        "aset:toggle:": operations.runtime_setting_toggle,
        "aset:edit:": operations.runtime_setting_edit_start,
        "admin:system:refresh": operations.system_health,
        "admin:log:": operations.audit_log,
        "stg:view:": storage.storage_card,
        "stg:add": storage.storage_add_choose_tier,
        "stg:addtier:": storage.storage_add_choose_role,
        "stg:addrole:": storage.storage_add_wait,
        "stg:replace:": storage.storage_replace_wait,
        "stg:cancel": storage.storage_cancel,
        "stg:test:": storage.storage_test,
        "stg:toggle:": storage.storage_toggle,
        "stg:role:": storage.storage_role,
        "stg:delete:": storage.storage_delete_confirm,
        "admin:confirm:storage_delete:": storage.storage_delete,
        "stg:stats": storage.storage_stats,
        "admin:users:card:": users.show_user_card,
        "admin:users:block:": users.confirm_block_user,
        "admin:confirm:block_user:": users.do_block_user,
        "admin:users:unblock:": users.do_unblock_user,
        "admin:users:message:": users.ask_message_text,
        "admin:users:search": users.start_user_search,
    }
    assert all(callable(handler) for handler in contracts.values())


@pytest.mark.asyncio
async def test_broadcast_retryafter_recovers_without_touching_download_queue(configured_db, monkeypatch):
    broadcast_id = broadcast_db.create_broadcast(1, {"audience": "all"}, {"type": "text", "text": "Hello"})
    broadcast_db.add_targets(broadcast_id, [700])
    target = dict(broadcast_db.get_pending_targets(broadcast_id, 1)[0])
    queued_before = download_queue.queued_count
    sleeps: list[float] = []

    class FloodOnceBot:
        def __init__(self):
            self.calls = 0

        async def send_message(self, chat_id, text, **kwargs):
            self.calls += 1
            if self.calls == 1:
                raise TelegramRetryAfter(
                    method=SendMessage(chat_id=chat_id, text=text),
                    message="flood",
                    retry_after=1,
                )

    async def fake_sleep(value):
        sleeps.append(value)

    monkeypatch.setattr(broadcast_sender.asyncio, "sleep", fake_sleep)
    bot = FloodOnceBot()
    await broadcast_sender._send_to_target(bot, broadcast_id, target, {"type": "text", "text": "Hello"})
    status = broadcast_db.count_targets_by_status(broadcast_id)
    assert bot.calls == 2 and sleeps == [1.0]
    assert status["sent"] == 1
    assert download_queue.queued_count == queued_before


@pytest.mark.asyncio
async def test_broadcast_forbidden_does_not_moderation_block_user(configured_db):
    db.upsert_user(701, "recipient", "Recipient", "en")
    broadcast_id = broadcast_db.create_broadcast(1, {"audience": "all"}, {"type": "text", "text": "Hello"})
    broadcast_db.add_targets(broadcast_id, [701])
    target = dict(broadcast_db.get_pending_targets(broadcast_id, 1)[0])

    class ForbiddenBot:
        async def send_message(self, chat_id, text, **kwargs):
            raise TelegramForbiddenError(
                method=SendMessage(chat_id=chat_id, text=text),
                message="bot was blocked by the user",
            )

    await broadcast_sender._send_to_target(
        ForbiddenBot(), broadcast_id, target, {"type": "text", "text": "Hello"}
    )
    assert broadcast_db.count_targets_by_status(broadcast_id)["blocked"] == 1
    assert not db.is_user_blocked(701)
    assert broadcast_sender.start_broadcast(ForbiddenBot(), 999_999) is False
