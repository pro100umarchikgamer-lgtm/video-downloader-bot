import os
import asyncio
from dotenv import load_dotenv
from aiogram import Bot, Dispatcher
from aiogram.filters import CommandStart
from aiogram.types import Message
from app.handlers.links import router as links_router

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
ADMIN_IDS = {
    int(x.strip())
    for x in os.getenv("ADMIN_IDS", "").split(",")
    if x.strip().isdigit()
}

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN не найден в .env")

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()
dp.include_router(links_router)

@dp.message(CommandStart())
async def start_handler(message: Message):
    if message.from_user.id in ADMIN_IDS:
        await message.answer(
            "👋 Привет, админ!\n\n"
            "Бот запущен и работает."
        )
    else:
        await message.answer(
            "👋 Привет!\n\n"
            "Бот работает."
        )

async def main():
    print("🤖 Бот запускается...")
    print(f"👤 ADMIN_IDS: {ADMIN_IDS}")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
