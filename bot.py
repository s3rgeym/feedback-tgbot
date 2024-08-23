import argparse
import asyncio
import fnmatch
import logging
import os
import re
from contextvars import ContextVar
from pathlib import Path
from urllib.parse import urlsplit

import aiosqlite
from aiogram import Bot, Dispatcher
from aiogram.filters import Command
from aiogram.types import (
    CallbackQuery,
    ErrorEvent,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)
from dotenv import load_dotenv

CWD = Path.cwd()

load_dotenv()


class NameSpace(argparse.Namespace):
    api_token: str
    owner_id: str
    verbosity: int


parser = argparse.ArgumentParser()
parser.add_argument(
    "-t",
    "--api-token",
    "--token",
    default=os.getenv("API_TOKEN"),
)
parser.add_argument(
    "-O",
    "--owner-id",
    "--owner",
    default=int(os.getenv("OWNER_ID", 0)),
)
parser.add_argument("-v", "--verbosity", action="count", default=0)
args = parser.parse_args(namespace=NameSpace())

logger = logging.getLogger(__name__)
lvl = max(logging.DEBUG, logging.WARNING - logging.DEBUG * args.verbosity)
logger.setLevel(level=lvl)

# Инициализация contextvars для хранения соединения с БД
db_connection_ctx: ContextVar[aiosqlite.Connection | None] = ContextVar(
    "db_connection",
    default=None,
)

# Инициализация бота и диспетчера
bot = Bot(token=args.api_token)
dp = Dispatcher()


# https://docs.aiogram.dev/en/latest/dispatcher/errors.html
@dp.error()
async def error_handler(event: ErrorEvent):
    logger.error("Error caused by %s", event.exception, exc_info=True)


def read_allowed_hosts() -> list[str]:
    """Читает список разрешенных хостов из файла."""
    return (CWD / "allowed_hosts.txt").read_text().splitlines()


def check_links(text: str, allowed_hosts: list[str]) -> bool:
    """Проверяет, содержит ли сообщение недопустимые ссылки."""
    links = re.findall(r"(https?://\S+)", text)
    for link in links:
        sp = urlsplit(link)
        for pat in allowed_hosts:
            if fnmatch.fnmatch(sp.hostname, pat):
                return False
    return True


