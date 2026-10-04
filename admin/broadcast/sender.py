"""
Доставка рассылок: отдельная asyncio.Task на каждую запущенную
рассылку, полностью независимая от DownloadQueue/video-worker'а —
не делит с ним семафор, очередь или rate limit.

Состояние — только в SQLite (broadcasts.status, broadcast_targets.status).
Процесс не хранит прогресс в памяти дольше одного батча: при перезапуске
бота можно просто заново вызвать start_broadcast() на рассылке со
статусом 'running' — она продолжит с текущих pending targets, ничего
не отправляя повторно уже отправленным (их статус 'sent').

Пауза реализована кооперативно: цикл проверяет broadcasts.status перед
каждым батчем и останавливается, если статус стал 'paused'/'cancelled'
(гонка возможна в пределах одного in-flight батча, не более того).
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Optional

from aiogram import Bot
from aiogram.exceptions import (
    TelegramBadRequest,
    TelegramForbiddenError,
    TelegramNetworkError,
    TelegramRetryAfter,
)

from admin.broadcast import db
from app.database import log_event

logger = logging.getLogger(__name__)

BATCH_SIZE = 25
# Пауза между отдельными сообщениями внутри батча — простая защита от
# упора в общий Telegram rate limit (помимо персонального RetryAfter).
SEND_INTERVAL_SECONDS = 0.05
MAX_NETWORK_RETRIES = 3
MAX_RETRY_AFTER_RETRIES = 5
MAX_RETRY_AFTER_SECONDS = 300

# Отслеживание активных задач рассылки, чтобы не запустить одну и ту же
# рассылку дважды параллельно в пределах одного процесса (требование
# "не допускать случайного двойного запуска").
_active_tasks: dict[int, asyncio.Task] = {}


def is_broadcast_running_locally(broadcast_id: int) -> bool:
    task = _active_tasks.get(broadcast_id)
    return task is not None and not task.done()


async def _send_one(bot: Bot, chat_id: int, payload: dict) -> None:
    msg_type = payload["type"]
    kwargs = {}
    if payload.get("reply_markup"):
        from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

        rows = [
            [InlineKeyboardButton(text=b["text"], url=b["url"]) for b in row]
            for row in payload["reply_markup"]
        ]
        kwargs["reply_markup"] = InlineKeyboardMarkup(inline_keyboard=rows)
    if payload.get("parse_mode"):
        kwargs["parse_mode"] = payload["parse_mode"]

    if msg_type == "text":
        await bot.send_message(chat_id, payload["text"], **kwargs)
    elif msg_type == "photo":
        await bot.send_photo(chat_id, payload["file_id"], caption=payload.get("caption"), **kwargs)
    elif msg_type == "video":
        await bot.send_video(chat_id, payload["file_id"], caption=payload.get("caption"), **kwargs)
    elif msg_type == "document":
        await bot.send_document(chat_id, payload["file_id"], caption=payload.get("caption"), **kwargs)
    elif msg_type == "animation":
        await bot.send_animation(chat_id, payload["file_id"], caption=payload.get("caption"), **kwargs)
    else:
        raise ValueError(f"Unsupported broadcast message type: {msg_type}")


async def _send_to_target(bot: Bot, broadcast_id: int, target: dict, payload: dict) -> None:
    """
    Один получатель. Не бросает исключения наружу — все ошибки
    обрабатываются здесь и переводят target в финальный статус.
    """
    retries = 0
    flood_retries = 0
    while True:
        try:
            await _send_one(bot, target["target_chat_id"], payload)
            db.set_target_status(target["id"], "sent")
            db.increment_broadcast_counter(broadcast_id, "sent")
            return

        except TelegramRetryAfter as exc:
            flood_retries += 1
            logger.info(
                "RetryAfter %s sec for broadcast=%s target=%s",
                exc.retry_after, broadcast_id, target["target_chat_id"],
            )
            if flood_retries > MAX_RETRY_AFTER_RETRIES or float(exc.retry_after) > MAX_RETRY_AFTER_SECONDS:
                db.set_target_status(target["id"], "failed", error="Repeated RetryAfter")
                db.increment_broadcast_counter(broadcast_id, "failed")
                return
            # Retrying earlier than Telegram's requested delay only creates a
            # flood loop. This sleep affects this broadcast task, not media
            # workers or polling.
            await asyncio.sleep(max(0.1, float(exc.retry_after)))
            # не увеличиваем retries — ожидание retry_after не является
            # "повтором из-за ошибки", это соблюдение лимита Telegram
            continue

        except TelegramForbiddenError:
            db.set_target_status(target["id"], "blocked", error="Forbidden")
            db.increment_broadcast_counter(broadcast_id, "blocked")
            # ``users.blocked`` is a moderation decision. A recipient who
            # blocked the bot is recorded only in this broadcast target and
            # must not become permanently moderation-banned.
            return

        except TelegramBadRequest as exc:
            db.set_target_status(target["id"], "failed", error=str(exc)[:500])
            db.increment_broadcast_counter(broadcast_id, "failed")
            return

        except TelegramNetworkError as exc:
            retries += 1
            if retries > MAX_NETWORK_RETRIES:
                db.set_target_status(target["id"], "failed", error=f"NetworkError: {exc}"[:500])
                db.increment_broadcast_counter(broadcast_id, "failed")
                return
            await asyncio.sleep(1.0 * retries)
            continue

        except Exception as exc:
            logger.exception(
                "Unexpected error sending broadcast=%s to target=%s",
                broadcast_id, target["target_chat_id"],
            )
            db.set_target_status(target["id"], "failed", error=str(exc)[:500])
            db.increment_broadcast_counter(broadcast_id, "failed")
            return


async def _run_broadcast(bot: Bot, broadcast_id: int) -> None:
    try:
        broadcast = db.get_broadcast(broadcast_id)
        if broadcast is None:
            logger.warning("Broadcast %s disappeared before start", broadcast_id)
            return
        payload = json.loads(broadcast["message_payload"])
        while True:
            current = db.get_broadcast(broadcast_id)
            if current is None:
                logger.warning("Broadcast %s disappeared while running", broadcast_id)
                return
            if current["status"] in ("paused", "cancelled"):
                logger.info("Broadcast %s stopped (status=%s)", broadcast_id, current["status"])
                return

            batch = db.get_pending_targets(broadcast_id, BATCH_SIZE)
            if not batch:
                break

            for target in batch:
                current = db.get_broadcast(broadcast_id)
                if current["status"] in ("paused", "cancelled"):
                    return
                await _send_to_target(bot, broadcast_id, dict(target), payload)
                await asyncio.sleep(SEND_INTERVAL_SECONDS)

        db.set_broadcast_status(broadcast_id, "completed")
        log_event("broadcast_finished", target=str(broadcast_id))
        logger.info("Broadcast %s completed", broadcast_id)

    except asyncio.CancelledError:
        # Keep status=running so startup recovery resumes pending recipients.
        logger.info("Broadcast %s interrupted by shutdown", broadcast_id)
        raise
    except Exception:
        logger.exception("Broadcast %s crashed", broadcast_id)
        db.set_broadcast_status(broadcast_id, "failed")
        log_event("broadcast_failed", target=str(broadcast_id), level="error")

    finally:
        _active_tasks.pop(broadcast_id, None)


def start_broadcast(bot: Bot, broadcast_id: int) -> bool:
    """
    Запускает доставку как отдельную asyncio.Task. Возвращает False,
    если рассылка уже выполняется в этом процессе (защита от двойного
    запуска в пределах процесса).

    Безопасно вызывать повторно после рестарта бота — pending targets
    читаются из SQLite, sent/blocked/failed не трогаются повторно.
    """
    if is_broadcast_running_locally(broadcast_id):
        return False
    if db.get_broadcast(broadcast_id) is None:
        return False

    db.set_broadcast_status(broadcast_id, "running")
    task = asyncio.create_task(_run_broadcast(bot, broadcast_id))
    _active_tasks[broadcast_id] = task
    return True


def pause_broadcast(broadcast_id: int) -> None:
    db.set_broadcast_status(broadcast_id, "paused")


def cancel_broadcast(broadcast_id: int) -> None:
    db.set_broadcast_status(broadcast_id, "cancelled")


def retry_failed(bot: Bot, broadcast_id: int) -> int:
    """
    Переводит failed targets в pending и перезапускает доставку только
    по ним (не трогая уже sent/blocked). Возвращает число targets,
    отправленных на повтор.
    """
    count = db.reset_failed_targets_to_pending(broadcast_id)
    if count > 0:
        start_broadcast(bot, broadcast_id)
    return count


async def shutdown_active() -> None:
    """Stop local send loops without finalising still-pending targets."""
    tasks = [task for task in _active_tasks.values() if not task.done()]
    for task in tasks:
        task.cancel()
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)
