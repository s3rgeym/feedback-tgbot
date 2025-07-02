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
from aiogram import Bot, Dispatcher, Router, F
from aiogram.filters import Command, StateFilter 
from aiogram.types import (
    CallbackQuery,
    ErrorEvent,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
    BotCommand,
    BotCommandScopeAllPrivateChats,
    BotCommandScopeChat,
)
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.exceptions import TelegramForbiddenError, TelegramBadRequest, TelegramAPIError
from aiogram.fsm.storage.memory import MemoryStorage


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
# Важно сохранить состояние
dp = Dispatcher(storage=MemoryStorage())

admin_router = Router()
admin_router.message.filter(F.from_user.id == args.owner_id, F.chat.type == "private")

user_router = Router()
user_router.message.filter(F.from_user.id != args.owner_id, F.chat.type == "private")


# Определяем состояния для разных режимов бота
class AdminStates(StatesGroup):
    # Состояние по умолчанию для админа (без активных спец. режимов)
    idle = State()
    # Админ вводит сообщение для массовой рассылки
    waiting_for_broadcast_message = State()
    # Бот находится в процессе рассылки
    broadcasting = State()


# https://docs.aiogram.dev/en/latest/dispatcher/errors.html
@dp.error()
async def error_handler(event: ErrorEvent):
    logger.error("Error caused by %s", event.exception, exc_info=True)


def read_allowed_hosts() -> list[str]:
    """Читает список разрешенных хостов из файла."""
    try:
        return (CWD / "allowed_hosts.txt").read_text().splitlines()
    except FileNotFoundError:
        logger.warning(f"File allowed_hosts.txt not found at {CWD}. No host filtering will be applied.")
        return []


def check_links(text: str, allowed_hosts: list[str]) -> bool:
    """Проверяет, содержит ли сообщение недопустимые ссылки."""
    if not allowed_hosts:
        return True

    links = re.findall(r"(https?://\S+)", text)
    for link in links:
        hostname = urlsplit(link).hostname
        if not any(
            fnmatch.fnmatch(hostname, pat)
            for pat in allowed_hosts
        ):
            return False
    return True


