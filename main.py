"""
Анонимные сообщения — мульти-чат версия.
Любой владелец группы добавляет бота к себе, а пользователи выбирают,
в какой чат писать анонимно.
"""
import asyncio
import html
import logging
import os
import random
import sys
import time

import aiosqlite
from dotenv import load_dotenv
from aiogram import Bot, Dispatcher, F, types
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ChatMemberStatus, ChatType, ParseMode
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.types import (
    BotCommand, BotCommandScopeAllGroupChats, BotCommandScopeAllPrivateChats,
    ChatMemberUpdated, InlineKeyboardButton, InlineKeyboardMarkup,
    KeyboardButton, ReplyKeyboardMarkup,
)

load_dotenv()

# ================= НАСТРОЙКИ =================
BOT_TOKEN = os.getenv("BOT_TOKEN")
OWNER_ID = int(os.getenv("OWNER_ID", "0") or 0)   # владелец бота (глобальный админ)
DB_FILE = os.getenv("DB_FILE", "anon_multi.db")
MEMBER_CACHE_TTL = 600                              # кэш проверки участия, сек

if not BOT_TOKEN:
    sys.exit("❌ BOT_TOKEN не найден в .env")

ADJECTIVES = ["Дикий", "Грустный", "Веселый", "Хитрый", "Мощный", "Сонный", "Быстрый",
              "Ленивый", "Безумный", "Четкий", "Скрытный", "Лютый", "Нежный", "Злой",
              "Добрый", "Сильный", "Умный", "Смешной"]
NOUNS = ["Енот", "Пингвин", "Тапок", "Хомяк", "Пельмень", "Маг", "Ниндзя", "Кактус", "Бобр",
         "Слон", "Призрак", "Батон", "Рыцарь", "Кот", "Пес", "Волк", "Тигр", "Лев"]

RANDOM_NICK_BTN = "🎲 Случайный ник"
MY_CHATS_BTN = "💬 Мои чаты"

logging.basicConfig(level=logging.INFO, stream=sys.stdout)
bot = Bot(BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher()
db: aiosqlite.Connection = None  # type: ignore
BOT_USERNAME = ""

last_message_time: dict[tuple[int, int], float] = {}
member_cache: dict[tuple[int, int], float] = {}


# ================= БАЗА ДАННЫХ =================
SCHEMA = """
PRAGMA journal_mode=WAL;
CREATE TABLE IF NOT EXISTS chats(
    chat_id    INTEGER PRIMARY KEY,
    title      TEXT,
    thread_id  INTEGER,             -- тема форума (NULL = общий)
    added_by   INTEGER,
    is_active  INTEGER DEFAULT 1,   -- бот в чате
    is_enabled INTEGER DEFAULT 1,   -- админ может выключить анонимку
    allow_media INTEGER DEFAULT 1,
    cooldown   INTEGER DEFAULT 3,
    created_at INTEGER
);
CREATE TABLE IF NOT EXISTS users(
    user_id INTEGER PRIMARY KEY,
    nickname TEXT,
    current_chat_id INTEGER,
    global_ban INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS user_chats(      -- куда пользователь уже заходил
    user_id INTEGER, chat_id INTEGER,
    PRIMARY KEY(user_id, chat_id)
);
CREATE TABLE IF NOT EXISTS bans(
    chat_id INTEGER, user_id INTEGER,
    PRIMARY KEY(chat_id, user_id)
);
CREATE TABLE IF NOT EXISTS message_logs(
    chat_id INTEGER, group_message_id INTEGER,
    user_id INTEGER, user_message_id INTEGER,
    created_at INTEGER,
    PRIMARY KEY(chat_id, group_message_id)
);
CREATE INDEX IF NOT EXISTS idx_logs_user ON message_logs(user_id, user_message_id);
CREATE TABLE IF NOT EXISTS reply_context(
    user_id INTEGER, dm_message_id INTEGER,
    chat_id INTEGER, group_message_id INTEGER,
    PRIMARY KEY(user_id, dm_message_id)
);
"""


async def db_start():
    global db
    db = await aiosqlite.connect(DB_FILE)
    await db.executescript(SCHEMA)
    await db.commit()


async def fetchone(sql, args=()):
    async with db.execute(sql, args) as cur:
        return await cur.fetchone()


async def fetchall(sql, args=()):
    async with db.execute(sql, args) as cur:
        return await cur.fetchall()


async def execute(sql, args=()):
    await db.execute(sql, args)
    await db.commit()


async def upsert_chat(chat: types.Chat, added_by: int | None):
    await execute(
        """INSERT INTO chats(chat_id, title, added_by, is_active, created_at)
           VALUES(?,?,?,1,?)
           ON CONFLICT(chat_id) DO UPDATE SET title=excluded.title, is_active=1""",
        (chat.id, chat.title, added_by, int(time.time())),
    )


async def get_chat(chat_id):
    return await fetchone(
        "SELECT chat_id, title, thread_id, is_active, is_enabled, allow_media, cooldown "
        "FROM chats WHERE chat_id=?", (chat_id,))


async def get_user(user_id):
    return await fetchone(
        "SELECT nickname, current_chat_id, global_ban FROM users WHERE user_id=?", (user_id,))


async def ensure_user(user_id):
    await execute("INSERT OR IGNORE INTO users(user_id) VALUES(?)", (user_id,))


async def is_banned(chat_id, user_id):
    return await fetchone("SELECT 1 FROM bans WHERE chat_id=? AND user_id=?", (chat_id, user_id)) is not None


# ================= ХЕЛПЕРЫ =================
def deep_link(chat_id: int) -> str:
    return f"https://t.me/{BOT_USERNAME}?start=c{chat_id}"


def add_to_group_link() -> str:
    return (f"https://t.me/{BOT_USERNAME}?startgroup=anon"
            f"&admin=delete_messages+pin_messages")


def main_kb():
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text=RANDOM_NICK_BTN), KeyboardButton(text=MY_CHATS_BTN)]],
        resize_keyboard=True)


