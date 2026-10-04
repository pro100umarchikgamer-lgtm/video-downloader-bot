"""Central user/group access checks for every ordinary-user route."""

from __future__ import annotations

from typing import Any, Awaitable, Callable

from aiogram import BaseMiddleware
from aiogram.types import CallbackQuery, Message, TelegramObject

from admin.db import get_group_locale, get_user_locale, is_group_blocked, is_user_blocked
from app.i18n import t


class UserAccessMiddleware(BaseMiddleware):
    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        user = getattr(event, "from_user", None)
        message = event.message if isinstance(event, CallbackQuery) else event
        chat = getattr(message, "chat", None)
        text = getattr(message, "text", "") or ""
        relevant_message = isinstance(event, CallbackQuery) or "http://" in text.lower() or "https://" in text.lower() or text.startswith(("/start", "/settings"))
        if user and is_user_blocked(user.id):
            if not relevant_message:
                return await handler(event, data)
            locale = get_user_locale(user.id, user.language_code)
            if isinstance(event, CallbackQuery):
                await event.answer(t(locale, "blocked"), show_alert=True)
            elif isinstance(event, Message):
                await event.answer(t(locale, "blocked"))
            return None
        if chat and chat.type != "private" and is_group_blocked(chat.id):
            if not relevant_message:
                return await handler(event, data)
            locale = get_group_locale(chat.id)
            if isinstance(event, CallbackQuery):
                await event.answer(t(locale, "group_disabled"), show_alert=True)
            elif isinstance(event, Message):
                await event.answer(t(locale, "group_disabled"))
            return None
        return await handler(event, data)
