import argparse
import asyncio
import fnmatch
import logging
import os
import re
import random
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
    BotCommand,
)
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.exceptions import TelegramForbiddenError, TelegramBadRequest, TelegramAPIError 

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

# Определяем состояния для рассылки
class BroadcastStates(StatesGroup):
    waiting_for_message = State()


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
        if not any(
            fnmatch.fnmatch(urlsplit(link).hostname, pat)
            for pat in allowed_hosts
        ):
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
    """Возвращает ID отправителя последнего сообщения."""
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


async def get_all_user_ids() -> list[int]:
    """Возвращает список всех уникальных ID пользователей, с которыми бот взаимодействовал."""
    connection: aiosqlite.Connection = db_connection_ctx.get()
    async with connection.execute("SELECT user_id FROM user_info") as cursor:
        results = await cursor.fetchall()
        return [row[0] for row in results]


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
    logger.debug(f"Incoming message from user #{message.from_user.id}")
    user_id = message.from_user.id

    if await check_user_banned(user_id):
        return

    username = message.from_user.username
    full_name = message.from_user.full_name

    await save_user_info(user_id, full_name, username)

    await bot.send_message(
        args.owner_id,
        f"_Сообщение от {full_name} @{username}:_",
        parse_mode="markdown",
    )

    keyboard = owner_keyboard(user_id)
    result = await bot.copy_message(
        args.owner_id,
        from_chat_id=message.chat.id,
        message_id=message.message_id,
        reply_markup=keyboard,
    )

    await save_message(result.message_id, user_id)
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

    if sender_id:
        logger.debug(f"Send reply to sender #{sender_id}")

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
        connection: aiosqlite.Connection = db_connection_ctx.get()
        res = await connection.execute_insert(
            "INSERT INTO banned_users (user_id) VALUES (?) ON CONFLICT DO NOTHING",
            (user_id,),
        )
        
        if not res:
            logger.warning(f"Failed to ban user #{user_id}")
            return
        
        await connection.commit()

        user_info = await get_user_info(user_id)
        full_name, username = user_info if user_info else (None, None)

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

        await bot.send_message(user_id, "🚫 Вы были заблокированы.")
    else:
        await callback.message.answer(
            "❗ Ошибка: не удалось найти пользователя."
        )


@dp.callback_query(lambda c: c.data.startswith("unblock_"))
async def unblock_user(callback: CallbackQuery) -> None:
    """Обрабатывает запрос на разблокировку пользователя."""
    user_id = int(callback.data.split("_")[1])

    connection: aiosqlite.Connection = db_connection_ctx.get()

    await connection.execute(
        "DELETE FROM banned_users WHERE user_id = ?", (user_id,)
    )
    await connection.commit()

    user_info = await get_user_info(user_id)
    full_name, username = user_info if user_info else (None, None)

    await callback.message.answer(
        f"✅ Пользователь {full_name} @{username} разблокирован."
    )

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

@dp.message(Command(commands=["broadcast"]), lambda message: message.from_user.id == args.owner_id)
async def cmd_broadcast(message: Message, state: FSMContext) -> None:
    """
    Обработчик команды /broadcast.
    Переводит бота в состояние ожидания сообщения для рассылки.
    """
    logger.info(f"Owner {message.from_user.id} initiated broadcast.")
    await message.answer("Отправьте мне сообщение, которое вы хотите разослать всем пользователям. Оно будет отправлено со всеми вложениями. Для отмены используйте /cancel.")
    await state.set_state(BroadcastStates.waiting_for_message)


@dp.message(BroadcastStates.waiting_for_message, lambda message: message.from_user.id == args.owner_id)
async def process_broadcast_message(message: Message, state: FSMContext) -> None:
    """
    Обрабатывает сообщение от владельца в состоянии ожидания рассылки
    и рассылает его всем пользователям.
    """
    logger.info(f"Owner {message.from_user.id} sending broadcast message.")
    
    all_user_ids = await get_all_user_ids()
    users_to_broadcast = all_user_ids 

    await message.answer(
        f"Начинаю рассылку для **{len(users_to_broadcast)}** пользователей. "
        f"Между отправками будет пауза от 1.5 до 3.0 секунд." # <--- ОБНОВЛЕННЫЙ ДИАПАЗОН В ТЕКСТЕ
        f"\n\n_Сообщение будет отправлено._"
    )

    sent_count = 0
    blocked_by_bot_count = 0
    other_errors_count = 0

    for user_id in users_to_broadcast:
        try:
            await bot.copy_message(
                chat_id=user_id,
                from_chat_id=message.chat.id,
                message_id=message.message_id,
            )
            sent_count += 1
            logger.info(f"Successfully sent broadcast to user {user_id}")
            
            # Генерируем случайную задержку от 1.5 до 3.0 секунд
            sleep_time = random.uniform(1.5, 3.0) # <--- ОБНОВЛЕННЫЙ ДИАПАЗОН
            await asyncio.sleep(sleep_time)
            
        except TelegramForbiddenError:
            logger.warning(f"Failed to send broadcast to user {user_id}: Bot was blocked by the user.")
            blocked_by_bot_count += 1
        except (TelegramBadRequest, TelegramAPIError) as e:
            logger.error(f"Failed to send broadcast to user {user_id} due to API error: {e}")
            other_errors_count += 1
        except Exception as e:
            logger.critical(f"Unexpected error when sending broadcast to user {user_id}: {e}", exc_info=True)
            other_errors_count += 1

    await message.answer(
        f"Рассылка завершена!\n"
        f"✅ Отправлено: {sent_count}\n"
        f"🚫 Пропущено (заблокировали бота): {blocked_by_bot_count}\n"
        f"❌ Ошибки при отправке (прочие): {other_errors_count}"
    )
    await state.clear()
    logger.info(f"Broadcast finished. Sent: {sent_count}, Blocked by bot: {blocked_by_bot_count}, Other errors: {other_errors_count}")

@dp.message(Command(commands=["cancel"]), BroadcastStates.waiting_for_message, lambda message: message.from_user.id == args.owner_id)
async def cancel_broadcast(message: Message, state: FSMContext) -> None:
    """
    Обработчик команды /cancel во время ожидания сообщения для рассылки.
    Отменяет процесс рассылки.
    """
    await state.clear()
    await message.answer("Рассылка отменена.")
    logger.info(f"Owner {message.from_user.id} cancelled broadcast.")


async def set_default_commands(bot: Bot):
    """
    Устанавливает команды по умолчанию для бота.
    """
    commands = [
        BotCommand(command="start", description="Запустить бота"),
        BotCommand(command="broadcast", description="Сделать рассылку"),
    ]
    await bot.set_my_commands(commands)
    logger.info("Default commands set.")


async def run() -> None:
    """Запускает бота."""
    await init_db()
    await set_default_commands(bot)
    await dp.start_polling(bot)


if __name__ == "__main__":
    logging.basicConfig(level=logging.WARNING)
    asyncio.run(run())