def actions_on_sender_keyboard(user_id: int) -> InlineKeyboardMarkup:
    """Создает клавиатуру с кнопками действий, которые владелец может применить к отправителю сообщения."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="ℹ️ Кто это?",
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
    logger.info("Initializing database...")
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
    logger.info("Database initialized.")


async def save_message(message_id: int, sender_id: int) -> None:
    """Сохраняет сообщение в базе данных."""
    connection: aiosqlite.Connection = db_connection_ctx.get()
    try:
        await connection.execute(
            "INSERT INTO message_senders (message_id, sender_id) VALUES (?, ?)",
            (message_id, sender_id),
        )
        await connection.commit()
    except aiosqlite.Error as e:
        logger.error(f"Error saving message {message_id} from sender {sender_id}: {e}", exc_info=True)


async def save_user_info(user_id: int, full_name: str, username: str) -> None:
    """Сохраняет или обновляет информацию о пользователе в базе данных."""
    connection: aiosqlite.Connection = db_connection_ctx.get()
    try:
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
    except aiosqlite.Error as e:
        logger.error(f"Error saving user info for user {user_id}: {e}", exc_info=True)


async def get_message_sender(message_id: int) -> int | None:
    """Возвращает ID отправителя по ID сообщения."""
    connection: aiosqlite.Connection = db_connection_ctx.get()
    try:
        async with connection.execute(
            "SELECT sender_id FROM message_senders WHERE message_id = ?",
            (message_id,),
        ) as cursor:
            result = await cursor.fetchone()
            return result[0] if result else None
    except aiosqlite.Error as e:
        logger.error(f"Error getting sender for message {message_id}: {e}", exc_info=True)
        return None


async def get_last_message_sender() -> int | None:
    """Возвращает ID отправителя последнего сообщения."""
    connection: aiosqlite.Connection = db_connection_ctx.get()
    try:
        async with connection.execute(
            "SELECT sender_id FROM message_senders ORDER BY ROWID DESC LIMIT 1"
        ) as cursor:
            result = await cursor.fetchone()
            return result[0] if result else None
    except aiosqlite.Error as e:
        logger.error(f"Error getting last message sender: {e}", exc_info=True)
        return None


async def get_user_info(user_id: int) -> tuple[str, str] | None:
    """Возвращает полное имя и юзернейм пользователя по его ID."""
    connection: aiosqlite.Connection = db_connection_ctx.get()
    try:
        async with connection.execute(
            "SELECT full_name, username FROM user_info WHERE user_id = ?",
            (user_id,),
        ) as cursor:
            return await cursor.fetchone()
    except aiosqlite.Error as e:
        logger.error(f"Error getting user info for user {user_id}: {e}", exc_info=True)
        return None


async def get_all_user_ids() -> list[int]:
    """Возвращает список всех уникальных ID пользователей, с которыми бот взаимодействовал."""
    connection: aiosqlite.Connection = db_connection_ctx.get()
    try:
        async with connection.execute("SELECT user_id FROM user_info") as cursor:
            results = await cursor.fetchall()
            return [row[0] for row in results]
    except aiosqlite.Error as e:
        logger.error(f"Error getting all user IDs: {e}", exc_info=True)
        return []


async def check_user_banned(user_id: int) -> bool:
    """Проверяет, заблокирован ли пользователь."""
    connection: aiosqlite.Connection = db_connection_ctx.get()
    try:
        async with connection.execute(
            "SELECT COUNT(*) FROM banned_users WHERE user_id = ?", (user_id,)
        ) as cursor:
            result = await cursor.fetchone()
            return result[0] > 0
    except aiosqlite.Error as e:
        logger.error(f"Error checking if user {user_id} is banned: {e}", exc_info=True)
        return False

# --- Обработчики команд и сообщений ---

# --- Обработчик /start для АДМИНА ---
@admin_router.message(Command(commands=["start"]))
async def admin_start(message: Message, state: FSMContext) -> None:
    """
    Обработчик команды /start для владельца бота.
    Сбрасывает состояние админа в idle.
    """
    logger.info(f"Admin {message.from_user.id} executed /start. Resetting state to idle.")
    await state.set_state(AdminStates.idle)
    await message.answer("👋 Привет, админ! Вы в обычном режиме.")


# --- Обработчик /start для ОБЫЧНЫХ ПОЛЬЗОВАТЕЛЕЙ ---
@user_router.message(Command(commands=["start"]))
async def user_start(message: Message) -> None:
    """
    Обработчик команды /start для обычного пользователя.
    """
    logger.info(f"User {message.from_user.id} executed /start.")
    await message.answer("👋 Я бот для обратной связи с его владельцем. Напишите мне сообщение, и я его перешлю.")


# Обработчик команды /broadcast (прямой доступ для владельца)
@admin_router.message(Command(commands=["broadcast"]))
async def cmd_broadcast(message: Message, state: FSMContext) -> None:
    """
    Обработчик команды /broadcast.
    Переводит бота в состояние ожидания сообщения для рассылки.
    """
    # Проверяем, не запущена ли уже рассылка
    current_state = await state.get_state()
    if current_state == AdminStates.broadcasting:
        await message.answer("⚠️ Рассылка уже активна. Дождитесь её завершения или отмените текущую рассылку командой /cancel.")
        return

    await state.set_state(AdminStates.waiting_for_broadcast_message)
    logger.info(f"Owner {message.from_user.id} initiated broadcast via /broadcast command.")
    await message.answer("✉️ Отправьте мне сообщение, которое вы хотите разослать всем пользователям. Оно будет отправлено со всеми вложениями. Для отмены используйте /cancel.")


@admin_router.message(Command(commands=["cancel"]))
async def cancel_admin_action(message: Message, state: FSMContext) -> None:
    """
    Обработчик команды /cancel, отменяет текущее админское действие
    и возвращает в idle.
    """
    logger.info("handle admin /cancel")
    await state.set_state(AdminStates.idle)
    await message.answer("✅ Действие отменено. Вы вернулись в обычный режим.")


# Обработчик сообщений для состояния рассылки (для владельца)
@admin_router.message(AdminStates.waiting_for_broadcast_message)
async def process_broadcast_message(message: Message, state: FSMContext) -> None:
    """
    Обрабатывает сообщение от владельца в состоянии ожидания рассылки
    и запускает рассылку.
    """
    logger.info(f"Owner {message.from_user.id} confirmed broadcast message.")

    # Запускаем рассылку напрямую, не в фоне
    await perform_broadcast(message, message.chat.id, state)

    logger.info("Broadcast task finished.")


async def perform_broadcast(message: Message, chat_id: int, state: FSMContext):
    """
    Выполняет массовую рассылку сообщений.
    """

    await state.set_state(AdminStates.broadcasting)

    users_to_broadcast = await get_all_user_ids()
    total_users = len(users_to_broadcast)
    sent_count = 0
    errors_count = 0

    progress_message = await bot.send_message(chat_id, "🚀 Начинаю рассылку...")

    for user_id in users_to_broadcast:
        # Пауза перед проверкой состояния и отправкой
        sleep_time = random.uniform(1.5, 3.0)
        await asyncio.sleep(sleep_time)

        # Проверка состояния после задержки, но до отправки
        current_state = await state.get_state()
        if current_state != AdminStates.broadcasting:
            logger.info(f"Broadcast stopped mid-loop for user {user_id} due to state change to {current_state}.")
            await progress_message.edit_text("🚫 Рассылка остановлена администратором.")
            await state.set_state(AdminStates.idle)
            return

        try:
            await bot.copy_message(
                chat_id=user_id,
                from_chat_id=chat_id,
                message_id=message.message_id,
            )
            logger.info(f"Successfully sent broadcast to user {user_id}")
            sent_count += 1
        except TelegramForbiddenError:
            logger.warning(f"Failed to send broadcast to user {user_id}: Bot was blocked by the user.")
            errors_count += 1
        except TelegramAPIError as e:
            logger.error(f"Failed to send broadcast to user {user_id} due to API error: {e}", exc_info=True)
            errors_count += 1
        except Exception as e:
            logger.error(f"An unexpected error occurred while sending broadcast to user {user_id}: {e}", exc_info=True)
            errors_count += 1
        await progress_message.edit_text(f"Отправлено: {sent_count}/{total_users}; Ошибок обнаружено: {errors_count}")

    await state.set_state(AdminStates.idle)
    await bot.send_message(chat_id, "✅ Рассылка завершена.")


@admin_router.message(StateFilter(None, AdminStates.idle))
async def handle_owner_message_general(message: Message, state: FSMContext) -> None:
    target_user_id = None

    if message.reply_to_message:
        target_user_id = await get_message_sender(message.reply_to_message.message_id)
    else:
        target_user_id = await get_last_message_sender()

    if not target_user_id:
        await message.reply("❌ Не могу найти последнего собеседника для пересылки.")
        return

    user_info = await get_user_info(target_user_id)
    user_display_name = user_info[0] if user_info else f"пользователю с ID {target_user_id}"

    try:
        await bot.copy_message(
            chat_id=target_user_id,
            from_chat_id=message.chat.id,
            message_id=message.message_id,
        )
        logger.debug(f"Successfully sent message from owner to user #{target_user_id}.")
        await message.answer(f"✅ Ваш ответ отправлен {user_display_name}.")
    except TelegramForbiddenError:
        logger.error(f"User {target_user_id} blocked bot. Cannot send message from owner.")
        await message.answer(f"❌ Ошибка: пользователь {user_display_name} заблокировал бота.", show_alert=True)
    except Exception as e:
        logger.error(f"Error sending message to #{target_user_id}: {e}", exc_info=True)
        await message.answer(f"❌ Произошла ошибка при отправке сообщения для пользователя {user_display_name}.")


@admin_router.callback_query(F.data.startswith("block_"))
async def block_user(callback: CallbackQuery) -> None:
    """Обрабатывает запрос на блокировку пользователя."""
    user_id = int(callback.data.split("_")[1])
    connection: aiosqlite.Connection = db_connection_ctx.get()

    async with connection.execute(
        "INSERT INTO banned_users (user_id) VALUES (?) ON CONFLICT DO NOTHING",
        (user_id,),
    ) as cursor:
        if cursor.rowcount == 0:
            await callback.answer("⚠️ Пользователь уже заблокирован.", show_alert=True)
            return

    await connection.commit()

    user_info = await get_user_info(user_id)
    full_name, username = user_info if user_info else (f"ID:{user_id}", 'N/A')

    # Создаем новую клавиатуру для разблокировки
    unblock_keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="✅ Разблокировать",
                    callback_data=f"unblock_{user_id}",
                )
            ],
        ]
    )

    # Редактируем исходное сообщение колбэка, чтобы изменить текст и кнопки
    try:
        await callback.message.edit_text(
            f"🚫 Пользователь **{full_name}** (@{username}) заблокирован.",
            parse_mode="Markdown", # Возвращаем Markdown для форматирования имени
            reply_markup=unblock_keyboard, # Устанавливаем новую клавиатуру
        )
    except TelegramBadRequest as e:
        if "message is not modified" in str(e):
            await callback.message.edit_reply_markup(reply_markup=unblock_keyboard)
        else:
            logger.error(f"Error editing message for block_user: {e}")
            await callback.answer("❌ Ошибка при обновлении сообщения.", show_alert=True)
            return

    try:
        await bot.send_message(user_id, "🚫 **Вы были заблокированы администратором.** Вы больше не можете отправлять сообщения этому боту. Если вы считаете, что это ошибка, пожалуйста, свяжитесь с администратором напрямую.", parse_mode="Markdown")
        logger.info(f"Sent ban notification to user {user_id}.")
    except TelegramForbiddenError:
        logger.info(f"Could not send ban notification to user {user_id}: Bot was blocked by the user.")
    except Exception as e:
        logger.error(f"Error sending ban notification to user {user_id}: {e}")

    await callback.answer("✅ Пользователь заблокирован.", show_alert=False)


@admin_router.callback_query(F.data.startswith("unblock_"))
async def unblock_user(callback: CallbackQuery) -> None:
    """Обрабатывает запрос на разблокировку пользователя."""
    user_id = int(callback.data.split("_")[1])

    connection: aiosqlite.Connection = db_connection_ctx.get()

    async with connection.execute(
        "DELETE FROM banned_users WHERE user_id = ?", (user_id,)
    ) as cursor:
        if cursor.rowcount == 0:
            await callback.answer("⚠️ Пользователь не был заблокирован.", show_alert=True)
            return

    await connection.commit()

    user_info = await get_user_info(user_id)
    full_name, username = user_info if user_info else (f"ID:{user_id}", 'N/A')

    # Редактируем исходное сообщение, удаляя кнопки
    try:
        await callback.message.edit_text(
            f"✅ Пользователь **{full_name}** (@{username}) разблокирован.",
            parse_mode="Markdown", # Возвращаем Markdown для форматирования имени
            reply_markup=None # Удаляем клавиатуру
        )
    except TelegramBadRequest as e:
        if "message is not modified" in str(e):
            await callback.message.edit_reply_markup(reply_markup=None)
        else:
            logger.error(f"Error editing message for unblock_user: {e}")
            await callback.answer("❌ Ошибка при обновлении сообщения.", show_alert=True)
            return

    try:
        await bot.send_message(
            user_id, "✅ **Вы разблокированы.** Теперь вы можете снова отправлять сообщения боту.", parse_mode="Markdown"
        )
        logger.info(f"Sent unban notification to user {user_id}.")
    except TelegramForbiddenError:
        logger.info(f"Could not send unban notification to user {user_id}: Bot was blocked by the user.")
    except Exception as e:
        logger.error(f"Error sending unban notification to user {user_id}: {e}")

    await callback.answer("✅ Пользователь разблокирован.", show_alert=False)


@admin_router.callback_query(F.data.startswith("whois_"))
async def whois(callback: CallbackQuery) -> None:
    """
    Обрабатывает запрос на просмотр информации о пользователе.
    """
    user_id = int(callback.data.split("_")[-1])
    if user_id:
        user_info = await get_user_info(user_id)
        if user_info:
            full_name, username = user_info
            await callback.message.answer(
                (
                    "ℹ️ Информация о пользователе:\n\n"
                    f"ID: `{user_id}`\n"
                    f"Ник: @{username if username else 'N/A'}\n"
                    f"Имя: {full_name}"
                ),
                parse_mode="Markdown"
            )
            await callback.answer(show_alert=False) # Просто закрываем всплывающее уведомление
        else:
            await callback.answer(
                "❌ Ошибка: не удалось найти информацию о пользователе.", show_alert=True
            )
    else:
        await callback.answer(
            "❌ Ошибка: не удалось найти отправителя.", show_alert=True
        )


# --- Обработчик сообщений от ОБЫЧНЫХ пользователей (используем user_router) ---

@user_router.message()
async def handle_user_message(message: Message) -> None:
    """Обрабатывает текстовые сообщения и вложения от пользователей."""
    logger.debug(f"Incoming message from user #{message.from_user.id}")
    user_id = message.from_user.id

    if await check_user_banned(user_id):
        logger.info(f"Blocked user {user_id} tried to send a message.")
        try:
            await message.answer("🚫 Вы не можете отправить сообщение, так как были забанены администратором.")
        except TelegramForbiddenError:
            logger.info(f"Could not notify user {user_id} about ban (already blocked bot).")
        except Exception as e:
            logger.error(f"Error sending ban message to user {user_id}: {e}")
        return

    username = message.from_user.username
    full_name = message.from_user.full_name

    # Проверка ссылок
    allowed_hosts = read_allowed_hosts()
    if message.text and not check_links(message.text, allowed_hosts):
        logger.warning(f"User {user_id} sent a message with disallowed links.")
        try:
            await message.answer("❌ Ваше сообщение содержит ссылки на запрещенные ресурсы и не может быть отправлено.")
        except TelegramForbiddenError:
            logger.info(f"Could not notify user {user_id} about disallowed links (user blocked bot).")
        except Exception as e:
            logger.error(f"Error notifying user {user_id} about disallowed links: {e}")
        return


    await save_user_info(user_id, full_name, username)

    # Убираем Markdown для сообщения админу, чтобы не конфликтовать со спецсимволами в имени
    await bot.send_message(
        args.owner_id,
        f"✉️ Сообщение от {full_name} @{username if username else 'N/A'}:",
        # parse_mode="Markdown", # Убрано
    )

    keyboard = actions_on_sender_keyboard(user_id)
    try:
        result = await bot.copy_message(
            args.owner_id,
            from_chat_id=message.chat.id,
            message_id=message.message_id,
            reply_markup=keyboard,
        )
        await save_message(result.message_id, user_id)
        try:
            await message.answer("✅ Ваше сообщение отправлено, ждите ответа.")
        except TelegramForbiddenError:
            logger.info(f"Could not send confirmation to user {user_id}: Bot was blocked by the user after message was forwarded.")
    except TelegramForbiddenError:
        logger.error(f"Owner {args.owner_id} blocked bot. Cannot forward message from user {user_id}.")
        await message.answer("❌ Ваше сообщение не может быть доставлено, так как владелец бота недоступен.")
    except Exception as e:
        logger.error(f"Error forwarding message from user {user_id} to owner {args.owner_id}: {e}", exc_info=True)
        await message.answer("❌ Произошла ошибка при отправке вашего сообщения. Попробуйте позже.")


async def set_commands_for_admin(bot: Bot, owner_id: int):
    """
    Устанавливает команды для владельца бота.
    Эти команды будут видны только в его приватном чате.
    """
    admin_commands = [
        BotCommand(command="start", description="👋 Запустить бота"),
        BotCommand(command="broadcast", description="✉️ Сделать рассылку"),
        BotCommand(command="cancel", description="🚫 Отменить действие"),
    ]
    await bot.set_my_commands(
        commands=admin_commands,
        scope=BotCommandScopeChat(chat_id=owner_id)
    )
    logger.info(f"Admin commands set for owner_id: {owner_id}")

async def set_commands_for_users(bot: Bot):
    """
    Устанавливает команды для обычных пользователей.
    Эти команды будут видны во всех приватных чатах (кроме админа).
    """
    user_commands = [
        BotCommand(command="start", description="👋 Начать общение"),
    ]
    await bot.set_my_commands(
        commands=user_commands,
        scope=BotCommandScopeAllPrivateChats()
    )
    logger.info("User commands set for all private chats.")

async def run() -> None:
    """Запускает бота."""
    await init_db()
    await set_commands_for_admin(bot, args.owner_id)
    await set_commands_for_users(bot)

    dp.include_router(admin_router)
    dp.include_router(user_router)

    await dp.start_polling(bot)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(run())
