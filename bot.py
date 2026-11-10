import asyncio
import hashlib
import html
import json
import logging
import os
import time
from typing import Optional

import aiosqlite
from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command, CommandStart
from aiogram.types import (
    Message,
    CallbackQuery,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
)
from dotenv import load_dotenv

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
OWNER_ID = int(os.getenv("OWNER_ID", "0"))
GROUP_ID = int(os.getenv("GROUP_ID", "0"))
DB_PATH = os.getenv("DB_PATH", "bot.db")

MODERATOR_IDS = {
    int(value.strip())
    for value in os.getenv("MODERATOR_IDS", "").split(",")
    if value.strip().isdigit()
}
if OWNER_ID:
    MODERATOR_IDS.add(OWNER_ID)

COOLDOWN_SECONDS = 15

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("anonymous_bot")

if not BOT_TOKEN:
    raise RuntimeError("Set BOT_TOKEN in the .env file.")
if not OWNER_ID:
    raise RuntimeError("Set OWNER_ID in the .env file.")

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

# Состояние режима отправки хранится в памяти. После перезапуска пользователь
# снова увидит главное меню и должен нажать кнопку отправки.
waiting_for_message: set[int] = set()

MAIN_MENU_TEXT = (
    "Привет! Здесь можно отправить анонимное сообщение в канал.\n\n"
    "Нажми кнопку ниже, чтобы начать."
)
SEND_PROMPT = "Напиши сообщение или отправь медиа. Оно будет передано на проверку."
SENT_TEXT = "✅ Сообщение отправлено на проверку."


def main_menu_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(
            text="✉️ Отправить анонимное сообщение",
            callback_data="send:start",
        )]
    ])


def consent_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(
            text="Ознакомился(ась), продолжить",
            callback_data="consent:yes",
        )]
    ])


async def show_main_menu(target: Message) -> None:
    await target.answer(MAIN_MENU_TEXT, reply_markup=main_menu_keyboard())


async def init_db() -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                username TEXT,
                first_name TEXT,
                last_name TEXT,
                consent INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS blocked (
                user_id INTEGER PRIMARY KEY,
                blocked_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                group_message_id INTEGER UNIQUE,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS anti_spam (
                user_id INTEGER PRIMARY KEY,
                last_sent_at REAL NOT NULL DEFAULT 0,
                last_hash TEXT
            )
        """)
        await db.commit()


async def save_user(user) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            INSERT INTO users (user_id, username, first_name, last_name)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                username=excluded.username,
                first_name=excluded.first_name,
                last_name=excluded.last_name
        """, (user.id, user.username, user.first_name, user.last_name))
        await db.commit()


async def has_consent(user_id: int) -> bool:
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "SELECT consent FROM users WHERE user_id = ?", (user_id,)
        )
        row = await cur.fetchone()
        return bool(row and row[0])


async def set_consent(user_id: int) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE users SET consent = 1 WHERE user_id = ?", (user_id,)
        )
        await db.commit()


async def is_blocked(user_id: int) -> bool:
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "SELECT 1 FROM blocked WHERE user_id = ?", (user_id,)
        )
        return await cur.fetchone() is not None


async def set_blocked(user_id: int, blocked: bool) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        if blocked:
            await db.execute(
                "INSERT OR IGNORE INTO blocked (user_id) VALUES (?)", (user_id,)
            )
        else:
            await db.execute(
                "DELETE FROM blocked WHERE user_id = ?", (user_id,)
            )
        await db.commit()


async def create_message(user_id: int) -> int:
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "INSERT INTO messages (user_id) VALUES (?)", (user_id,)
        )
        await db.commit()
        return int(cur.lastrowid)


async def delete_message(internal_id: int) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("DELETE FROM messages WHERE id = ?", (internal_id,))
        await db.commit()


async def set_group_message(internal_id: int, group_message_id: int) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE messages SET group_message_id = ? WHERE id = ?",
            (group_message_id, internal_id),
        )
        await db.commit()


async def get_message_sender(internal_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("""
            SELECT m.id, u.user_id, u.username, u.first_name, u.last_name
            FROM messages m
            JOIN users u ON u.user_id = m.user_id
            WHERE m.id = ?
        """, (internal_id,))
        return await cur.fetchone()


async def get_recipient(group_message_id: int) -> Optional[int]:
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "SELECT user_id FROM messages WHERE group_message_id = ?",
            (group_message_id,),
        )
        row = await cur.fetchone()
        return int(row[0]) if row else None


