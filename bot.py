import asyncio
import os

from dotenv import load_dotenv

load_dotenv()

from aiogram import Bot, Dispatcher
from aiogram.filters import CommandStart
from aiogram.types import Message

from app.handlers.links import router as links_router
from worker.worker import worker_loop


BOT_TOKEN = os.getenv("BOT_TOKEN")

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN не найден в .env")


ADMIN_IDS = {
    int(x.strip())
    for x in os.getenv("ADMIN_IDS", "").split(",")
    if x.strip()
}


bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

dp.include_router(links_router)


@dp.message(CommandStart())
async def start_handler(message: Message):
    if message.from_user and message.from_user.id in ADMIN_IDS:
        await message.answer(
            "Привет, админ!\n\n"
            "Бот запущен и работает."
        )
    else:
        await message.answer(
            "Привет!\n\n"
            "Отправь мне ссылку на видео."
        )


async def main():
    queue_workers = int(os.getenv("QUEUE_WORKERS", "2"))

    queue_tasks = [
        asyncio.create_task(worker_loop(bot))
        for _ in range(queue_workers)
    ]

    try:
        await dp.start_polling(bot)
    finally:
        for task in queue_tasks:
            task.cancel()

        await asyncio.gather(
            *queue_tasks,
            return_exceptions=True
        )


if __name__ == "__main__":
    asyncio.run(main())
