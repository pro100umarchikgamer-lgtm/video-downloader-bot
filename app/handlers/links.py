from __future__ import annotations

import logging
import re
import secrets
import time
from dataclasses import dataclass

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from admin import db
from app.context import app_context
from app.i18n import quality_label, t
from app.keyboards.main import cancel_keyboard
from app.security import UnsafeURLError, validate_public_url
from app.services.cache import normalized_url
from app.services.jobs import create_job, finish_job
from app.services.queue import QueueItem, download_queue

router = Router(name="links")
logger = logging.getLogger(__name__)
URL_RE = re.compile(r"https?://[^\s<>\"]+", re.IGNORECASE)
QUALITY_VALUES = ("auto", "360", "480", "720", "1080", "1440", "2160")
QUALITY_ALIASES = {
    "auto": "auto",
    "360": "360", "360p": "360",
    "480": "480", "480p": "480", "sd": "480",
    "720": "720", "720p": "720", "hd": "720",
    "1080": "1080", "1080p": "1080", "fhd": "1080", "fullhd": "1080",
    "1440": "1440", "1440p": "1440", "2k": "1440", "qhd": "1440",
    "2160": "2160", "2160p": "2160", "4k": "2160", "uhd": "2160",
}
PENDING_TTL_SECONDS = 600


@dataclass(slots=True)
class PendingQuality:
    token: str
    user_id: int
    chat_id: int
    url: str
    locale: str
    request_message_id: int | None
    created_at: float


_pending_by_user: dict[int, PendingQuality] = {}


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


def _quality_from_token(value: str | None) -> str | None:
    if not value:
        return None
    return QUALITY_ALIASES.get(value.strip().lower())


def _cleanup_pending() -> None:
    cutoff = time.monotonic() - PENDING_TTL_SECONDS
    stale = [uid for uid, request in _pending_by_user.items() if request.created_at < cutoff]
    for uid in stale:
        _pending_by_user.pop(uid, None)