def write_button(chat_id):
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✉️ Написать анонимно", url=deep_link(chat_id))]])


async def is_group_admin(chat_id, user_id) -> bool:
    if user_id == OWNER_ID:
        return True
    try:
        m = await bot.get_chat_member(chat_id, user_id)
        return m.status in (ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.CREATOR)
    except TelegramBadRequest:
        return False


async def is_member(chat_id, user_id) -> bool:
    """Писать можно только в чат, где ты состоишь — иначе бота используют для спама."""
    key = (chat_id, user_id)
    if time.time() - member_cache.get(key, 0) < MEMBER_CACHE_TTL:
        return True
    try:
        m = await bot.get_chat_member(chat_id, user_id)
    except (TelegramBadRequest, TelegramForbiddenError):
        return False
    ok = m.status in (ChatMemberStatus.MEMBER, ChatMemberStatus.ADMINISTRATOR,
                      ChatMemberStatus.CREATOR) or (
        m.status == ChatMemberStatus.RESTRICTED and getattr(m, "is_member", False))
    if ok:
        member_cache[key] = time.time()
    return ok


async def my_chats(user_id: int):
    """Чаты пользователя, где бот есть и человек всё ещё участник."""
    rows = await fetchall(
        "SELECT c.chat_id, c.title FROM user_chats uc JOIN chats c ON c.chat_id=uc.chat_id "
        "WHERE uc.user_id=? AND c.is_active=1 AND c.is_enabled=1 ORDER BY c.title", (user_id,))
    result = []
    for cid, title in rows:
        if await is_member(cid, user_id):
            result.append((cid, title))
        else:
            await execute("DELETE FROM user_chats WHERE user_id=? AND chat_id=?", (user_id, cid))
    return result


def chats_kb(rows, current):
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(
        text=("✅ " if current == cid else "") + (title or str(cid)),
        callback_data=f"sel:{cid}")] for cid, title in rows])


