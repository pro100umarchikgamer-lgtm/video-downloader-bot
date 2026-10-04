from __future__ import annotations

import asyncio
import json
import logging
import re
import shutil
import time
from contextlib import suppress
from dataclasses import dataclass

from aiogram import Bot
from aiogram.exceptions import TelegramForbiddenError
from aiogram.types import FSInputFile

from admin.buttons.service import build_video_keyboard
from app.context import app_context
from app.database import log_event
from app.errors import DownloadCancelled, UserError
from app.i18n import t
from app.keyboards.main import cancel_keyboard, retry_keyboard
from app.services.cache import get_content_id
from app.services.downloader import DownloadResult
from app.services.formats import available_qualities, choose_quality
from app.services.inflight import InflightCoordinator
from app.services.jobs import (
    finish_job,
    increment_cache_counter,
    start_job,
    update_job,
)
from app.services.queue import QueueItem, download_queue
from app.services.storage import CacheReferenceFailure, classify_cache_delivery_error
from app.services.telegram import telegram_retry
from app.services.video_note import is_video_note_eligible

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class PreparedMedia:
    result: DownloadResult
    cached_file_id: str | None


inflight = InflightCoordinator[PreparedMedia]()


def _clean_title(value: str | None) -> str:
    title = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", "", value or "Video")
    title = re.sub(r"\s+", " ", title).strip()
    return (title or "Video")[:900]


async def _safe_edit(bot: Bot, item: QueueItem, message, text: str, reply_markup=None) -> None:
    if not message and not item.status_message_id:
        return
    try:
        if message:
            await telegram_retry(lambda: message.edit_text(text, reply_markup=reply_markup), max_attempts=2)
        else:
            await telegram_retry(
                lambda: bot.edit_message_text(
                    text,
                    chat_id=item.chat_id,
                    message_id=item.status_message_id,
                    reply_markup=reply_markup,
                ),
                max_attempts=2,
            )
    except Exception:
        logger.debug("Status edit failed", exc_info=True)


async def _safe_delete(bot: Bot, item: QueueItem, message) -> None:
    if not message and not item.status_message_id:
        return
    try:
        if message:
            await message.delete()
        else:
            await bot.delete_message(item.chat_id, item.status_message_id)
    except Exception:
        pass


def _check_disk(estimated_size: int | None, max_size: int) -> None:
    context = app_context()
    free = shutil.disk_usage(context.bootstrap.temp_dir).free
    reserve = int(context.runtime.get("disk_min_free_mb")) * 1024 * 1024
    expected = estimated_size * 2 if estimated_size else min(max_size, 1024 * 1024 * 1024)
    if free < reserve + expected:
        raise UserError("disk_pressure", "insufficient temporary disk space", True)


async def _deliver_cached(bot: Bot, item: QueueItem, info: dict, content_id: str, quality: int, status) -> bool:
    context = app_context()
    entries = context.storage.cache_entries(content_id, quality)
    saw_temporary = False
    for entry in entries:
        if item.cancel_event.is_set():
            raise DownloadCancelled()
        token = context.video_notes.create_delivery(
            job_id=item.job_id,
            requester_id=item.user_id,
            chat_id=item.chat_id,
            content_id=content_id,
            source_url=item.url,
            quality=quality,
            telegram_file_id=entry["telegram_file_id"],
            duration=info.get("duration") or entry["duration"],
        )
        keyboard = await build_video_keyboard(bot, locale=item.locale, video_note_token=token)
        await _safe_edit(bot, item, status, t(item.locale, "sending"), reply_markup=cancel_keyboard(item.locale, item.job_id))
        started = time.monotonic()
        try:
            await telegram_retry(
                lambda entry=entry: bot.send_video(
                    item.chat_id,
                    video=entry["telegram_file_id"],
                    caption=_clean_title(info.get("title") or entry["title"]),
                    supports_streaming=True,
                    reply_markup=keyboard,
                )
            )
            context.storage.touch_entry(entry["id"])
            increment_cache_counter(item.user_id, True)
            update_job(
                item.job_id,
                cache_hit=1,
                upload_ms=round((time.monotonic() - started) * 1000),
                filesize=entry["filesize"],
            )
            return True
        except TelegramForbiddenError:
            raise
        except Exception as exc:
            failure = classify_cache_delivery_error(exc)
            if failure is CacheReferenceFailure.PERMANENT:
                context.storage.delete_permanently_stale(entry["id"], reason=str(exc))
                continue
            if failure is CacheReferenceFailure.RECIPIENT:
                raise
            saw_temporary = True
            logger.warning("Temporary cached-media delivery failure entry_id=%s: %s", entry["id"], exc)
            continue
    if saw_temporary and entries:
        # A transient failure of a reusable Telegram reference is not proof
        # that the source or direct-upload path is unavailable.  Keep the
        # cache rows intact and continue through the normal preparation path.
        # This keeps cache/storage an optimisation rather than a delivery
        # dependency.
        logger.info(
            "Falling back to fresh media preparation after temporary cached-media failure job_id=%s",
            item.job_id,
        )
    return False