def _quality_request_keyboard(locale: str, token: str) -> InlineKeyboardMarkup:
    labels = {
        "auto": quality_label(locale, "auto"),
        "360": "360p",
        "480": "480p",
        "720": "720p · HD",
        "1080": "1080p · FHD",
        "1440": "1440p · 2K",
        "2160": "2160p · 4K",
    }
    rows = [
        [
            InlineKeyboardButton(text=labels["720"], callback_data=f"qsel:{token}:720"),
            InlineKeyboardButton(text=labels["1080"], callback_data=f"qsel:{token}:1080"),
        ],
        [
            InlineKeyboardButton(text=labels["1440"], callback_data=f"qsel:{token}:1440"),
            InlineKeyboardButton(text=labels["2160"], callback_data=f"qsel:{token}:2160"),
        ],
        [
            InlineKeyboardButton(text=labels["480"], callback_data=f"qsel:{token}:480"),
            InlineKeyboardButton(text=labels["360"], callback_data=f"qsel:{token}:360"),
        ],
        [InlineKeyboardButton(text=labels["auto"], callback_data=f"qsel:{token}:auto")],
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _locale_for_message(message: Message) -> str:
    if message.chat.type == "private":
        return db.get_user_locale(message.from_user.id, message.from_user.language_code)
    return db.get_group_locale(message.chat.id)


def _fallback_quality(message: Message) -> str:
    context = app_context()
    if message.chat.type == "private":
        return db.get_user_quality(message.from_user.id, context.runtime.get("private_default_quality"))
    return db.get_group_quality(message.chat.id, context.runtime.get("group_default_quality"))


def _user_has_manual_quality(user_id: int) -> bool:
    row = db.get_user(user_id)
    return bool(row and row["quality_manual"])


async def _safe_reply(message: Message, text: str, *, reply_markup=None):
    try:
        return await message.reply(text, reply_markup=reply_markup)
    except Exception:
        return await message.answer(text, reply_markup=reply_markup)


async def _validate_one_url(url: str) -> str | None:
    try:
        await validate_public_url(url)
        return url
    except UnsafeURLError:
        return None


async def _enqueue(
    *,
    user_id: int,
    chat_id: int,
    url: str,
    quality: str,
    locale: str,
    request_message_id: int | None,
    status_message=None,
    output_mode: str = "video",
) -> bool:
    allowed, available = await download_queue.can_add(user_id, 1)
    if not allowed:
        if status_message:
            key = "queue_full" if available == 0 else "queue_partial"
            try:
                await status_message.edit_text(t(locale, key, available=available))
            except Exception:
                pass
        return False

    job_id = None
    reservation_transferred = False
    try:
        job_id = create_job(user_id, chat_id, url, quality)
        if status_message is None:
            # No Message object is available here, so the caller creates a status
            # when it wants a visible processing message.
            status_message_id = None
        else:
            status_message_id = status_message.message_id
            try:
                await status_message.edit_text(
                    t(locale, "processing" if output_mode == "video" else "video_note_processing"),
                    reply_markup=cancel_keyboard(locale, job_id),
                )
            except Exception:
                logger.debug("Could not edit status message job_id=%s", job_id, exc_info=True)

        item = QueueItem(
            job_id=job_id,
            user_id=user_id,
            chat_id=chat_id,
            url=url,
            quality=quality,
            locale=locale,
            request_message_id=request_message_id,
            status_message_id=status_message_id,
            output_mode=output_mode,
        )
        added = await download_queue.put(item)
        if added:
            reservation_transferred = True
            return True
        finish_job(job_id, "cancelled", error_category="duplicate")
        return False
    except Exception:
        logger.exception("Could not enqueue download user_id=%s", user_id)
        if job_id:
            finish_job(job_id, "failed", error_category="temporary_error")
        return False
    finally:
        if not reservation_transferred:
            await download_queue.release_reserved(user_id)


async def _handle_single_command(
    message: Message,
    *,
    output_mode: str = "video",
) -> None:
    if not message.from_user:
        return
    locale = _locale_for_message(message)
    context = app_context()
    if context.runtime.get("maintenance_mode"):
        await _safe_reply(message, t(locale, "maintenance"))
        return

    rate_ok, remaining = await download_queue.check_message_allowed(message.from_user.id)
    if not rate_ok:
        await _safe_reply(message, t(locale, "rate_limit", seconds=remaining))
        return

    parts = (message.text or "").strip().split(maxsplit=2)
    quality = "720" if output_mode == "circle" else "1080"
    url_text = ""

    if len(parts) >= 2:
        maybe_quality = _quality_from_token(parts[1])
        if maybe_quality is not None and output_mode == "video":
            quality = maybe_quality
            url_text = parts[2] if len(parts) >= 3 else ""
        else:
            url_text = " ".join(parts[1:])

    urls = extract_urls(url_text)
    if not urls and message.reply_to_message:
        reply_text = (message.reply_to_message.text or message.reply_to_message.caption or "")
        urls = extract_urls(reply_text)
    if len(urls) != 1:
        await _safe_reply(message, t(locale, "command_usage_circle" if output_mode == "circle" else "command_usage_download"))
        return

    url = await _validate_one_url(urls[0])
    if not url:
        await _safe_reply(message, t(locale, "invalid_link"))
        return

    status = await _safe_reply(
        message,
        t(locale, "processing" if output_mode == "video" else "video_note_processing"),
    )
    original_message_id = (
        message.reply_to_message.message_id
        if message.reply_to_message and extract_urls(message.reply_to_message.text or message.reply_to_message.caption or "")
        else message.message_id
    )
    added = await _enqueue(
        user_id=message.from_user.id,
        chat_id=message.chat.id,
        url=url,
        quality=quality,
        locale=locale,
        request_message_id=original_message_id,
        status_message=status,
        output_mode=output_mode,
    )
    if not added:
        try:
            await status.delete()
        except Exception:
            pass


@router.message(Command("dl", "download"))
async def download_command(message: Message) -> None:
    await _handle_single_command(message, output_mode="video")


@router.message(Command("circle"))
async def circle_command(message: Message) -> None:
    await _handle_single_command(message, output_mode="circle")


@router.callback_query(F.data.startswith("qsel:"))
async def quality_selection_callback(callback: CallbackQuery) -> None:
    await callback.answer()
    if not callback.message:
        return
    _cleanup_pending()
    parts = (callback.data or "").split(":")
    if len(parts) != 3:
        return
    _, token, quality = parts
    if quality not in QUALITY_VALUES:
        return
    pending = _pending_by_user.get(callback.from_user.id)
    locale = db.get_user_locale(callback.from_user.id, callback.from_user.language_code)
    if (
        pending is None
        or pending.token != token
        or pending.chat_id != callback.message.chat.id
        or time.monotonic() - pending.created_at > PENDING_TTL_SECONDS
    ):
        await callback.message.edit_text(t(locale, "stale_action"))
        return
    _pending_by_user.pop(callback.from_user.id, None)
    added = await _enqueue(
        user_id=pending.user_id,
        chat_id=pending.chat_id,
        url=pending.url,
        quality=quality,
        locale=pending.locale,
        request_message_id=pending.request_message_id,
        status_message=callback.message,
        output_mode="video",
    )
    if not added:
        try:
            await callback.message.edit_text(t(locale, "queue_full"))
        except Exception:
            pass


@router.message(F.text)
async def handle_video_links(message: Message) -> None:
    if not message.from_user:
        return
    urls = extract_urls(message.text or "")
    if not urls:
        return

    context = app_context()
    locale = _locale_for_message(message)
    if context.runtime.get("maintenance_mode"):
        await _safe_reply(message, t(locale, "maintenance"))
        return

    allowed, remaining = await download_queue.check_message_allowed(message.from_user.id)
    if not allowed:
        await _safe_reply(message, t(locale, "rate_limit", seconds=remaining))
        return

    max_batch = int(context.runtime.get("max_batch_size"))
    if len(urls) > max_batch:
        await _safe_reply(message, t(locale, "batch_limit", limit=max_batch))
        return

    valid_urls: list[str] = []
    for url in urls:
        checked = await _validate_one_url(url)
        if checked:
            valid_urls.append(checked)
    if not valid_urls:
        await _safe_reply(message, t(locale, "invalid_link"))
        return
    invalid_count = len(urls) - len(valid_urls)

    # In private chats a user who has never chosen a persistent quality gets a
    # one-tap per-download choice. FHD remains the operational default elsewhere.
    if (
        message.chat.type == "private"
        and len(valid_urls) == 1
        and context.runtime.get("quality_selection_enabled")
        and not _user_has_manual_quality(message.from_user.id)
    ):
        _cleanup_pending()
        token = secrets.token_urlsafe(6)
        pending = PendingQuality(
            token=token,
            user_id=message.from_user.id,
            chat_id=message.chat.id,
            url=valid_urls[0],
            locale=locale,
            request_message_id=message.message_id,
            created_at=time.monotonic(),
        )
        _pending_by_user[message.from_user.id] = pending
        await _safe_reply(
            message,
            t(locale, "quality_request", default="1080p"),
            reply_markup=_quality_request_keyboard(locale, token),
        )
        if invalid_count:
            await message.answer(t(locale, "batch_invalid", count=invalid_count))
        return

    quality = _fallback_quality(message)
    accepted = 0
    for url in valid_urls:
        status = None
        if len(valid_urls) == 1:
            status = await _safe_reply(message, t(locale, "processing"))
        added = await _enqueue(
            user_id=message.from_user.id,
            chat_id=message.chat.id,
            url=url,
            quality=quality,
            locale=locale,
            request_message_id=message.message_id,
            status_message=status,
            output_mode="video",
        )
        if added:
            accepted += 1
        elif status:
            try:
                await status.delete()
            except Exception:
                pass

    if len(valid_urls) > 1:
        await message.answer(t(locale, "accepted_many", count=accepted))
    if invalid_count:
        await message.answer(t(locale, "batch_invalid", count=invalid_count))
