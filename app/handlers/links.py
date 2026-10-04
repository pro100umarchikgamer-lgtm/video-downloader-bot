from __future__ import annotations

import logging
import re

from aiogram import F, Router
from aiogram.types import Message

from admin import db
from app.context import app_context
from app.i18n import t
from app.keyboards.main import cancel_keyboard
from app.security import UnsafeURLError, validate_public_url
from app.services.cache import normalized_url
from app.services.jobs import create_job, finish_job
from app.services.queue import QueueItem, download_queue

router = Router(name="links")
logger = logging.getLogger(__name__)
URL_RE = re.compile(r"https?://[^\s<>\"]+", re.IGNORECASE)


def extract_urls(text: str) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for candidate in URL_RE.findall(text):
        url = candidate.rstrip(".,!?;:)]}»\"")
        try:
            identity = normalized_url(url)
        except ValueError:
            identity = url
        if identity not in seen:
            seen.add(identity)
            result.append(url)
    return result


@router.message(F.text)
async def handle_video_links(message: Message) -> None:
    if not message.from_user:
        return
    urls = extract_urls(message.text or "")
    if not urls:
        return
    context = app_context()
    locale = (
        db.get_user_locale(message.from_user.id, message.from_user.language_code)
        if message.chat.type == "private"
        else db.get_group_locale(message.chat.id)
    )
    if context.runtime.get("maintenance_mode"):
        await message.answer(t(locale, "maintenance"))
        return
    allowed, remaining = await download_queue.check_message_allowed(message.from_user.id)
    if not allowed:
        await message.answer(t(locale, "rate_limit", seconds=remaining))
        return
    max_batch = int(context.runtime.get("max_batch_size"))
    if len(urls) > max_batch:
        await message.answer(t(locale, "batch_limit", limit=max_batch))
        return

    valid_urls: list[str] = []
    for url in urls:
        try:
            await validate_public_url(url)
            valid_urls.append(url)
        except UnsafeURLError:
            continue
    if not valid_urls:
        await message.answer(t(locale, "invalid_link"))
        return
    invalid_count = len(urls) - len(valid_urls)

    allowed, available = await download_queue.can_add(message.from_user.id, len(valid_urls))
    if not allowed:
        key = "queue_full" if available == 0 else "queue_partial"
        await message.answer(t(locale, key, available=available))
        return

    fallback_quality = (
        context.runtime.get("private_default_quality")
        if message.chat.type == "private"
        else context.runtime.get("group_default_quality")
    )
    if context.runtime.get("quality_selection_enabled"):
        quality = (
            db.get_user_quality(message.from_user.id, fallback_quality)
            if message.chat.type == "private"
            else db.get_group_quality(message.chat.id, fallback_quality)
        )
    else:
        quality = fallback_quality
    accepted = 0
    for url in valid_urls:
        job_id = None
        status_message = None
        reservation_transferred = False
        try:
            job_id = create_job(message.from_user.id, message.chat.id, url, quality)
            if len(valid_urls) == 1:
                try:
                    status_message = await message.answer(
                        t(locale, "processing"),
                        reply_markup=cancel_keyboard(locale, job_id),
                    )
                except Exception:
                    logger.warning("Could not send initial status job_id=%s", job_id, exc_info=True)
            item = QueueItem(
                job_id=job_id,
                user_id=message.from_user.id,
                chat_id=message.chat.id,
                url=url,
                quality=quality,
                locale=locale,
                status_message_id=status_message.message_id if status_message else None,
            )
            added = await download_queue.put(item)
            if added:
                accepted += 1
                reservation_transferred = True
                continue
            finish_job(job_id, "cancelled", error_category="duplicate")
        except Exception:
            logger.exception("Could not enqueue download user_id=%s", message.from_user.id)
            if job_id:
                finish_job(job_id, "failed", error_category="temporary_error")
        finally:
            # A successfully queued job owns its reservation until the worker
            # finishes. Every other outcome releases exactly one slot.
            if not reservation_transferred:
                await download_queue.release_reserved(message.from_user.id)
        if job_id is not None:
            # Duplicate/failed queue items have no useful cancel action.
            if status_message:
                try:
                    await status_message.delete()
                except Exception:
                    pass
    if len(valid_urls) > 1:
        await message.answer(t(locale, "accepted_many", count=accepted))
    if invalid_count:
        await message.answer(t(locale, "batch_invalid", count=invalid_count))