async def select_chat(message: types.Message, user_id: int, chat_id: int):
    chat = await get_chat(chat_id)
    if not chat or not chat[3]:
        return await message.answer("❌ Этот чат не найден или бот из него удалён.")
    if not await is_member(chat_id, user_id):
        return await message.answer("🚫 Ты не участник этого чата.")
    await ensure_user(user_id)
    await execute("UPDATE users SET current_chat_id=? WHERE user_id=?", (chat_id, user_id))
    await execute("INSERT OR IGNORE INTO user_chats VALUES(?,?)", (user_id, chat_id))
    user = await get_user(user_id)
    text = f"✅ Теперь твои сообщения уходят в «<b>{html.escape(chat[1] or 'чат')}</b>»."
    if not user[0]:
        text += "\n\nСначала выбери ник — нажми «🎲 Случайный ник» или /nick Имя."
    await message.answer(text, reply_markup=main_kb())


# ================= ЗАПОМИНАЕМ УЧАСТНИКОВ =================
seen_members: set[tuple[int, int]] = set()


async def remember_member(user_id: int, chat_id: int):
    key = (user_id, chat_id)
    if key in seen_members:
        return
    seen_members.add(key)
    await execute("INSERT OR IGNORE INTO users(user_id) VALUES(?)", (user_id,))
    await execute("INSERT OR IGNORE INTO user_chats VALUES(?,?)", (user_id, chat_id))


@dp.message.outer_middleware()
async def track_members(handler, message: types.Message, data):
    """Любой, кто пишет в группе, попадает в «Мои чаты» автоматически.
    Работает, если бот админ в группе или у него выключен privacy mode (/setprivacy в BotFather)."""
    if (message.chat.type in (ChatType.GROUP, ChatType.SUPERGROUP)
            and message.from_user and not message.from_user.is_bot):
        try:
            await remember_member(message.from_user.id, message.chat.id)
        except Exception as e:
            logging.warning(e)
    return await handler(message, data)


@dp.chat_member()
async def on_member_join(event: ChatMemberUpdated):
    """Новый участник зашёл в группу (приходит, только если бот админ)."""
    if event.new_chat_member.status in (ChatMemberStatus.MEMBER, ChatMemberStatus.ADMINISTRATOR,
                                        ChatMemberStatus.CREATOR):
        await remember_member(event.new_chat_member.user.id, event.chat.id)


# ================= БОТ ДОБАВЛЕН / УДАЛЁН =================
@dp.my_chat_member(F.chat.type.in_({ChatType.GROUP, ChatType.SUPERGROUP}))
async def on_bot_status(event: ChatMemberUpdated):
    new = event.new_chat_member.status
    if new in (ChatMemberStatus.MEMBER, ChatMemberStatus.ADMINISTRATOR):
        was_inside = event.old_chat_member.status in (
            ChatMemberStatus.MEMBER, ChatMemberStatus.ADMINISTRATOR)
        await upsert_chat(event.chat, event.from_user.id)
        if not was_inside:
            await bot.send_message(
                event.chat.id,
                "👋 <b>Анонимные сообщения</b> подключены!\n\n"
                "Жми кнопку ниже, чтобы написать в этот чат анонимно.\n"
                "Админам: /anonhelp — настройки.",
                reply_markup=write_button(event.chat.id))
    elif new in (ChatMemberStatus.LEFT, ChatMemberStatus.KICKED):
        await execute("UPDATE chats SET is_active=0 WHERE chat_id=?", (event.chat.id,))


@dp.message(F.migrate_to_chat_id)
async def on_migrate(message: types.Message):
    """Группа стала супергруппой — у неё меняется ID."""
    old, new = message.chat.id, message.migrate_to_chat_id
    for table in ("chats", "user_chats", "bans", "message_logs", "reply_context"):
        await db.execute(f"UPDATE OR IGNORE {table} SET chat_id=? WHERE chat_id=?", (new, old))
    await db.execute("UPDATE users SET current_chat_id=? WHERE current_chat_id=?", (new, old))
    await db.commit()


