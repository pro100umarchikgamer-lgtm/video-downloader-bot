import logging

from aiogram import Router
from aiogram.types import ErrorEvent

from app.middleware import UserAccessMiddleware

user_router = Router(name="user")
user_router.message.outer_middleware(UserAccessMiddleware())
user_router.callback_query.outer_middleware(UserAccessMiddleware())

logger = logging.getLogger(__name__)


@user_router.errors()
async def user_error_handler(event: ErrorEvent) -> bool:
    exc = event.exception
    logger.error(
        "User handler error",
        exc_info=(type(exc), exc, exc.__traceback__),
    )
    callback = getattr(event.update, "callback_query", None)
    if callback:
        try:
            from admin.db import get_user_locale
            from app.i18n import t
            locale = get_user_locale(callback.from_user.id, callback.from_user.language_code)
            await callback.answer(t(locale, "stale_action"))
        except Exception:
            pass
    return True

from app.handlers import callbacks, links, settings, start  # noqa: E402

user_router.include_router(start.router)
user_router.include_router(settings.router)
user_router.include_router(links.router)
user_router.include_router(callbacks.router)
