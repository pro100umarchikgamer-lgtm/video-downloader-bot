"""
Middleware уровня Dispatcher: фиксирует активность пользователей и групп
на каждом входящем Update (сообщения и callback-запросы, личка и группы).

Подключается один раз в bot.py через dp.update.outer_middleware(...).
Не требует изменений в app/handlers/links.py или других хендлерах.
"""

from __future__ import annotations

from typing import Any, Awaitable, Callable, Optional

from aiogram import BaseMiddleware
from aiogram.types import Chat, TelegramObject, Update, User

from admin.db import upsert_group, upsert_user


def _extract_user(event: Update) -> Optional[User]:
    inner = event.event
    return getattr(inner, "from_user", None)


def _extract_chat(event: Update) -> Optional[Chat]:
    inner = event.event
    chat = getattr(inner, "chat", None)
    if chat is not None:
        return chat
    # callback_query не имеет .chat напрямую, но имеет .message.chat
    message = getattr(inner, "message", None)
    if message is not None:
        return getattr(message, "chat", None)
    return None


class TrackingMiddleware(BaseMiddleware):
    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        if isinstance(event, Update):
            user = _extract_user(event)
            chat = _extract_chat(event)

            if user is not None:
                upsert_user(
                    telegram_id=user.id,
                    username=user.username,
                    first_name=user.first_name,
                    language_code=user.language_code,
                )

            if chat is not None and chat.type != "private":
                upsert_group(
                    chat_id=chat.id,
                    chat_type=chat.type,
                    title=chat.title,
                    username=chat.username,
                    initial_language_code=user.language_code if user else None,
                )

        return await handler(event, data)