# ================= ЛИЧКА: КОМАНДЫ =================
@dp.message(CommandStart(), F.chat.type == ChatType.PRIVATE)
async def cmd_start(message: types.Message, command: CommandObject):
    await ensure_user(message.from_user.id)
    arg = command.args or ""
    if arg.startswith("c") and arg[1:].lstrip("-").isdigit():
        return await select_chat(message, message.from_user.id, int(arg[1:]))

    chats = await my_chats(message.from_user.id)
    if len(chats) == 1:                      # один чат — выбираем сразу
        return await select_chat(message, message.from_user.id, chats[0][0])
    if len(chats) > 1:                       # несколько — показываем список
        await message.answer("👋 <b>Анонимные сообщения</b>\n\nВыбери, в какой чат писать:",
                             reply_markup=chats_kb(chats, None))
        return await message.answer("Ник и меню — внизу 👇", reply_markup=main_kb())

    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="➕ Добавить бота в свой чат", url=add_to_group_link())]])
    await message.answer(
        "👋 <b>Анонимные сообщения</b>\n\n"
        "<b>Как писать:</b> нажми «✉️ Написать анонимно» в нужном чате "
        "или выбери его в «💬 Мои чаты».\n"
        "<b>Ник:</b> «🎲 Случайный ник» или /nick Имя.\n"
        "<b>Ответы:</b> если тебе ответят, я перешлю сюда — жми «Ответить» на моё сообщение.\n\n"
        "Хочешь анонимку у себя? Добавь бота в группу 👇",
        reply_markup=kb)
    await message.answer("Меню внизу 👇", reply_markup=main_kb())


@dp.message(Command("help"), F.chat.type == ChatType.PRIVATE)
async def cmd_help(message: types.Message):
    await message.answer(
        "📋 <b>Команды:</b>\n"
        "/chats — выбрать чат\n/nick Имя — свой ник\n"
        "/del — удалить своё сообщение (Reply на него)\n/rules — правила")


@dp.message(Command("rules"))
async def cmd_rules(message: types.Message):
    await message.answer(
        "📜 <b>Правила:</b>\n1. Не оскорблять участников.\n2. Без спама и рекламы.\n"
        "3. Запрещён контент 18+.\n\n👮 Админы чата могут забанить нарушителя.")


@dp.message(F.text == RANDOM_NICK_BTN, F.chat.type == ChatType.PRIVATE)
async def cmd_random_nick(message: types.Message):
    nick = f"{random.choice(ADJECTIVES)}{random.choice(NOUNS)}"
    await ensure_user(message.from_user.id)
    await execute("UPDATE users SET nickname=? WHERE user_id=?", (nick, message.from_user.id))
    await message.answer(f"✅ Твой новый ник: <b>{nick}</b>")


@dp.message(Command("nick"), F.chat.type == ChatType.PRIVATE)
async def cmd_nick(message: types.Message, command: CommandObject):
    nick = (command.args or "").strip()
    if not nick:
        return await message.answer("⚠️ Использование: /nick Имя")
    if len(nick) > 20:
        return await message.answer("⚠️ Максимум 20 символов.")
    await ensure_user(message.from_user.id)
    await execute("UPDATE users SET nickname=? WHERE user_id=?", (nick, message.from_user.id))
    await message.answer(f"✅ Ник установлен: <b>{html.escape(nick)}</b>")


@dp.message(Command("chats"), F.chat.type == ChatType.PRIVATE)
@dp.message(F.text == MY_CHATS_BTN, F.chat.type == ChatType.PRIVATE)
async def cmd_chats(message: types.Message):
    rows = await my_chats(message.from_user.id)
    if not rows:
        return await message.answer(
            "Пока пусто. Напиши что-нибудь в своей группе или нажми там «✉️ Написать анонимно» "
            "(админ вызывает кнопку командой /anon).")
    user = await get_user(message.from_user.id)
    await message.answer("Куда писать?", reply_markup=chats_kb(rows, user[1] if user else None))


@dp.callback_query(F.data.startswith("sel:"))
async def cb_select(call: types.CallbackQuery):
    await call.answer()
    await select_chat(call.message, call.from_user.id, int(call.data[4:]))


@dp.message(Command("del"), F.chat.type == ChatType.PRIVATE)
async def cmd_del(message: types.Message):
    if not message.reply_to_message:
        return await message.answer("⚠️ Сделай Reply на своё сообщение и отправь /del.")
    row = await fetchone(
        "SELECT chat_id, group_message_id FROM message_logs WHERE user_id=? AND user_message_id=?",
        (message.from_user.id, message.reply_to_message.message_id))
    if not row:
        return await message.answer("❌ Это сообщение нельзя удалить.")
    try:
        await bot.delete_message(row[0], row[1])
        await message.answer("🗑 Удалено из чата.")
    except TelegramBadRequest:
        await message.answer("❌ Не удалось удалить (старше 48 часов или уже удалено).")