def owner_keyboard(user_id: int) -> InlineKeyboardMarkup:
    """Создает клавиатуру с кнопками управления для владельца."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="👁️ Кто это?",
                    callback_data=f"whois_{user_id}",
                ),
                InlineKeyboardButton(
                    text="🚫 Бан",
                    callback_data=f"block_{user_id}",
                ),
            ],
        ]
    )


async def init_db() -> None:
    """Инициализирует базу данных, создавая необходимые таблицы."""
    logger.info("init database")
    connection = await aiosqlite.connect(CWD / "bot.db")
    db_connection_ctx.set(connection)
    await connection.execute(
        """
        CREATE TABLE IF NOT EXISTS message_senders (
            message_id INTEGER PRIMARY KEY,
            sender_id INTEGER,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    await connection.execute(
        """
        CREATE TABLE IF NOT EXISTS user_info (
            user_id INTEGER PRIMARY KEY,
            full_name TEXT,
            username TEXT,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    await connection.execute(
        """
        CREATE TABLE IF NOT EXISTS banned_users (
            user_id INTEGER PRIMARY KEY,
            banned_at DATETIME DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    await connection.commit()


async def save_message(message_id: int, sender_id: int) -> None:
    """Сохраняет сообщение в базе данных."""
    connection: aiosqlite.Connection = db_connection_ctx.get()
    await connection.execute(
        "INSERT INTO message_senders (message_id, sender_id) VALUES (?, ?)",
        (message_id, sender_id),
    )
    await connection.commit()


async def save_user_info(user_id: int, full_name: str, username: str) -> None:
    """Сохраняет или обновляет информацию о пользователе в базе данных."""
    connection: aiosqlite.Connection = db_connection_ctx.get()
    await connection.execute(
        """
        INSERT INTO user_info (user_id, full_name, username)
        VALUES (?, ?, ?)
        ON CONFLICT(user_id) DO UPDATE SET
            full_name=excluded.full_name,
            username=excluded.username,
            updated_at=CURRENT_TIMESTAMP
        """,
        (user_id, full_name, username),
    )
    await connection.commit()


async def get_message_sender(message_id: int) -> int | None:
    """Возвращает ID отправителя по ID сообщения."""
    connection: aiosqlite.Connection = db_connection_ctx.get()
    async with connection.execute(
        "SELECT sender_id FROM message_senders WHERE message_id = ?",
        (message_id,),
    ) as cursor:
        result = await cursor.fetchone()
        return result[0] if result else None


async def get_last_message_sender() -> int | None:
    """Возвращает ID отправителя по ID сообщения."""
    connection: aiosqlite.Connection = db_connection_ctx.get()
    async with connection.execute(
        "SELECT sender_id FROM message_senders ORDER BY ROWID DESC LIMIT 1"
    ) as cursor:
        result = await cursor.fetchone()
        return result[0] if result else None


async def get_user_info(user_id: int) -> tuple[str, str] | None:
    """Возвращает полное имя и юзернейм пользователя по его ID."""
    connection: aiosqlite.Connection = db_connection_ctx.get()
    async with connection.execute(
        "SELECT full_name, username FROM user_info WHERE user_id = ?",
        (user_id,),
    ) as cursor:
        return await cursor.fetchone()


async def check_user_banned(user_id: int) -> bool:
    """Проверяет, заблокирован ли пользователь."""
    connection: aiosqlite.Connection = db_connection_ctx.get()
    async with connection.execute(
        "SELECT COUNT(*) FROM banned_users WHERE user_id = ?", (user_id,)
    ) as cursor:
        result = await cursor.fetchone()
        return result[0] > 0


@dp.message(Command(commands=["start"]))
async def start(message: Message) -> None:
    """Обработчик команды /start, приветствует пользователя."""
    logger.info(f"command /start executed by user id: {message.from_user.id}")
    await message.answer("👋 Я бот для обратной связи с его владельцем.")


@dp.message(lambda message: message.from_user.id != args.owner_id)
async def handle_user_message(message: Message) -> None:
    """Обрабатывает текстовые сообщения и вложения от пользователей."""
    logger.debug(f"message from user #{message.from_user.id}")
    user_id = message.from_user.id

    if await check_user_banned(user_id):
        await bot.send_message(
            user_id, "🚫 Сообщение не было отправлено так как вы заблокированы."
        )
        return

    # if message.text and not check_links(message.text, read_allowed_hosts()):
    #     await bot.send_message(
    #         user_id, "🚫 Ваше сообщение содержит недопустимые ссылки."
    #     )
    #     return

    username = message.from_user.username
    full_name = message.from_user.full_name

    await save_user_info(user_id, full_name, username)

    keyboard = owner_keyboard(user_id)
    result = await bot.copy_message(
        args.owner_id,
        from_chat_id=message.chat.id,
        message_id=message.message_id,
        reply_markup=keyboard,
    )
    await save_message(result.message_id, user_id)
    await bot.send_message(
        args.owner_id,
        f"_Сообщение от {full_name}_",
        reply_to_message_id=result.message_id,
        parse_mode="markdown",
    )
    await message.answer("✅ Ваше сообщение отправлено, ждите ответа.")


@dp.message(lambda message: message.from_user.id == args.owner_id)
async def handle_owner_message(message: Message) -> None:
    """Обрабатывает сообщения от владельца и пересылает их соответствующим пользователям."""

    sender_id = None
    if message.reply_to_message:
        sender_id = await get_message_sender(
            message.reply_to_message.message_id
        )
    else:
        sender_id = await get_last_message_sender()

    logger.debug(f"reply to sender: {sender_id}")

    if sender_id:
        await bot.copy_message(
            sender_id,
            from_chat_id=message.chat.id,
            message_id=message.message_id,
        )
    else:
        await message.reply(
            "❗ Ошибка: не удалось найти пользователя, отправившего сообщение."
        )


@dp.callback_query(lambda c: c.data.startswith("block_"))
async def block_user(callback: CallbackQuery) -> None:
    """Обрабатывает запрос на блокировку пользователя."""
    user_id = int(callback.data.split("_")[1])
    if user_id:
        # Блокируем пользователя
        connection: aiosqlite.Connection = db_connection_ctx.get()
        await connection.execute(
            "INSERT INTO banned_users (user_id) VALUES (?) ON CONFLICT DO NOTHING",
            (user_id,),
        )
        await connection.commit()

        # Получаем информацию о пользователе
        user_info = await get_user_info(user_id)
        full_name, username = user_info if user_info else (None, None)

        # Уведомляем владельца
        keyboard = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text="Разблокировать",
                        callback_data=f"unblock_{user_id}",
                    )
                ],
            ]
        )
        await callback.message.answer(
            f"🚫 Пользователь {full_name} @{username} заблокирован.",
            reply_markup=keyboard,
        )

        # Уведомляем пользователя
        await bot.send_message(user_id, "🚫 Вы были заблокированы.")
    else:
        await callback.message.answer(
            "❗ Ошибка: не удалось найти пользователя."
        )


@dp.callback_query(lambda c: c.data.startswith("unblock_"))
async def unblock_user(callback: CallbackQuery) -> None:
    """Обрабатывает запрос на разблокировку пользователя."""
    user_id = int(callback.data.split("_")[1])

    # Разблокируем пользователя
    connection: aiosqlite.Connection = db_connection_ctx.get()

    await connection.execute(
        "DELETE FROM banned_users WHERE user_id = ?", (user_id,)
    )
    await connection.commit()

    # Получаем информацию о пользователе
    user_info = await get_user_info(user_id)
    full_name, username = user_info if user_info else (None, None)

    # Уведомляем владельца
    await callback.message.answer(
        f"✅ Пользователь {full_name} @{username} разблокирован."
    )

    # Уведомляем владельца
    await bot.send_message(
        user_id, "✅ Вы разблокированы и можете писать снова."
    )


@dp.callback_query(lambda c: c.data.startswith("whois_"))
async def whois(callback: CallbackQuery) -> None:
    """Обрабатывает запрос на просмотр информации о пользователе."""
    user_id = int(callback.data.split("_")[-1])
    if user_id:
        user_info = await get_user_info(user_id)
        if user_info:
            full_name, username = user_info
            await callback.message.answer(
                (
                    "👤 Информация о пользователе:\n\n"
                    f"ID:  #{user_id}\n"
                    f"Ник: @{username}\n"
                    f"Имя: {full_name}"
                )
            )
        else:
            await callback.message.answer(
                "❗ Ошибка: не удалось найти информацию о пользователе."
            )
    else:
        await callback.message.answer(
            "❗ Ошибка: не удалось найти отправителя."
        )


async def run() -> None:
    """Запускает бота."""
    await init_db()
    await dp.start_polling(bot)


if __name__ == "__main__":
    logging.basicConfig(level=logging.WARNING)
    asyncio.run(run())
