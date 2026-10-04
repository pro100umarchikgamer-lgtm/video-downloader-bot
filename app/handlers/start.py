from aiogram import Router
from aiogram.filters import CommandStart
from aiogram.types import Message

from admin.db import get_group_locale, get_user_locale
from app.i18n import t

router = Router(name="start")


@router.message(CommandStart())
async def start_handler(message: Message) -> None:
    if not message.from_user:
        return
    locale = (
        get_user_locale(message.from_user.id, message.from_user.language_code)
        if message.chat.type == "private"
        else get_group_locale(message.chat.id)
    )
    await message.answer(t(locale, "start"))