# ================= ГРУППА: КОМАНДЫ АДМИНОВ =================
GROUP = F.chat.type.in_({ChatType.GROUP, ChatType.SUPERGROUP})


@dp.message(Command("anon", "start"), GROUP)
async def grp_anon(message: types.Message):
    await upsert_chat(message.chat, None)
    await message.answer("✉️ Написать в этот чат анонимно:", reply_markup=write_button(message.chat.id))


@dp.message(Command("anonhelp"), GROUP)
async def grp_help(message: types.Message):
    await message.reply(
        "⚙️ <b>Для админов:</b>\n"
        "/anon — кнопка «Написать анонимно» (можно закрепить)\n"
        "/anonon, /anonoff — включить / выключить\n"
        "/settopic — слать анонимки в текущую тему форума (/settopic off — в общий)\n"
        "/media on|off — разрешить фото/стикеры\n"
        "/cooldown N — пауза между сообщениями, сек\n"
        "/anonban, /anonunban — Reply на анонимку или ID")


async def admin_guard(message: types.Message) -> bool:
    if not await is_group_admin(message.chat.id, message.from_user.id):
        await message.reply("❌ Только для админов чата.")
        return False
    await upsert_chat(message.chat, None)
    return True


@dp.message(Command("anonon", "anonoff"), GROUP)
async def grp_toggle(message: types.Message, command: CommandObject):
    if not await admin_guard(message):
        return
    on = command.command == "anonon"
    await execute("UPDATE chats SET is_enabled=? WHERE chat_id=?", (int(on), message.chat.id))
    await message.reply("🟢 Анонимки включены." if on else "🔴 Анонимки выключены.")


@dp.message(Command("settopic"), GROUP)
async def grp_topic(message: types.Message, command: CommandObject):
    if not await admin_guard(message):
        return
    thread = None if (command.args or "").strip() == "off" else message.message_thread_id
    await execute("UPDATE chats SET thread_id=? WHERE chat_id=?", (thread, message.chat.id))
    await message.reply("✅ Анонимки будут приходить сюда." if thread else "✅ Анонимки — в общий чат.")


@dp.message(Command("media"), GROUP)
async def grp_media(message: types.Message, command: CommandObject):
    if not await admin_guard(message):
        return
    on = (command.args or "").strip() != "off"
    await execute("UPDATE chats SET allow_media=? WHERE chat_id=?", (int(on), message.chat.id))
    await message.reply("✅ Медиа разрешены." if on else "✅ Только текст.")


@dp.message(Command("cooldown"), GROUP)
async def grp_cooldown(message: types.Message, command: CommandObject):
    if not await admin_guard(message):
        return
    arg = (command.args or "").strip()
    if not arg.isdigit() or not 0 <= int(arg) <= 3600:
        return await message.reply("⚠️ Пример: /cooldown 10")
    await execute("UPDATE chats SET cooldown=? WHERE chat_id=?", (int(arg), message.chat.id))
    await message.reply(f"✅ Пауза: {arg} сек.")


async def target_from(message: types.Message, command: CommandObject):
    if message.reply_to_message:
        row = await fetchone(
            "SELECT user_id FROM message_logs WHERE chat_id=? AND group_message_id=?",
            (message.chat.id, message.reply_to_message.message_id))
        return row[0] if row else None
    arg = (command.args or "").strip()
    return int(arg) if arg.isdigit() else None


@dp.message(Command("anonban", "anonunban"), GROUP)
async def grp_ban(message: types.Message, command: CommandObject):
    if not await admin_guard(message):
        return
    uid = await target_from(message, command)
    if not uid:
        return await message.reply("⚠️ Reply на анонимку или /anonban ID")
    title = html.escape(message.chat.title or "чата")
    if command.command == "anonban":
        await execute("INSERT OR IGNORE INTO bans VALUES(?,?)", (message.chat.id, uid))
        if message.reply_to_message:  # убираем жёсткое сообщение из чата
            try:
                await message.reply_to_message.delete()
            except TelegramBadRequest:
                pass
        # ID не показываем в чате, чтобы не раскрывать анонима
        await message.reply("🚫 Автор забанен в анонимке этого чата.")
        note = f"⛔️ Вас забанили в анонимке «{title}»."
    else:
        await execute("DELETE FROM bans WHERE chat_id=? AND user_id=?", (message.chat.id, uid))
        await message.reply("✅ Автор разбанен.")
        note = f"🟢 Вас разбанили в «{title}»."
    try:
        await bot.send_message(uid, note)
    except Exception:
        pass


