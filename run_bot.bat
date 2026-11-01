@echo off
REM Автозапуск мафия-бота. Запускайте двойным кликом или из cmd.

cd /d "%~dp0"

REM Токен бота (из BotFather)
set "BOT_TOKEN=8546215865:AAFx2l3B0H6qTK-HB3Xhp2vkCpU6GefbUQU"

REM Создать venv, если нет
if not exist ".venv\Scripts\python.exe" (
    python -m venv .venv
)

REM Установка зависимостей
.\.venv\Scripts\python.exe -m pip install -r requirements.txt

REM Сброс вебхука на случай конфликта getUpdates
.\.venv\Scripts\python.exe -c "import os, asyncio; from aiogram import Bot; async def main(): bot=Bot(os.getenv('BOT_TOKEN')); await bot.delete_webhook(drop_pending_updates=True); await bot.session.close(); asyncio.run(main())"

REM Запуск бота (polling)
.\.venv\Scripts\python.exe bot.py

pause

