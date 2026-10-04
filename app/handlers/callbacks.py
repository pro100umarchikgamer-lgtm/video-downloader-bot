from __future__ import annotations

from aiogram import F, Router
from aiogram.types import CallbackQuery

from admin import db
from app.context import app_context
from app.i18n import t
from app.services.jobs import create_job, finish_job, get_job
from app.services.queue import QueueItem, download_queue

router = Router(name="functional_callbacks")


@router.callback_query(F.data.startswith("job:cancel:"))
async def cancel_download(callback: CallbackQuery) -> None:
    job_id = callback.data.split(":", 2)[2]
    await callback.answer()
    job = get_job(job_id)
    locale = (
        db.get_group_locale(job["chat_id"])
        if job is not None and job["chat_id"] < 0
        else db.get_user_locale(callback.from_user.id, callback.from_user.language_code)
    )
    if job is None:
        if callback.message:
            await callback.message.edit_text(t(locale, "stale_action"))
        return
    if job["requester_id"] != callback.from_user.id:
        if callback.message:
            await callback.message.answer(t(locale, "not_yours"))
        return
    result = await download_queue.cancel(job_id, callback.from_user.id)
    if result == "forbidden":
        if callback.message:
            await callback.message.answer(t(locale, "not_yours"))
    elif result == "missing":
        if callback.message:
            await callback.message.edit_text(t(locale, "already_finished"))
    elif result == "queued":
        finish_job(job_id, "cancelled", error_category="cancelled")
        if callback.message:
            await callback.message.edit_text(t(locale, "cancelled"))
    elif callback.message:
        await callback.message.edit_text(t(locale, "cancel_requested"))


@router.callback_query(F.data.startswith("job:retry:"))
async def retry_download(callback: CallbackQuery) -> None:
    await callback.answer()
    old_id = callback.data.split(":", 2)[2]
    old = get_job(old_id)
    locale = (
        db.get_group_locale(old["chat_id"])
        if old is not None and old["chat_id"] < 0
        else db.get_user_locale(callback.from_user.id, callback.from_user.language_code)
    )
    if old is None:
        if callback.message:
            await callback.message.edit_text(t(locale, "stale_action"))
        return
    if old["requester_id"] != callback.from_user.id:
        if callback.message:
            await callback.message.answer(t(locale, "not_yours"))
        return
    context = app_context()
    if context.runtime.get("maintenance_mode"):
        if callback.message:
            await callback.message.answer(t(locale, "maintenance"))
        return
    rate_ok, remaining = await download_queue.check_message_allowed(callback.from_user.id)
    if not rate_ok:
        if callback.message:
            await callback.message.answer(t(locale, "rate_limit", seconds=remaining))
        return
    allowed, _ = await download_queue.can_add(callback.from_user.id, 1)
    if not allowed:
        if callback.message:
            await callback.message.answer(t(locale, "queue_full"))
        return
    job_id = create_job(
        callback.from_user.id,
        old["chat_id"],
        old["url"],
        old["requested_quality"],
        retry_of=old_id,
    )
    added = await download_queue.put(
        QueueItem(
            job_id=job_id,
            user_id=callback.from_user.id,
            chat_id=old["chat_id"],
            url=old["url"],
            quality=old["requested_quality"],
            locale=locale,
            retry_of=old_id,
        )
    )
    if not added:
        await download_queue.release_reserved(callback.from_user.id)
        finish_job(job_id, "cancelled", error_category="duplicate")
    if callback.message:
        await callback.message.edit_reply_markup(reply_markup=None)


@router.callback_query(F.data.startswith("vn:"))
async def make_video_note(callback: CallbackQuery, bot) -> None:
    token = callback.data.split(":", 1)[1]
    context = app_context()
    row = context.video_notes.get_delivery(token)
    locale = db.get_user_locale(callback.from_user.id, callback.from_user.language_code)
    if row is not None and row["chat_id"] < 0:
        locale = db.get_group_locale(row["chat_id"])
    await callback.answer()
    if row is None:
        if callback.message:
            await callback.message.answer(t(locale, "stale_action"))
        return
    if row["requester_id"] != callback.from_user.id:
        if callback.message:
            await callback.message.answer(t(locale, "not_yours"))
        return
    claim = context.video_notes.claim(token)
    if claim == "busy":
        if callback.message:
            await callback.message.answer(t(locale, "video_note_busy"))
        return
    if claim == "done":
        if callback.message:
            await callback.message.answer(t(locale, "video_note_ready"))
        return
    if claim == "missing":
        if callback.message:
            await callback.message.answer(t(locale, "stale_action"))
        return
    status = await callback.message.answer(t(locale, "video_note_processing")) if callback.message else None
    result = await context.video_notes.create_and_send(bot, token)
    if status:
        if result == "done":
            try:
                await status.delete()
            except Exception:
                pass
        else:
            await status.edit_text(t(locale, "video_note_failed"))


@router.callback_query()
async def stale_user_callback(callback: CallbackQuery) -> None:
    """Final user-router guard: malformed/old callbacks never raise."""
    locale = db.get_user_locale(callback.from_user.id, callback.from_user.language_code)
    await callback.answer(t(locale, "stale_action"), show_alert=False)
