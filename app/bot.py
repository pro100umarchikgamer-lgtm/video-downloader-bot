"""Application composition root."""

from __future__ import annotations

import asyncio
import logging
import shutil
from contextlib import suppress
from pathlib import Path

from aiogram import Bot, Dispatcher
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.client.telegram import TelegramAPIServer
from aiogram.types import (
    BotCommand,
    BotCommandScopeAllPrivateChats,
    BotCommandScopeDefault,
    BotCommandScopeAllGroupChats,
    BotCommandScopeChat,
)

from admin.auth import bootstrap_owner_ids
from admin.broadcast import db as broadcast_db
from admin.broadcast import sender as broadcast_sender
from admin.buttons.db import init_buttons_db
from admin.buttons.service import ensure_add_to_group_entry
from admin.db import init_admin_db, list_admins
from admin.handlers import admin_router
from admin.middleware import TrackingMiddleware
from app.config import BootstrapConfig, load_bootstrap_config
from app.context import AppContext, set_context
from app.database import init_db, log_event, set_database_path
from app.handlers import user_router
from app.i18n import SUPPORTED_LOCALES, t
from app.runtime_config import init_runtime_config
from app.services.alerts import AlertService
from app.services.cleanup import apply_data_retention, cleanup_orphan_temp
from app.services.downloader import Downloader
from app.services.queue import download_queue
from app.services.jobs import recover_interrupted_jobs
from app.services.storage import StorageService
from app.services.telegram import telegram_retry
from app.services.video_note import VideoNoteService
from worker.worker import worker_loop

logger = logging.getLogger(__name__)


def create_bot(config: BootstrapConfig) -> Bot:
    if not config.telegram_api_base_url:
        return Bot(config.bot_token)
    server = TelegramAPIServer.from_base(
        config.telegram_api_base_url,
        is_local=config.telegram_api_is_local,
    )
    return Bot(config.bot_token, session=AiohttpSession(api=server))


async def register_commands(bot: Bot) -> None:
    async def set_safely(commands, scope, language_code: str | None = None) -> bool:
        try:
            await telegram_retry(
                lambda: bot.set_my_commands(
                    commands,
                    scope=scope,
                    language_code=language_code,
                )
            )
            return True
        except Exception:
            # A transient command-menu failure must not take the downloader
            # offline. Polling can start and the menu will be retried on the
            # next restart; users can still type the documented commands.
            logger.warning(
                "Could not register bot commands (scope=%s language=%s)",
                type(scope).__name__, language_code or "default",
                exc_info=True,
            )
            return False

    default = [
        BotCommand(command="start", description=t("ru", "cmd_start")),
        BotCommand(command="settings", description=t("ru", "cmd_settings")),
        BotCommand(command="dl", description=t("ru", "cmd_download")),
        BotCommand(command="circle", description=t("ru", "cmd_circle")),
    ]
    await set_safely(default, BotCommandScopeDefault())
    await set_safely(default, BotCommandScopeAllPrivateChats())
    await set_safely(default, BotCommandScopeAllGroupChats())
    telegram_codes = {"ru": "ru", "kk": "kk", "uz_latn": "uz", "en": "en"}
    for locale, language_code in telegram_codes.items():
        commands = [
            BotCommand(command="start", description=t(locale, "cmd_start")),
            BotCommand(command="settings", description=t(locale, "cmd_settings")),
            BotCommand(command="dl", description=t(locale, "cmd_download")),
            BotCommand(command="circle", description=t(locale, "cmd_circle")),
        ]
        await set_safely(commands, BotCommandScopeAllPrivateChats(), language_code)
    admin_ids = set(bootstrap_owner_ids()) | {row["telegram_id"] for row in list_admins() if row["enabled"]}
    for admin_id in admin_ids:
        await set_safely(
            [*default, BotCommand(command="admin", description="Панель администратора")],
            BotCommandScopeChat(chat_id=admin_id),
        )


async def maintenance_loop(context: AppContext, ready_file: Path) -> None:
    while True:
        try:
            # Keep Docker readiness fresh only while polling is active and at
            # least one supervised download worker is actually running.
            # A live/zombie Python PID alone is therefore insufficient.
            if context.polling and any(state == "running" for state in context.worker_states.values()):
                ready_file.touch()
            cleanup_orphan_temp(context.bootstrap.temp_dir, context.runtime)
            context.video_notes.cleanup_expired()
            apply_data_retention(context.runtime)
            free = shutil.disk_usage(context.bootstrap.data_dir).free
            reserve = int(context.runtime.get("disk_min_free_mb")) * 1024 * 1024
            if free < reserve and context.alerts is not None:
                await context.alerts.notify("disk-low", "Критически мало свободного места на диске.", cooldown_minutes=30)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Periodic maintenance failed")
        await asyncio.sleep(60)