# ================= ВЛАДЕЛЕЦ БОТА =================
OWNER = F.from_user.id == OWNER_ID


@dp.message(Command("stats"), OWNER, F.chat.type == ChatType.PRIVATE)
async def own_stats(message: types.Message):
    chats = (await fetchone("SELECT COUNT(*) FROM chats WHERE is_active=1"))[0]
    users = (await fetchone("SELECT COUNT(*) FROM users"))[0]
    msgs = (await fetchone("SELECT COUNT(*) FROM message_logs"))[0]
    await message.answer(f"📊 Чатов: {chats}\nПользователей: {users}\nСообщений: {msgs}")


@dp.message(Command("gban", "gunban"), OWNER, F.chat.type == ChatType.PRIVATE)
async def own_gban(message: types.Message, command: CommandObject):
    arg = (command.args or "").strip()
    if not arg.isdigit():
        return await message.answer("⚠️ /gban ID")
    val = int(command.command == "gban")
    await ensure_user(int(arg))
    await execute("UPDATE users SET global_ban=? WHERE user_id=?", (val, int(arg)))
    await message.answer("🔇 Глобальный бан." if val else "✅ Глобальный бан снят.")


@dp.message(Command("broadcast"), OWNER, F.chat.type == ChatType.PRIVATE)
async def own_broadcast(message: types.Message, command: CommandObject):
    if not command.args:
        return await message.answer("⚠️ /broadcast Текст")
    users = [r[0] for r in await fetchall("SELECT user_id FROM users")]
    ok = 0
    for uid in users:
        try:
            await bot.send_message(uid, f"📢 <b>Объявление:</b>\n\n{html.escape(command.args)}")
            ok += 1
        except Exception:
            pass
        await asyncio.sleep(0.05)  # ~20 сообщений/сек — в лимитах Telegram
    await message.answer(f"✅ Получили: {ok} из {len(users)}")


# ================= ЛИЧКА → ЧАТ =================
@dp.message(F.chat.type == ChatType.PRIVATE)
async def private_handler(message: types.Message):
    uid = message.from_user.id
    user = await get_user(uid)
    if not user or not user[0]:
        return await message.answer("🛑 Сначала выбери ник 👇", reply_markup=main_kb())
    if user[2]:
        return await message.answer("⛔️ Вы заблокированы.")

    # Reply на пересланный ответ → пишем в тот чат, откуда он пришёл
    chat_id, reply_to = user[1], None
    if message.reply_to_message:
        ctx = await fetchone(
            "SELECT chat_id, group_message_id FROM reply_context WHERE user_id=? AND dm_message_id=?",
            (uid, message.reply_to_message.message_id))
        if not ctx:
            ctx = await fetchone(
                "SELECT chat_id, group_message_id FROM message_logs WHERE user_id=? AND user_message_id=?",
                (uid, message.reply_to_message.message_id))
        if ctx:
            chat_id, reply_to = ctx

    if not chat_id:
        return await message.answer("❓ Не выбран чат. Нажми «💬 Мои чаты» или кнопку в самом чате.")
    chat = await get_chat(chat_id)
    if not chat or not chat[3]:
        return await message.answer("❌ Бот больше не в этом чате. Выбери другой: /chats")
    _, title, thread_id, _, enabled, allow_media, cooldown = chat
    if not enabled:
        return await message.answer("🔴 Админы этого чата выключили анонимки.")
    if await is_banned(chat_id, uid):
        return await message.answer("⛔️ Ты забанен в этом чате.")
    if not await is_member(chat_id, uid):
        return await message.answer("🚫 Ты больше не участник этого чата.")
    if not message.text and not allow_media:
        return await message.answer("⚠️ В этом чате можно только текст.")
    key = (uid, chat_id)
    if time.time() - last_message_time.get(key, 0) < max(cooldown, 1):
        return await message.answer(f"⏳ Не чаще раза в {max(cooldown, 1)} сек.")
    last_message_time[key] = time.time()

    header = f"👤 <b>{html.escape(user[0])}</b>:"
    common = dict(chat_id=chat_id, message_thread_id=thread_id, reply_to_message_id=reply_to,
                  reply_markup=write_button(chat_id))  # кнопка под каждой анонимкой
    try:
        if message.text:
            sent = await bot.send_message(text=f"{header}\n{html.escape(message.text)}", **common)
        else:
            caption = html.escape(message.caption or "")
            sent = await bot.copy_message(from_chat_id=uid, message_id=message.message_id,
                                          caption=f"{header}\n{caption}", **common)
    except TelegramBadRequest as e:
        if "reply" in str(e).lower() and reply_to:  # исходное сообщение удалили
            common["reply_to_message_id"] = None
            sent = await bot.send_message(text=f"{header}\n{html.escape(message.text or '')}", **common)
        else:
            logging.error(e)
            return await message.answer("❌ Не удалось отправить. Возможно, у бота нет прав в чате.")
    except TelegramForbiddenError:
        await execute("UPDATE chats SET is_active=0 WHERE chat_id=?", (chat_id,))
        return await message.answer("❌ Бота удалили из этого чата.")

    sent_id = sent.message_id
    await execute("INSERT OR REPLACE INTO message_logs VALUES(?,?,?,?,?)",
                  (chat_id, sent_id, uid, message.message_id, int(time.time())))
    await message.answer(f"✅ Отправлено в «{html.escape(title or 'чат')}»", disable_notification=True)