async def process_download(bot: Bot, item: QueueItem) -> None:
    context = app_context()
    started_total = time.monotonic()
    status = None
    start_job(item.job_id, round((time.monotonic() - item.created_monotonic) * 1000))
    try:
        if item.cancel_event.is_set():
            raise DownloadCancelled()
        if item.status_message_id is None:
            status = await telegram_retry(
                lambda: bot.send_message(
                    item.chat_id,
                    t(item.locale, "processing"),
                    reply_markup=cancel_keyboard(item.locale, item.job_id),
                )
            )
        metadata_started = time.monotonic()
        info = await context.downloader.extract_info(item.url)
        metadata_ms = round((time.monotonic() - metadata_started) * 1000)
        if item.cancel_event.is_set():
            raise DownloadCancelled()
        content_id = get_content_id(item.url, info)
        max_bytes = int(context.runtime.get("max_delivery_mb")) * 1024 * 1024
        quality, estimated = choose_quality(info.get("formats") or [], max_bytes, item.quality)
        if estimated is not None and estimated > max_bytes:
            raise UserError("too_large", f"estimated size {estimated}", False)
        _check_disk(estimated, max_bytes)
        source = str(info.get("extractor_key") or info.get("extractor") or "unknown")
        update_job(
            item.job_id,
            effective_quality=quality,
            source=source,
            content_id=content_id,
            title=_clean_title(info.get("title")),
            duration=info.get("duration"),
            available_qualities=json.dumps(available_qualities(info.get("formats") or [])),
            metadata_ms=metadata_ms,
        )

        if await _deliver_cached(bot, item, info, content_id, quality, status):
            finish_job(item.job_id, "success", total_ms=round((time.monotonic() - started_total) * 1000))
            await _safe_delete(bot, item, status)
            return
        increment_cache_counter(item.user_id, False)
        await _safe_edit(bot, item, status, t(item.locale, "downloading"), reply_markup=cancel_keyboard(item.locale, item.job_id))

        async def prepare(shared_cancel: asyncio.Event) -> PreparedMedia:
            result = None
            try:
                result = await context.downloader.download(
                    item.url,
                    quality,
                    max_bytes,
                    info=info,
                    cancel_event=shared_cancel,
                )
                cached = await context.storage.store(
                    bot,
                    result,
                    content_id=content_id,
                    original_url=item.url,
                )
                return PreparedMedia(result=result, cached_file_id=cached.file_id if cached else None)
            except Exception:
                if result is not None:
                    context.downloader.cleanup(result.path)
                raise

        key = f"{content_id}:{quality}"
        async with inflight.acquire(
            key,
            prepare,
            item.cancel_event,
            lambda media: context.downloader.cleanup(media.result.path),
        ) as media:
            if item.cancel_event.is_set():
                raise DownloadCancelled()
            token = context.video_notes.create_delivery(
                job_id=item.job_id,
                requester_id=item.user_id,
                chat_id=item.chat_id,
                content_id=content_id,
                source_url=item.url,
                quality=media.result.quality,
                telegram_file_id=media.cached_file_id,
                duration=media.result.duration,
                source_path=media.result.path if is_video_note_eligible(media.result.duration) else None,
            )
            keyboard = await build_video_keyboard(bot, locale=item.locale, video_note_token=token)
            await _safe_edit(bot, item, status, t(item.locale, "sending"), reply_markup=cancel_keyboard(item.locale, item.job_id))
            upload_started = time.monotonic()
            async def send(video):
                return await telegram_retry(
                    lambda: bot.send_video(
                        item.chat_id,
                        video=video,
                        caption=_clean_title(media.result.title),
                        supports_streaming=True,
                        reply_markup=keyboard,
                    )
                )

            if media.cached_file_id:
                try:
                    sent = await send(media.cached_file_id)
                except TelegramForbiddenError:
                    raise
                except Exception as exc:
                    failure = classify_cache_delivery_error(exc)
                    if failure is CacheReferenceFailure.PERMANENT:
                        for entry in context.storage.cache_entries(content_id, quality):
                            if entry["telegram_file_id"] == media.cached_file_id:
                                context.storage.delete_permanently_stale(entry["id"], reason=str(exc))
                    logger.warning(
                        "Fresh storage reference could not be delivered; using direct upload job_id=%s failure=%s",
                        item.job_id,
                        failure.value,
                    )
                    sent = await send(FSInputFile(media.result.path))
            else:
                sent = await send(FSInputFile(media.result.path))
            # If storage was unavailable, the direct-delivery file_id is saved
            # only in the short-lived delivery record, not presented as a cache
            # channel copy.
            if token and sent.video and not media.cached_file_id:
                with suppress(Exception):
                    from app.database import get_connection
                    with get_connection() as conn:
                        conn.execute("UPDATE media_deliveries SET telegram_file_id=? WHERE token=?", (sent.video.file_id, token))
                        conn.commit()
            update_job(
                item.job_id,
                filesize=media.result.filesize,
                download_ms=media.result.elapsed_ms,
                processing_ms=media.result.processing_ms,
                upload_ms=round((time.monotonic() - upload_started) * 1000),
            )
        finish_job(item.job_id, "success", total_ms=round((time.monotonic() - started_total) * 1000))
        await _safe_delete(bot, item, status)
    except DownloadCancelled:
        finish_job(item.job_id, "cancelled", error_category="cancelled", total_ms=round((time.monotonic() - started_total) * 1000))
        await _safe_edit(bot, item, status, t(item.locale, "cancelled"))
    except TelegramForbiddenError:
        finish_job(item.job_id, "failed", error_category="delivery_error", total_ms=round((time.monotonic() - started_total) * 1000))
        logger.info("Bot blocked or removed while delivering job_id=%s", item.job_id)
    except UserError as exc:
        finish_job(item.job_id, "failed", error_category=exc.category, total_ms=round((time.monotonic() - started_total) * 1000))
        logger.warning("Job failed job_id=%s category=%s detail=%s", item.job_id, exc.category, exc.detail)
        if exc.category in {"temporary_error", "delivery_error", "disk_pressure"}:
            log_event("download_error", target=item.job_id, level="warning", metadata={"category": exc.category})
        if exc.category == "disk_pressure" and context.alerts is not None:
            await context.alerts.notify("disk-low", "Недостаточно места для новой загрузки.", cooldown_minutes=30)
        markup = retry_keyboard(item.locale, item.job_id) if exc.retryable else None
        await _safe_edit(bot, item, status, t(item.locale, exc.category), reply_markup=markup)
    except Exception:
        finish_job(item.job_id, "failed", error_category="temporary_error", total_ms=round((time.monotonic() - started_total) * 1000))
        logger.exception("Unexpected download failure job_id=%s", item.job_id)
        log_event("download_error", target=item.job_id, level="error", metadata={"category": "unexpected"})
        await _safe_edit(bot, item, status, t(item.locale, "temporary_error"), reply_markup=retry_keyboard(item.locale, item.job_id))
async def worker_loop(bot: Bot) -> None:
    try:
        await download_queue.run(lambda item: process_download(bot, item))
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.exception("Queue worker crashed")
        raise