async def get_stats():
    async with aiosqlite.connect(DB_PATH) as db:
        users = await (await db.execute("SELECT COUNT(*) FROM users")).fetchone()
        messages = await (await db.execute("SELECT COUNT(*) FROM messages")).fetchone()
        blocked = await (await db.execute("SELECT COUNT(*) FROM blocked")).fetchone()
        return users[0], messages[0], blocked[0]


def is_owner(user_id: int) -> bool:
    return user_id == OWNER_ID


def message_fingerprint(message: Message) -> str:
    """Отпечаток содержимого для защиты от повторной отправки."""
    parts = [message.content_type]
    text = message.text or message.caption or ""
    parts.append(text.strip())
    media_attrs = (
        "photo", "video", "document", "audio", "voice",
        "video_note", "animation", "sticker",
    )
    for attr in media_attrs:
        item = getattr(message, attr, None)
        if item:
            if attr == "photo" and isinstance(item, list):
                item = item[-1] if item else None
            unique_id = getattr(item, "file_unique_id", None)
            if unique_id:
                parts.append(f"{attr}:{unique_id}")
    # Учитываем контакт/геопозицию и другие типы без file_unique_id.
    if len(parts) == 2 and not text:
        try:
            payload = message.model_dump(
                exclude={"message_id", "date", "chat", "from_user"}
            )
            parts.append(json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str))
        except Exception:
            parts.append(message.content_type)
    raw = "\x1f".join(parts)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


async def check_antispam(user_id: int, fingerprint: str):
    """Возвращает (разрешено, причина, оставшиеся_секунды)."""
    now = time.time()
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "SELECT last_sent_at, last_hash FROM anti_spam WHERE user_id = ?",
            (user_id,),
        )
        row = await cur.fetchone()
        if row:
            last_sent_at, last_hash = row
            if last_hash and last_hash == fingerprint:
                return False, "duplicate", 0
            remaining = COOLDOWN_SECONDS - (now - float(last_sent_at))
            if remaining > 0:
                return False, "cooldown", int(remaining) + 1
        return True, "", 0