# ================= ЧАТ → ЛИЧКА (ответы) =================
@dp.message(GROUP, F.reply_to_message)
async def group_reply_handler(message: types.Message):
    if message.text and message.text.startswith("/"):
        return
    rt = message.reply_to_message
    if not rt.from_user or rt.from_user.id != bot.id:
        return
    row = await fetchone(
        "SELECT user_id FROM message_logs WHERE chat_id=? AND group_message_id=?",
        (message.chat.id, message.reply_to_message.message_id))
    if not row:
        return
    header = f"🔔 <b>Ответ из «{html.escape(message.chat.title or 'чата')}»:</b>\n"
    try:
        if message.text:
            sent = await bot.send_message(row[0], header + html.escape(message.text))
        else:
            sent = await bot.copy_message(row[0], message.chat.id, message.message_id,
                                          caption=header + html.escape(message.caption or ""))
        await execute("INSERT OR REPLACE INTO reply_context VALUES(?,?,?,?)",
                      (row[0], sent.message_id, message.chat.id, message.message_id))
    except Exception as e:
        logging.warning(f"Ответ не доставлен: {e}")


# ================= ЗАПУСК =================
async def cleanup_loop():
    """Чистим старые связи раз в сутки (удалять всё равно можно только 48 ч)."""
    while True:
        await execute("DELETE FROM message_logs WHERE created_at < ?", (int(time.time()) - 30 * 86400,))
        await asyncio.sleep(86400)


async def main():
    global BOT_USERNAME
    await db_start()
    me = await bot.get_me()
    BOT_USERNAME = me.username
    await bot.set_my_commands(
        [BotCommand(command="start", description="Начать"),
         BotCommand(command="chats", description="Выбрать чат"),
         BotCommand(command="nick", description="Сменить ник"),
         BotCommand(command="del", description="Удалить сообщение"),
         BotCommand(command="help", description="Помощь")],
        scope=BotCommandScopeAllPrivateChats())
    await bot.set_my_commands(
        [BotCommand(command="anon", description="Кнопка «Написать анонимно»"),
         BotCommand(command="anonhelp", description="Настройки для админов"),
         BotCommand(command="anonban", description="Забанить анонима (Reply)")],
        scope=BotCommandScopeAllGroupChats())
    asyncio.create_task(cleanup_loop())
    logging.info(f"✅ @{BOT_USERNAME} запущен (multi-chat)")
    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())


if __name__ == "__main__":
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("Бот выключен")
