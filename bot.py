import asyncio
import logging
import os
import sqlite3
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path

from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)
from aiohttp import web
from dotenv import load_dotenv


BASE_DIR = Path(__file__).resolve().parent
DB_PATH = BASE_DIR / "bot.sqlite3"
CONSOLE_USER_LIMIT = 0
TESTER_USER_ID = -1

GENDERS = {
    "male": "Парень",
    "female": "Девушка",
}

LOOKING_FOR = {
    "male": "Парня",
    "female": "Девушку",
    "any": "Не важно",
}

PURPOSES = {
    "chat": "Общение",
    "relationship": "Отношения",
}

AGES = ["13-15", "16-17", "18-20", "21-25", "26-35", "36+"]

router = Router()


class ProfileFlow(StatesGroup):
    rules = State()
    gender = State()
    age = State()
    name = State()
    looking_for = State()
    purpose = State()


@dataclass
class UserProfile:
    user_id: int
    gender: str | None
    age: str | None
    name: str | None
    looking_for: str | None
    purpose: str | None
    subscription: str
    likes: int
    dislikes: int
    last_partner_id: int | None


def db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    with db() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                gender TEXT,
                age TEXT,
                name TEXT,
                looking_for TEXT,
                purpose TEXT,
                subscription TEXT NOT NULL DEFAULT 'Бесплатная',
                likes INTEGER NOT NULL DEFAULT 0,
                dislikes INTEGER NOT NULL DEFAULT 0,
                accepted_rules INTEGER NOT NULL DEFAULT 0,
                last_partner_id INTEGER
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS waiting_queue (
                user_id INTEGER PRIMARY KEY,
                created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS active_chats (
                user_id INTEGER PRIMARY KEY,
                partner_id INTEGER NOT NULL,
                created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS console_inbox (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                message TEXT NOT NULL,
                created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """
        )


def is_console_user(user_id: int) -> bool:
    return user_id < CONSOLE_USER_LIMIT


def push_console_message(user_id: int, message: str) -> None:
    with db() as conn:
        conn.execute(
            "INSERT INTO console_inbox (user_id, message) VALUES (?, ?)",
            (user_id, message),
        )


def fetch_console_messages(user_id: int) -> list[str]:
    with db() as conn:
        rows = conn.execute(
            "SELECT id, message FROM console_inbox WHERE user_id = ? ORDER BY id ASC",
            (user_id,),
        ).fetchall()
        if rows:
            conn.execute(
                "DELETE FROM console_inbox WHERE id IN (%s)"
                % ",".join("?" for _ in rows),
                [row["id"] for row in rows],
            )
    return [str(row["message"]) for row in rows]


def ensure_user(user_id: int) -> None:
    with db() as conn:
        conn.execute("INSERT OR IGNORE INTO users (user_id) VALUES (?)", (user_id,))


def get_profile(user_id: int) -> UserProfile | None:
    with db() as conn:
        row = conn.execute("SELECT * FROM users WHERE user_id = ?", (user_id,)).fetchone()
    if not row:
        return None
    return UserProfile(
        user_id=row["user_id"],
        gender=row["gender"],
        age=row["age"],
        name=row["name"],
        looking_for=row["looking_for"],
        purpose=row["purpose"],
        subscription=row["subscription"],
        likes=row["likes"],
        dislikes=row["dislikes"],
        last_partner_id=row["last_partner_id"],
    )


def profile_is_complete(profile: UserProfile | None) -> bool:
    return bool(
        profile
        and profile.gender
        and profile.age
        and profile.name
        and profile.looking_for
        and profile.purpose
    )


def update_user(user_id: int, **fields: object) -> None:
    if not fields:
        return
    keys = ", ".join(f"{key} = ?" for key in fields)
    values = list(fields.values())
    values.append(user_id)
    with db() as conn:
        conn.execute(f"UPDATE users SET {keys} WHERE user_id = ?", values)


def remove_from_queue(user_id: int) -> None:
    with db() as conn:
        conn.execute("DELETE FROM waiting_queue WHERE user_id = ?", (user_id,))


def get_partner(user_id: int) -> int | None:
    with db() as conn:
        row = conn.execute(
            "SELECT partner_id FROM active_chats WHERE user_id = ?", (user_id,)
        ).fetchone()
    return int(row["partner_id"]) if row else None


def get_active_users_count() -> int:
    with db() as conn:
        row = conn.execute(
            """
            SELECT COUNT(*) AS count
            FROM (
                SELECT user_id FROM waiting_queue
                UNION
                SELECT user_id FROM active_chats
            )
            """
        ).fetchone()
    return int(row["count"]) if row else 0


def profile_to_dict(profile: UserProfile | None) -> dict[str, object]:
    if not profile:
        return {"complete": False}
    return {
        "complete": profile_is_complete(profile),
        "user_id": profile.user_id,
        "gender": profile.gender,
        "age": profile.age,
        "name": profile.name,
        "looking_for": profile.looking_for,
        "purpose": profile.purpose,
        "subscription": profile.subscription,
        "likes": profile.likes,
        "dislikes": profile.dislikes,
        "last_partner_id": profile.last_partner_id,
        "active_count": get_active_users_count(),
        "partner_id": get_partner(profile.user_id),
    }


def are_compatible(first: UserProfile, second: UserProfile) -> bool:
    if first.purpose != second.purpose:
        return False
    first_wants_second = first.looking_for == "any" or first.looking_for == second.gender
    second_wants_first = second.looking_for == "any" or second.looking_for == first.gender
    return first_wants_second and second_wants_first


def find_partner_for(profile: UserProfile) -> int | None:
    with db() as conn:
        rows = conn.execute(
            """
            SELECT user_id
            FROM waiting_queue
            WHERE user_id != ?
            ORDER BY created_at ASC
            """,
            (profile.user_id,),
        ).fetchall()
    for row in rows:
        candidate = get_profile(int(row["user_id"]))
        if candidate and profile_is_complete(candidate) and are_compatible(profile, candidate):
            return candidate.user_id
    return None


def create_chat(first_id: int, second_id: int) -> None:
    with db() as conn:
        conn.execute("DELETE FROM waiting_queue WHERE user_id IN (?, ?)", (first_id, second_id))
        conn.execute("DELETE FROM active_chats WHERE user_id IN (?, ?)", (first_id, second_id))
        conn.execute(
            "INSERT INTO active_chats (user_id, partner_id) VALUES (?, ?)",
            (first_id, second_id),
        )
        conn.execute(
            "INSERT INTO active_chats (user_id, partner_id) VALUES (?, ?)",
            (second_id, first_id),
        )
        conn.execute(
            "UPDATE users SET last_partner_id = ? WHERE user_id = ?",
            (second_id, first_id),
        )
        conn.execute(
            "UPDATE users SET last_partner_id = ? WHERE user_id = ?",
            (first_id, second_id),
        )


def close_chat(user_id: int) -> int | None:
    partner_id = get_partner(user_id)
    if not partner_id:
        remove_from_queue(user_id)
        return None
    with db() as conn:
        conn.execute("DELETE FROM active_chats WHERE user_id IN (?, ?)", (user_id, partner_id))
        conn.execute("DELETE FROM waiting_queue WHERE user_id IN (?, ?)", (user_id, partner_id))
    return partner_id


def add_to_queue(user_id: int) -> None:
    with db() as conn:
        conn.execute("INSERT OR REPLACE INTO waiting_queue (user_id) VALUES (?)", (user_id,))


def increment_reputation(user_id: int, field: str) -> None:
    if field not in {"likes", "dislikes"}:
        return
    with db() as conn:
        conn.execute(f"UPDATE users SET {field} = {field} + 1 WHERE user_id = ?", (user_id,))


def kb(buttons: list[list[tuple[str, str]]]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=text, callback_data=data) for text, data in row]
            for row in buttons
        ]
    )


def rules_keyboard() -> InlineKeyboardMarkup:
    return kb([[("✅ Продолжить", "rules:accept")]])


def gender_keyboard(prefix: str) -> InlineKeyboardMarkup:
    return kb([[("👨 Парень", f"{prefix}:male"), ("👩 Девушка", f"{prefix}:female")]])


def age_keyboard(prefix: str) -> InlineKeyboardMarkup:
    return kb(
        [
            [(age, f"{prefix}:{age}") for age in AGES[:3]],
            [(age, f"{prefix}:{age}") for age in AGES[3:]],
        ]
    )


def looking_for_keyboard(prefix: str) -> InlineKeyboardMarkup:
    return kb(
        [
            [("👨 Парня", f"{prefix}:male"), ("👩 Девушку", f"{prefix}:female")],
            [("✨ Не важно", f"{prefix}:any")],
        ]
    )


def purpose_keyboard(prefix: str) -> InlineKeyboardMarkup:
    return kb([[("💬 Общение", f"{prefix}:chat"), ("❤️ Отношения", f"{prefix}:relationship")]])


def main_menu_keyboard() -> InlineKeyboardMarkup:
    return kb(
        [
            [("🔎 Начать поиск", "search:start")],
            [("✏️ Переделать информацию", "edit:menu")],
        ]
    )


def edit_menu_keyboard() -> InlineKeyboardMarkup:
    return kb(
        [
            [("👤 Пол", "edit:gender"), ("🎂 Возраст", "edit:age")],
            [("📝 Имя", "edit:name"), ("🎯 Кого ищешь", "edit:looking_for")],
            [("💭 Цель", "edit:purpose")],
            [("⬅️ Назад", "menu:show")],
        ]
    )


def chat_keyboard() -> InlineKeyboardMarkup:
    return kb([[("🚪 Завершить чат", "chat:stop")]])


def rating_keyboard(partner_id: int) -> InlineKeyboardMarkup:
    return kb(
        [
            [
                ("👍 Лайк", f"rate:{partner_id}:like"),
                ("👎 Дизлайк", f"rate:{partner_id}:dislike"),
                ("⏭️ Пропустить", f"rate:{partner_id}:skip"),
            ],
            [("🚩 Пожаловаться", f"report:{partner_id}")],
        ]
    )


async def safe_delete(bot: Bot, chat_id: int, message_id: int) -> None:
    with suppress(TelegramBadRequest, TelegramForbiddenError):
        await bot.delete_message(chat_id, message_id)


async def remember_message(state: FSMContext, message: Message) -> None:
    data = await state.get_data()
    ids = data.get("cleanup_message_ids", [])
    ids.append(message.message_id)
    await state.update_data(cleanup_message_ids=ids)


async def cleanup_flow_messages(bot: Bot, user_id: int, state: FSMContext) -> None:
    data = await state.get_data()
    for message_id in data.get("cleanup_message_ids", []):
        await safe_delete(bot, user_id, int(message_id))
    await state.update_data(cleanup_message_ids=[])


async def ask_gender(message: Message, state: FSMContext) -> None:
    sent = await message.answer("👤 Выбери свой пол:", reply_markup=gender_keyboard("gender"))
    await remember_message(state, sent)
    await state.set_state(ProfileFlow.gender)


async def ask_age(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.message.edit_text(
        "🎂 Выбери возраст. Боту можно пользоваться только с 13 лет:",
        reply_markup=age_keyboard("age"),
    )
    await state.set_state(ProfileFlow.age)


async def ask_name(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.message.edit_text("📝 Напиши имя, которое будет видно в анкете:")
    await state.set_state(ProfileFlow.name)


async def ask_looking_for(message: Message, state: FSMContext) -> None:
    sent = await message.answer("🎯 Кого ты ищешь?", reply_markup=looking_for_keyboard("looking"))
    await remember_message(state, sent)
    await state.set_state(ProfileFlow.looking_for)


async def ask_purpose(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.message.edit_text("💭 Для чего ищешь собеседника?", reply_markup=purpose_keyboard("purpose"))
    await state.set_state(ProfileFlow.purpose)


async def send_main_menu(message: Message | CallbackQuery) -> None:
    user_id = message.from_user.id
    profile = get_profile(user_id)
    active_count = get_active_users_count()
    if not profile_is_complete(profile):
        text = "📋 Анкета еще не заполнена. Нажми /start, чтобы пройти вопросы."
    else:
        text = (
            "✨ <b>Твоя анкета</b>\n\n"
            f"📝 Имя: <b>{profile.name}</b>\n"
            f"👤 Пол: <b>{GENDERS.get(profile.gender, profile.gender)}</b>\n"
            f"🎂 Возраст: <b>{profile.age}</b>\n"
            f"🎯 Ищешь: <b>{LOOKING_FOR.get(profile.looking_for, profile.looking_for)}</b>\n"
            f"💭 Цель: <b>{PURPOSES.get(profile.purpose, profile.purpose)}</b>\n\n"
            f"💎 Подписка: <b>{profile.subscription}</b>\n"
            f"⭐ Репутация: <b>{profile.likes}</b> лайков, <b>{profile.dislikes}</b> дизлайков\n"
            f"🟢 Активно сейчас: <b>{active_count}</b>"
        )
    markup = main_menu_keyboard() if profile_is_complete(profile) else None
    if isinstance(message, CallbackQuery):
        await message.message.edit_text(text, reply_markup=markup)
    else:
        await message.answer(text, reply_markup=markup)


async def notify_user(
    bot: Bot,
    user_id: int,
    text: str,
    reply_markup: InlineKeyboardMarkup | None = None,
) -> None:
    if is_console_user(user_id):
        push_console_message(user_id, text)
        return
    with suppress(TelegramBadRequest, TelegramForbiddenError):
        await bot.send_message(user_id, text, reply_markup=reply_markup)


@router.message(CommandStart())
async def start(message: Message, state: FSMContext) -> None:
    ensure_user(message.from_user.id)
    await close_chat_if_needed(message.bot, message.from_user.id, notify_partner=True)
    remove_from_queue(message.from_user.id)
    await state.clear()

    profile = get_profile(message.from_user.id)
    if profile_is_complete(profile):
        await send_main_menu(message)
        return

    text = (
        "📜 <b>Правила бота</b>\n\n"
        "1. Общайся уважительно и не оскорбляй собеседников.\n"
        "2. Не отправляй спам, рекламу, угрозы и запрещенный контент.\n"
        "3. Не передавай личные данные, если не уверен в собеседнике.\n"
        "4. Пользоваться ботом можно только с 13 лет.\n"
        "5. Жалобы могут привести к ограничению доступа.\n\n"
        "✅ Нажимая кнопку ниже, ты подтверждаешь, что принимаешь правила."
    )
    sent = await message.answer(text, reply_markup=rules_keyboard())
    await state.update_data(cleanup_message_ids=[sent.message_id])
    await state.set_state(ProfileFlow.rules)


@router.callback_query(F.data == "rules:accept")
async def accept_rules(callback: CallbackQuery, state: FSMContext) -> None:
    update_user(callback.from_user.id, accepted_rules=1)
    await callback.answer()
    await callback.message.edit_text("🚀 Отлично, начнем анкету.")
    await ask_gender(callback.message, state)


@router.callback_query(ProfileFlow.gender, F.data.startswith("gender:"))
async def set_gender(callback: CallbackQuery, state: FSMContext) -> None:
    gender = callback.data.split(":", 1)[1]
    update_user(callback.from_user.id, gender=gender)
    await callback.answer()
    await ask_age(callback, state)


@router.callback_query(ProfileFlow.age, F.data.startswith("age:"))
async def set_age(callback: CallbackQuery, state: FSMContext) -> None:
    age = callback.data.split(":", 1)[1]
    update_user(callback.from_user.id, age=age)
    await callback.answer()
    await ask_name(callback, state)


@router.message(ProfileFlow.name)
async def set_name(message: Message, state: FSMContext) -> None:
    name = (message.text or "").strip()
    if len(name) < 2 or len(name) > 32:
        await message.answer("Имя должно быть от 2 до 32 символов. Напиши еще раз:")
        return
    update_user(message.from_user.id, name=name)
    await remember_message(state, message)
    data = await state.get_data()
    if data.get("editing_field") == "name":
        await state.clear()
        await message.answer("Сохранено.")
        await send_main_menu(message)
        return
    await ask_looking_for(message, state)


@router.callback_query(ProfileFlow.looking_for, F.data.startswith("looking:"))
async def set_looking_for(callback: CallbackQuery, state: FSMContext) -> None:
    looking_for = callback.data.split(":", 1)[1]
    update_user(callback.from_user.id, looking_for=looking_for)
    await callback.answer()
    await ask_purpose(callback, state)


@router.callback_query(ProfileFlow.purpose, F.data.startswith("purpose:"))
async def set_purpose(callback: CallbackQuery, state: FSMContext) -> None:
    purpose = callback.data.split(":", 1)[1]
    update_user(callback.from_user.id, purpose=purpose)
    await callback.answer()
    await cleanup_flow_messages(callback.bot, callback.from_user.id, state)
    await state.clear()
    await callback.message.answer("✅ Анкета готова.")
    await send_main_menu(callback.message)


@router.callback_query(F.data == "menu:show")
async def show_menu(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await callback.answer()
    await send_main_menu(callback)


@router.callback_query(F.data == "edit:menu")
async def edit_menu(callback: CallbackQuery) -> None:
    await callback.answer()
    await callback.message.edit_text("✏️ Что именно хочешь переделать?", reply_markup=edit_menu_keyboard())


@router.callback_query(F.data.startswith("edit:"))
async def edit_field(callback: CallbackQuery, state: FSMContext) -> None:
    field = callback.data.split(":", 1)[1]
    await callback.answer()
    await state.update_data(editing_field=field)
    if field == "gender":
        await callback.message.edit_text("👤 Выбери новый пол:", reply_markup=gender_keyboard("setedit"))
    elif field == "age":
        await callback.message.edit_text("🎂 Выбери новый возраст:", reply_markup=age_keyboard("setedit"))
    elif field == "name":
        await callback.message.edit_text("📝 Напиши новое имя:")
        await state.set_state(ProfileFlow.name)
    elif field == "looking_for":
        await callback.message.edit_text("🎯 Кого теперь ищешь?", reply_markup=looking_for_keyboard("setedit"))
    elif field == "purpose":
        await callback.message.edit_text("💭 Выбери новую цель:", reply_markup=purpose_keyboard("setedit"))


@router.callback_query(F.data.startswith("setedit:"))
async def save_edit_callback(callback: CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    field = data.get("editing_field")
    value = callback.data.split(":", 1)[1]
    if field in {"gender", "age", "looking_for", "purpose"}:
        update_user(callback.from_user.id, **{field: value})
    await state.clear()
    await callback.answer("Сохранено")
    await send_main_menu(callback)


@router.callback_query(F.data == "search:start")
async def start_search(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    ensure_user(callback.from_user.id)
    profile = get_profile(callback.from_user.id)
    if not profile_is_complete(profile):
        await callback.answer("Сначала заполни анкету 📋", show_alert=True)
        return

    if get_partner(callback.from_user.id):
        await callback.answer("Ты уже в чате 💬", show_alert=True)
        return

    partner_id = find_partner_for(profile)
    if partner_id:
        create_chat(callback.from_user.id, partner_id)
        await callback.answer()
        await callback.message.edit_text(
            "🎉 Собеседник найден. Вы начали беседу.\n\nПиши сообщение, а я передам его дальше.",
            reply_markup=chat_keyboard(),
        )
        await notify_user(
            callback.bot,
            partner_id,
            "🎉 Собеседник найден. Вы начали беседу.\n\nПиши сообщение, а я передам его дальше.",
            chat_keyboard(),
        )
        return

    add_to_queue(callback.from_user.id)
    await callback.answer()
    await callback.message.edit_text(
        "🔎 Ищу активного собеседника по твоей категории.\n\nКак только кто-то подойдет, я соединю вас.",
        reply_markup=kb([[("❌ Отменить поиск", "search:cancel")]]),
    )


@router.callback_query(F.data == "search:cancel")
async def cancel_search(callback: CallbackQuery) -> None:
    remove_from_queue(callback.from_user.id)
    await callback.answer("Поиск отменен")
    await send_main_menu(callback)


async def close_chat_if_needed(bot: Bot, user_id: int, notify_partner: bool) -> int | None:
    partner_id = close_chat(user_id)
    if partner_id and notify_partner:
        await notify_user(
            bot,
            partner_id,
            "🚪 Собеседник завершил чат. Оцени общение:",
            rating_keyboard(user_id),
        )
    return partner_id


@router.callback_query(F.data == "chat:stop")
async def stop_chat_callback(callback: CallbackQuery) -> None:
    partner_id = await close_chat_if_needed(callback.bot, callback.from_user.id, notify_partner=True)
    await callback.answer()
    if partner_id:
        await callback.message.edit_text(
            "🚪 Чат завершен. Оцени собеседника:",
            reply_markup=rating_keyboard(partner_id),
        )
    else:
        await callback.message.edit_text("💤 Активного чата нет.", reply_markup=main_menu_keyboard())


@router.message(Command("stop"))
async def stop_chat_command(message: Message) -> None:
    partner_id = await close_chat_if_needed(message.bot, message.from_user.id, notify_partner=True)
    if partner_id:
        await message.answer("🚪 Чат завершен. Оцени собеседника:", reply_markup=rating_keyboard(partner_id))
    else:
        await message.answer("💤 Активного чата нет.")


@router.callback_query(F.data.startswith("rate:"))
async def rate_partner(callback: CallbackQuery) -> None:
    _, partner_id_raw, rating = callback.data.split(":")
    partner_id = int(partner_id_raw)
    if rating == "like":
        increment_reputation(partner_id, "likes")
        text = "👍 Спасибо, лайк засчитан."
    elif rating == "dislike":
        increment_reputation(partner_id, "dislikes")
        text = "👎 Спасибо, дизлайк засчитан."
    else:
        text = "⏭️ Оценка пропущена."
    await callback.answer(text)
    await callback.message.edit_text(text, reply_markup=main_menu_keyboard())


@router.callback_query(F.data.startswith("report:"))
async def report_partner(callback: CallbackQuery) -> None:
    await callback.answer("Жалоба принята. Пока это тестовая кнопка.", show_alert=True)


@router.message()
async def relay_message(message: Message) -> None:
    partner_id = get_partner(message.from_user.id)
    if not partner_id:
        return

    if is_console_user(partner_id):
        text = message.text or message.caption
        if not text:
            content_type = getattr(message, "content_type", "сообщение")
            text = f"[{content_type}]"
        push_console_message(partner_id, f"Собеседник: {text}")
        return

    with suppress(TelegramBadRequest, TelegramForbiddenError):
        await message.bot.copy_message(
            chat_id=partner_id,
            from_chat_id=message.chat.id,
            message_id=message.message_id,
        )


async def main() -> None:
    load_dotenv(BASE_DIR / ".env")
    token = os.getenv("BOT_TOKEN")
    if not token:
        raise RuntimeError("BOT_TOKEN не найден. Создай .env по примеру .env.example")

    logging.basicConfig(level=logging.INFO)
    init_db()

    bot = Bot(token=token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher(storage=MemoryStorage())
    dp.include_router(router)

    runner = await start_health_server(bot)
    try:
        await bot.delete_webhook(drop_pending_updates=True)
        await dp.start_polling(bot)
    finally:
        await runner.cleanup()


async def start_health_server(bot: Bot) -> web.AppRunner:
    async def health(_: web.Request) -> web.Response:
        return web.Response(text="Bot is running")

    def tester_secret() -> str:
        return os.getenv("TESTER_SECRET") or "suka1234567890"

    async def read_payload(request: web.Request) -> dict[str, object]:
        if request.can_read_body:
            try:
                data = await request.json()
                if isinstance(data, dict):
                    return data
            except Exception:
                return {}
        return {}

    def is_authorized(request: web.Request, data: dict[str, object]) -> bool:
        secret = tester_secret()
        if not secret:
            return False
        provided = (
            request.headers.get("X-Tester-Secret")
            or request.query.get("secret")
            or str(data.get("secret", ""))
        )
        return provided == secret

    def unauthorized() -> web.Response:
        return web.json_response(
            {"ok": False, "error": "TESTER_SECRET is missing or incorrect"},
            status=403,
        )

    async def tester_profile(request: web.Request) -> web.Response:
        data = await read_payload(request)
        if not is_authorized(request, data):
            return unauthorized()

        if request.method == "POST":
            ensure_user(TESTER_USER_ID)
            allowed = {"gender", "age", "name", "looking_for", "purpose"}
            fields = {key: data[key] for key in allowed if key in data}
            if fields:
                fields["accepted_rules"] = 1
                update_user(TESTER_USER_ID, **fields)

        ensure_user(TESTER_USER_ID)
        return web.json_response(
            {"ok": True, "profile": profile_to_dict(get_profile(TESTER_USER_ID))}
        )

    async def tester_search(request: web.Request) -> web.Response:
        data = await read_payload(request)
        if not is_authorized(request, data):
            return unauthorized()

        ensure_user(TESTER_USER_ID)
        profile = get_profile(TESTER_USER_ID)
        if not profile_is_complete(profile):
            return web.json_response({"ok": False, "error": "profile_incomplete"}, status=400)

        partner_id = get_partner(TESTER_USER_ID)
        if partner_id:
            return web.json_response({"ok": True, "status": "chat", "partner_id": partner_id})

        partner_id = find_partner_for(profile)
        if partner_id:
            create_chat(TESTER_USER_ID, partner_id)
            await notify_user(
                bot,
                partner_id,
                "🎉 Собеседник найден. Вы начали беседу.\n\nПиши сообщение, а я передам его дальше.",
                chat_keyboard(),
            )
            return web.json_response({"ok": True, "status": "chat", "partner_id": partner_id})

        add_to_queue(TESTER_USER_ID)
        return web.json_response({"ok": True, "status": "waiting"})

    async def tester_cancel(request: web.Request) -> web.Response:
        data = await read_payload(request)
        if not is_authorized(request, data):
            return unauthorized()
        remove_from_queue(TESTER_USER_ID)
        return web.json_response({"ok": True})

    async def tester_send(request: web.Request) -> web.Response:
        data = await read_payload(request)
        if not is_authorized(request, data):
            return unauthorized()

        text = str(data.get("text", "")).strip()
        if not text:
            return web.json_response({"ok": False, "error": "empty_text"}, status=400)

        partner_id = get_partner(TESTER_USER_ID)
        if not partner_id:
            return web.json_response({"ok": False, "error": "no_active_chat"}, status=400)

        if is_console_user(partner_id):
            push_console_message(partner_id, f"Собеседник: {text}")
        else:
            await notify_user(bot, partner_id, text)
        return web.json_response({"ok": True})

    async def tester_inbox(request: web.Request) -> web.Response:
        data = await read_payload(request)
        if not is_authorized(request, data):
            return unauthorized()

        return web.json_response(
            {
                "ok": True,
                "messages": fetch_console_messages(TESTER_USER_ID),
                "partner_id": get_partner(TESTER_USER_ID),
                "active_count": get_active_users_count(),
            }
        )

    async def tester_stop(request: web.Request) -> web.Response:
        data = await read_payload(request)
        if not is_authorized(request, data):
            return unauthorized()

        partner_id = close_chat(TESTER_USER_ID)
        if partner_id:
            await notify_user(
                bot,
                partner_id,
                "🚪 Собеседник завершил чат. Оцени общение:",
                rating_keyboard(TESTER_USER_ID),
            )
        return web.json_response({"ok": True, "partner_id": partner_id})

    async def tester_rate(request: web.Request) -> web.Response:
        data = await read_payload(request)
        if not is_authorized(request, data):
            return unauthorized()

        partner_id = int(data.get("partner_id") or 0)
        rating = str(data.get("rating", "skip"))
        if partner_id and rating == "like":
            increment_reputation(partner_id, "likes")
        elif partner_id and rating == "dislike":
            increment_reputation(partner_id, "dislikes")
        return web.json_response({"ok": True})

    app = web.Application()
    app.router.add_get("/", health)
    app.router.add_get("/health", health)
    app.router.add_get("/tester/profile", tester_profile)
    app.router.add_post("/tester/profile", tester_profile)
    app.router.add_post("/tester/search", tester_search)
    app.router.add_post("/tester/cancel", tester_cancel)
    app.router.add_post("/tester/send", tester_send)
    app.router.add_get("/tester/inbox", tester_inbox)
    app.router.add_post("/tester/stop", tester_stop)
    app.router.add_post("/tester/rate", tester_rate)

    runner = web.AppRunner(app)
    await runner.setup()
    port = int(
        os.getenv("PORT")
        or os.getenv("APP_PORT")
        or os.getenv("JRM_PORT")
        or "80"
    )
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    logging.info("Health server started on port %s", port)
    return runner


if __name__ == "__main__":
    asyncio.run(main())