async def resume_broadcasts(bot: Bot) -> None:
    for row in broadcast_db.list_broadcasts(status="running", limit=1000):
        broadcast_sender.start_broadcast(bot, row["id"])


async def worker_supervisor(bot: Bot, index: int, context: AppContext) -> None:
    crashes = 0
    while context.polling:
        context.worker_states[index] = "running"
        try:
            await worker_loop(bot)
            context.worker_states[index] = "stopped"
            return
        except asyncio.CancelledError:
            context.worker_states[index] = "cancelled"
            raise
        except Exception:
            crashes += 1
            context.worker_states[index] = "restarting"
            logger.exception("Worker %s crashed (%s)", index, crashes)
            if context.alerts is not None:
                await context.alerts.notify(
                    f"worker-crash:{index}",
                    f"Download worker #{index} аварийно перезапущен.",
                    cooldown_minutes=15,
                )
            await asyncio.sleep(min(10, crashes))


async def main() -> None:
    config = load_bootstrap_config()
    logging.basicConfig(
        level=getattr(logging, config.log_level, logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    set_database_path(config.database_path)
    try:
        applied = init_db()
    except Exception:
        logger.exception("Database migration failed")
        emergency_bot = create_bot(config)
        for owner_id in config.owner_ids:
            with suppress(Exception):
                await emergency_bot.send_message(owner_id, "🔴 Запуск остановлен: не удалось применить миграцию базы данных.")
        await emergency_bot.session.close()
        raise
    runtime = init_runtime_config(config)
    for version in applied:
        log_event("migration_applied", metadata={"version": version})
    interrupted = recover_interrupted_jobs()
    init_admin_db()
    broadcast_db.init_broadcast_db()
    init_buttons_db()
    ensure_add_to_group_entry()
    download_queue.configure(runtime)

    downloader = Downloader(config.temp_dir)
    storage = StorageService(runtime)
    storage.seed_legacy_channels(config.legacy_cache_channels)
    video_notes = VideoNoteService(runtime, downloader, config.temp_dir)
    context = AppContext(config, runtime, downloader, storage, video_notes)
    set_context(context)

    bot = create_bot(config)
    alerts = AlertService(bot, config, runtime)
    storage.alert_service = alerts
    context.alerts = alerts
    dispatcher = Dispatcher()
    dispatcher.update.outer_middleware(TrackingMiddleware())
    dispatcher.include_router(admin_router)
    dispatcher.include_router(user_router)

    cleanup_orphan_temp(config.temp_dir, runtime)
    interrupted_notes = video_notes.recover_interrupted()
    video_notes.cleanup_expired()
    apply_data_retention(runtime)
    await register_commands(bot)
    await resume_broadcasts(bot)
    log_event("application_started", metadata={"version": config.app_version, "local_api": config.telegram_api_is_local})
    if interrupted:
        log_event("jobs_recovered_after_restart", level="warning", metadata={"count": interrupted})
    if interrupted_notes:
        log_event("video_notes_recovered_after_restart", level="warning", metadata={"count": interrupted_notes})

    worker_count = int(runtime.get("queue_workers"))
    context.polling = True
    context.worker_states = {index: "starting" for index in range(worker_count)}
    ready_file = config.data_dir / "health.ready"
    ready_file.unlink(missing_ok=True)
    workers = [asyncio.create_task(worker_supervisor(bot, index, context), name=f"download-worker-{index}") for index in range(worker_count)]
    maintenance = asyncio.create_task(maintenance_loop(context, ready_file), name="maintenance")
    try:
        await dispatcher.start_polling(bot, allowed_updates=dispatcher.resolve_used_update_types())
    finally:
        context.polling = False
        await download_queue.stop(cancel_active=False)
        maintenance.cancel()
        with suppress(asyncio.CancelledError):
            await maintenance
        try:
            await asyncio.wait_for(asyncio.gather(*workers, return_exceptions=True), timeout=30)
        except asyncio.TimeoutError:
            for task in workers:
                task.cancel()
            await asyncio.gather(*workers, return_exceptions=True)
        await broadcast_sender.shutdown_active()
        cleanup_orphan_temp(config.temp_dir, runtime)
        ready_file.unlink(missing_ok=True)
        await bot.session.close()
        log_event("application_stopped")


def run() -> None:
    asyncio.run(main())