async def record_antispam(user_id: int, fingerprint: str) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            INSERT INTO anti_spam (user_id, last_sent_at, last_hash)
            VALUES (?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                last_sent_at=excluded.last_sent_at,
                last_hash=excluded.last_hash
        """, (user_id, time.time(), fingerprint))
        await db.commit()


@dp.message(CommandStart(), F.chat.type == "private")
async def start(message: Message):
    if not message.from_user:
        return
    await save_user(message.from_user)
    waiting_for_message.discard(message.from_user.id)

    if await is_blocked(message.from_user.id):
        await message.answer("Доступ к отправке сообщений ограничен.")
        return

    if await has_consent(message.from_user.id):
        await show_main_menu(message)
        return

    await message.answer(
        "Привет! Для начала ознакомься с правилами.\n\n"
        "• Не вставляй ссылки и юзернеймы — такие сообщения не публикуются.\n"
        "• Никакой рекламы — такие сообщения тоже не проходят.\n"
        "• Не спамь — всё дойдёт, просто иногда с задержкой.\n"
        "• Не оскорбляй учителей — такое мы не одобряем.\n"
        "• Не комментируй посты в анонимных сообщениях — для этого есть комментарии.\n"
        "• Администрация в праве не публиковать сообщение без объяснений.\n\n"
        "Нажимая кнопку, ты подтверждаешь, что ознакомился(ась) с правилами.",
        reply_markup=consent_keyboard(),
    )


@dp.callback_query(F.data == "consent:yes")
async def consent(callback: CallbackQuery):
    await save_user(callback.from_user)
    await set_consent(callback.from_user.id)
    await callback.answer("Готово")
    if callback.message:
        await show_main_menu(callback.message)


@dp.callback_query(F.data == "send:start")
async def begin_sending(callback: CallbackQuery):
    user_id = callback.from_user.id
    if await is_blocked(user_id):
        await callback.answer("Отправка для вас ограничена.", show_alert=True)
        return
    if not await has_consent(user_id):
        await callback.answer("Сначала ознакомьтесь с правилами через /start.", show_alert=True)
        return
    waiting_for_message.add(user_id)
    await callback.answer()
    if callback.message:
        await callback.message.answer(SEND_PROMPT)


@dp.message(Command("admin"), F.chat.type == "private")
async def admin_panel(message: Message):
    if not message.from_user or not is_owner(message.from_user.id):
        return
    waiting_for_message.discard(message.from_user.id)
    users, messages, blocked = await get_stats()
    await message.answer(
        "⚙️ <b>Панель владельца</b>\n\n"
        f"Пользователей: {users}\n"
        f"Сообщений: {messages}\n"
        f"Заблокировано: {blocked}\n\n"
        "Команды:\n"
        "/block ID — заблокировать\n"
        "/unblock ID — разблокировать\n"
        "/stats — статистика\n"
        "/start — главное меню",
        parse_mode="HTML",
    )


@dp.message(Command("stats"), F.chat.type == "private")
async def stats(message: Message):
    if not message.from_user or not is_owner(message.from_user.id):
        return
    waiting_for_message.discard(message.from_user.id)
    users, messages, blocked = await get_stats()
    await message.answer(
        f"Пользователей: {users}\nСообщений: {messages}\nБлокировок: {blocked}"
    )


@dp.message(Command("block"), F.chat.type == "private")
async def block_command(message: Message):
    if not message.from_user or not is_owner(message.from_user.id):
        return
    waiting_for_message.discard(message.from_user.id)
    parts = (message.text or "").split()
    if len(parts) != 2 or not parts[1].isdigit():
        await message.answer("Использование: /block TELEGRAM_ID")
        return
    await set_blocked(int(parts[1]), True)
    await message.answer("Пользователь заблокирован.")


@dp.message(Command("unblock"), F.chat.type == "private")
async def unblock_command(message: Message):
    if not message.from_user or not is_owner(message.from_user.id):
        return
    waiting_for_message.discard(message.from_user.id)
    parts = (message.text or "").split()
    if len(parts) != 2 or not parts[1].isdigit():
        await message.answer("Использование: /unblock TELEGRAM_ID")
        return
    await set_blocked(int(parts[1]), False)
    await message.answer("Пользователь разблокирован.")


@dp.message(Command("groupid"), F.chat.type.in_({"group", "supergroup"}))
async def get_group_id(message: Message):
    if message.from_user and is_owner(message.from_user.id):
        await message.reply(
            f"ID этой группы: <code>{message.chat.id}</code>",
            parse_mode="HTML",
        )


@dp.message(F.chat.type == "private")
async def receive_private_message(message: Message):
    if not message.from_user or message.from_user.is_bot:
        return
    user_id = message.from_user.id
    await save_user(message.from_user)

    # Любые команды не считаются анонимными сообщениями.
    if message.text and message.text.startswith("/"):
        if user_id == OWNER_ID:
            await message.answer("Неизвестная команда. Используй /start.")
        return

    if not await has_consent(user_id):
        await message.answer("Сначала отправь /start и ознакомься с правилами.")
        return
    if await is_blocked(user_id):
        await message.answer("Вы были заблокированы, отправка невозможна.")
        return
    if user_id not in waiting_for_message:
        await show_main_menu(message)
        return
    if GROUP_ID == 0:
        await message.answer("Бот ещё настраивается. Попробуй позже.")
        waiting_for_message.discard(user_id)
        await show_main_menu(message)
        return

    fingerprint = message_fingerprint(message)
    allowed, reason, remaining = await check_antispam(user_id, fingerprint)
    if not allowed:
        waiting_for_message.discard(user_id)
        if reason == "duplicate":
            await message.answer("Это сообщение уже отправлялось, не спамь.")
        else:
            await message.answer(
                f"Не спамь, подожди ещё {remaining} сек."
            )
        await show_main_menu(message)
        return

    internal_id = await create_message(user_id)
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(
            text="Информация об отправителе",
            callback_data=f"whois:{internal_id}",
        )]
    ])

    try:
        if message.text:
            safe_text = html.escape(message.text)
            sent_to_group = await bot.send_message(
                chat_id=GROUP_ID,
                text=(
                    "<b>🔔 Новое сообщение</b>\n\n"
                    f"<blockquote>{safe_text}</blockquote>"
                ),
                parse_mode="HTML",
                reply_markup=keyboard,
            )

        elif message.photo:
            caption = (
                "<b>🔔 Новое сообщение</b>\n\n"
                f"<blockquote>{html.escape(message.caption or '')}</blockquote>"
            )
            sent_to_group = await bot.send_photo(
                chat_id=GROUP_ID,
                photo=message.photo[-1].file_id,
                caption=caption,
                parse_mode="HTML",
                reply_markup=keyboard,
            )

        elif message.video:
            caption = (
                "<b>🔔 Новое сообщение</b>\n\n"
                f"<blockquote>{html.escape(message.caption or '')}</blockquote>"
            )
            sent_to_group = await bot.send_video(
                chat_id=GROUP_ID,
                video=message.video.file_id,
                caption=caption,
                parse_mode="HTML",
                reply_markup=keyboard,
            )

        else:
            copied = await bot.copy_message(
                chat_id=GROUP_ID,
                from_chat_id=message.chat.id,
                message_id=message.message_id,
                reply_markup=keyboard,
            )
            sent_to_group = copied

        await set_group_message(internal_id, sent_to_group.message_id)
        await record_antispam(user_id, fingerprint)
        waiting_for_message.discard(user_id)
        await message.answer(SENT_TEXT)
        await show_main_menu(message)

    except Exception:
        await delete_message(internal_id)
        logger.exception("Could not copy incoming message")
        await message.answer(
            "Не получилось отправить сообщение. Попробуй ещё раз."
        )
        # Оставляем режим отправки включённым, чтобы пользователь мог повторить.


@dp.callback_query(F.data.startswith("whois:"))
async def show_sender(callback: CallbackQuery):
    if not is_owner(callback.from_user.id):
        await callback.answer("Доступ запрещён.", show_alert=True)
        return
    if not callback.message or callback.message.chat.id != GROUP_ID:
        await callback.answer("Эта кнопка работает только в группе бота.")
        return
    try:
        internal_id = int(callback.data.split(":", 1)[1])
        row = await get_message_sender(internal_id)
        if not row:
            await callback.answer("Запись не найдена.")
            return
        _, user_id, username, first_name, last_name = row
        username_text = f"@{html.escape(username)}" if username else "не указан"
        name_text = html.escape(" ".join(
            part for part in [first_name, last_name] if part
        )) or "не указано"
        await bot.send_message(
            OWNER_ID,
            "<b>Данные отправителя</b>\n"
            f"Сообщение №{internal_id}\n"
            f"Имя: {name_text}\n"
            f"Username: {username_text}\n"
            f"Telegram ID: <code>{user_id}</code>",
            parse_mode="HTML",
        )
        await callback.answer("Данные отправлены.")
    except (ValueError, TypeError):
        await callback.answer("Некорректный номер сообщения.")
    except Exception:
        logger.exception("Could not show sender")
        await callback.answer("Не удалось получить данные.")


@dp.message(F.chat.type.in_({"group", "supergroup"}), F.reply_to_message)
async def moderator_reply(message: Message):
    if not message.from_user or message.from_user.id not in MODERATOR_IDS:
        return
    if GROUP_ID == 0 or message.chat.id != GROUP_ID:
        return
    if message.text and message.text.startswith("/"):
        return

    recipient_id = await get_recipient(message.reply_to_message.message_id)
    if not recipient_id:
        return
    try:
        await bot.copy_message(
            chat_id=recipient_id,
            from_chat_id=message.chat.id,
            message_id=message.message_id,
        )
        await message.reply("Ответ отправлен.")
    except Exception:
        logger.exception("Could not deliver moderator reply")
        await message.reply("Не удалось отправить ответ.")


@dp.message(Command("help"), F.chat.type == "private")
async def help_command(message: Message):
    await message.answer(
        "Команды:\n"
        "/start — открыть главное меню\n"
        "/help — помощь\n\n"
        "Чтобы отправить сообщение, нажми кнопку «Отправить анонимное сообщение»."
    )


async def main():
    await init_db()
    logger.info("Bot is starting")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
