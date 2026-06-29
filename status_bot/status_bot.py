import asyncio
import html
import logging
import os
import sqlite3
from contextlib import suppress
from dataclasses import dataclass
from datetime import datetime, timedelta
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
    KeyboardButton,
    Message,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
)
from dotenv import load_dotenv


BASE_DIR = Path(__file__).resolve().parent
DB_PATH = BASE_DIR / "status_bot.sqlite3"

ADMIN_PASSWORD_DEFAULT = "sonimaadmin2026"
COMPLAINT_LIMIT_FOR_PUNISHMENT = 3

STATUS_LABELS = {
    "clean": "🟢 Чистый",
    "suspicious": "🟡 Подозрительный",
    "punished": "🔴 Наказанный",
}

REASON_LIST = [
    "Педофилия или попытки общения с несовершеннолетними в опасном контексте",
    "Домогательство, давление, сексуальные сообщения без согласия",
    "Угрозы, шантаж или агрессия",
    "Рассылка запрещенного/опасного контента",
    "Спам, мошенничество или выдача себя за другого человека",
    "Повторные жалобы от пользователей",
]

APPEAL_QUESTIONS = [
    "1/5. Кратко опиши, почему ты считаешь статус ошибочным.",
    "2/5. Были ли у тебя конфликты с пользователями перед жалобой?",
    "3/5. Можешь ли ты объяснить ситуацию со своей стороны?",
    "4/5. Есть ли доказательства или свидетели? Если нет, напиши «нет».",
    "5/5. Что ты готов сделать, чтобы ситуация не повторилась?",
]

public_router = Router(name="public")
admin_router = Router(name="admin")

public_bot: Bot | None = None
admin_bot_instance: Bot | None = None


class PublicFlow(StatesGroup):
    report_target = State()
    report_reason = State()
    report_photo = State()
    appeal_answers = State()


class AdminFlow(StatesGroup):
    password = State()
    manual_target = State()
    manual_action = State()
    manual_duration = State()
    manual_reason = State()
    reject_reply = State()


@dataclass
class UserInfo:
    user_id: int
    username: str | None
    full_name: str
    accepted_rules: int
    complaints_count: int
    warnings_count: int
    status: str
    status_reason: str | None
    punished_until: str | None


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
                username TEXT,
                full_name TEXT NOT NULL DEFAULT '',
                accepted_rules INTEGER NOT NULL DEFAULT 0,
                complaints_count INTEGER NOT NULL DEFAULT 0,
                warnings_count INTEGER NOT NULL DEFAULT 0,
                status TEXT NOT NULL DEFAULT 'clean',
                status_reason TEXT,
                punished_until TEXT,
                created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS admins (
                admin_id INTEGER PRIMARY KEY,
                username TEXT,
                full_name TEXT NOT NULL DEFAULT '',
                created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS cases (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                case_type TEXT NOT NULL,
                reporter_id INTEGER NOT NULL,
                target_id INTEGER,
                text TEXT,
                answers TEXT,
                photo_file_id TEXT,
                status TEXT NOT NULL DEFAULT 'pending',
                admin_id INTEGER,
                admin_reply TEXT,
                created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                resolved_at DATETIME
            )
            """
        )


def now_iso() -> str:
    return datetime.utcnow().replace(microsecond=0).isoformat()


def display_name(message: Message) -> str:
    user = message.from_user
    if not user:
        return "Unknown"
    return user.full_name or user.username or str(user.id)


def mention(user_id: int, full_name: str | None, username: str | None = None) -> str:
    name = full_name or username or str(user_id)
    safe_name = html.escape(name)
    if username:
        return f'<a href="https://t.me/{html.escape(username)}">{safe_name}</a>'
    return f'<a href="tg://user?id={user_id}">{safe_name}</a>'


def ensure_user(message_or_id: Message | int, username: str | None = None, full_name: str | None = None) -> None:
    if isinstance(message_or_id, Message):
        user = message_or_id.from_user
        if not user:
            return
        user_id = user.id
        username = user.username
        full_name = user.full_name
    else:
        user_id = message_or_id

    with db() as conn:
        conn.execute(
            """
            INSERT INTO users (user_id, username, full_name, updated_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                username = excluded.username,
                full_name = excluded.full_name,
                updated_at = excluded.updated_at
            """,
            (user_id, username, full_name or "", now_iso()),
        )


def get_user(user_id: int) -> UserInfo | None:
    with db() as conn:
        row = conn.execute("SELECT * FROM users WHERE user_id = ?", (user_id,)).fetchone()
    if not row:
        return None
    return UserInfo(
        user_id=row["user_id"],
        username=row["username"],
        full_name=row["full_name"],
        accepted_rules=row["accepted_rules"],
        complaints_count=row["complaints_count"],
        warnings_count=row["warnings_count"],
        status=row["status"],
        status_reason=row["status_reason"],
        punished_until=row["punished_until"],
    )


def find_user(value: str) -> UserInfo | None:
    value = value.strip()
    if value.startswith("@"):
        username = value[1:].lower()
        with db() as conn:
            row = conn.execute("SELECT user_id FROM users WHERE lower(username) = ?", (username,)).fetchone()
        return get_user(int(row["user_id"])) if row else None
    if value.isdigit():
        ensure_user(int(value))
        return get_user(int(value))
    return None


def accept_rules(user_id: int) -> None:
    with db() as conn:
        conn.execute("UPDATE users SET accepted_rules = 1, updated_at = ? WHERE user_id = ?", (now_iso(), user_id))


def set_status(user_id: int, status: str, reason: str | None = None, punished_until: str | None = None) -> None:
    ensure_user(user_id)
    with db() as conn:
        conn.execute(
            """
            UPDATE users
            SET status = ?, status_reason = ?, punished_until = ?, updated_at = ?
            WHERE user_id = ?
            """,
            (status, reason, punished_until, now_iso(), user_id),
        )


def increment_complaint(target_id: int, reason: str) -> UserInfo:
    ensure_user(target_id)
    with db() as conn:
        conn.execute(
            """
            UPDATE users
            SET complaints_count = complaints_count + 1,
                warnings_count = warnings_count + 1,
                status_reason = ?,
                updated_at = ?
            WHERE user_id = ?
            """,
            (reason, now_iso(), target_id),
        )
        row = conn.execute("SELECT complaints_count FROM users WHERE user_id = ?", (target_id,)).fetchone()
        count = int(row["complaints_count"])
        status = "punished" if count >= COMPLAINT_LIMIT_FOR_PUNISHMENT else "suspicious"
        conn.execute("UPDATE users SET status = ? WHERE user_id = ?", (status, target_id))
    return get_user(target_id)


def create_case(
    case_type: str,
    reporter_id: int,
    target_id: int | None,
    text: str | None,
    answers: str | None = None,
    photo_file_id: str | None = None,
) -> int:
    with db() as conn:
        cur = conn.execute(
            """
            INSERT INTO cases (case_type, reporter_id, target_id, text, answers, photo_file_id)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (case_type, reporter_id, target_id, text, answers, photo_file_id),
        )
        return int(cur.lastrowid)


def get_case(case_id: int) -> sqlite3.Row | None:
    with db() as conn:
        return conn.execute("SELECT * FROM cases WHERE id = ?", (case_id,)).fetchone()


def resolve_case(case_id: int, status: str, admin_id: int, reply: str | None = None) -> None:
    with db() as conn:
        conn.execute(
            """
            UPDATE cases
            SET status = ?, admin_id = ?, admin_reply = ?, resolved_at = ?
            WHERE id = ?
            """,
            (status, admin_id, reply, now_iso(), case_id),
        )


def add_admin(message: Message) -> None:
    user = message.from_user
    if not user:
        return
    with db() as conn:
        conn.execute(
            """
            INSERT OR REPLACE INTO admins (admin_id, username, full_name)
            VALUES (?, ?, ?)
            """,
            (user.id, user.username, user.full_name),
        )


def is_admin(user_id: int) -> bool:
    with db() as conn:
        row = conn.execute("SELECT 1 FROM admins WHERE admin_id = ?", (user_id,)).fetchone()
    return bool(row)


def admin_ids() -> list[int]:
    with db() as conn:
        rows = conn.execute("SELECT admin_id FROM admins").fetchall()
    return [int(row["admin_id"]) for row in rows]


def ikb(rows: list[list[tuple[str, str]]]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=text, callback_data=data) for text, data in row]
            for row in rows
        ]
    )


def rules_keyboard() -> InlineKeyboardMarkup:
    return ikb([[("✅ Согласиться с правилами", "rules:accept")]])


def public_menu_keyboard(user: UserInfo) -> InlineKeyboardMarkup:
    return ikb(
        [
            [("📌 Узнать причину", "menu:reasons")],
            [("🚩 Пожаловаться", "menu:report")],
            [("📝 Подать апелляцию", "menu:appeal")],
        ]
    )


def appeal_from_reasons_keyboard() -> InlineKeyboardMarkup:
    return ikb([[("📝 Подать апелляцию", "menu:appeal")], [("⬅️ В меню", "menu:show")]])


def skip_photo_keyboard() -> InlineKeyboardMarkup:
    return ikb([[("➡️ Без фото", "report:skip_photo")]])


def admin_case_keyboard(case_id: int) -> InlineKeyboardMarkup:
    return ikb(
        [
            [("✅ Подтвердить", f"case:{case_id}:approve")],
            [("❌ Отменить", f"case:{case_id}:reject")],
            [("💬 Отменить и ответить", f"case:{case_id}:reject_reply")],
        ]
    )


def admin_reply_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="🚫 Заблокировать/предупредить")],
            [KeyboardButton(text="📋 Меню")],
        ],
        resize_keyboard=True,
    )


def manual_action_keyboard() -> InlineKeyboardMarkup:
    return ikb([[("🚫 Заблокировать", "manual:block"), ("⚠️ Предупредить", "manual:warn")]])


async def send_public_menu(message: Message | CallbackQuery) -> None:
    user_id = message.from_user.id
    user = get_user(user_id)
    if not user:
        ensure_user(user_id)
        user = get_user(user_id)
    text = (
        "🛡️ <b>Твой статус</b>\n\n"
        f"ID аккаунта: <code>{user.user_id}</code>\n"
        f"Статус: <b>{STATUS_LABELS.get(user.status, user.status)}</b>\n"
        f"Количество жалоб: <b>{user.complaints_count}</b>\n"
        f"Предупреждения: <b>{user.warnings_count}</b>"
    )
    if user.punished_until:
        text += f"\nНаказание до: <b>{html.escape(user.punished_until)}</b>"
    if isinstance(message, CallbackQuery):
        await message.message.edit_text(text, reply_markup=public_menu_keyboard(user))
    else:
        await message.answer(text, reply_markup=public_menu_keyboard(user))


@public_router.message(CommandStart())
async def public_start(message: Message) -> None:
    ensure_user(message)
    user = get_user(message.from_user.id)
    if user and user.accepted_rules:
        await send_public_menu(message)
        return

    text = (
        "🛡️ <b>Статус-бот</b>\n\n"
        "Этот бот показывает твой ID, количество жалоб и текущий статус аккаунта.\n\n"
        "Статусы:\n"
        "🟢 Чистый — нет серьезных подтвержденных жалоб.\n"
        "🟡 Подозрительный — есть предупреждения или подтвержденные жалобы.\n"
        "🔴 Наказанный — превышен лимит жалоб или назначено наказание админом.\n\n"
        f"Твой ID: <code>{message.from_user.id}</code>\n\n"
        "Правила: нельзя отправлять ложные жалобы, спамить апелляциями или использовать бот для травли."
    )
    await message.answer(text, reply_markup=rules_keyboard())


@public_router.callback_query(F.data == "rules:accept")
async def public_accept_rules(callback: CallbackQuery) -> None:
    ensure_user(callback.from_user.id, callback.from_user.username, callback.from_user.full_name)
    accept_rules(callback.from_user.id)
    await callback.answer("Правила приняты")
    await send_public_menu(callback)


@public_router.callback_query(F.data == "menu:show")
async def public_show_menu(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await callback.answer()
    await send_public_menu(callback)


@public_router.callback_query(F.data == "menu:reasons")
async def public_reasons(callback: CallbackQuery) -> None:
    text = "📌 <b>Возможные причины жалоб и наказаний</b>\n\n"
    text += "\n".join(f"• {html.escape(reason)}" for reason in REASON_LIST)
    user = get_user(callback.from_user.id)
    if user and user.status_reason:
        text += f"\n\nТекущая причина в профиле: <b>{html.escape(user.status_reason)}</b>"
    await callback.answer()
    await callback.message.edit_text(text, reply_markup=appeal_from_reasons_keyboard())


@public_router.callback_query(F.data == "menu:appeal")
async def public_appeal(callback: CallbackQuery, state: FSMContext) -> None:
    user = get_user(callback.from_user.id)
    await callback.answer()
    if user and user.status == "clean" and user.complaints_count == 0 and user.warnings_count == 0:
        await callback.message.edit_text(
            "🟢 У вас нет наказаний или предупреждений. Апелляция сейчас не нужна.",
            reply_markup=ikb([[("⬅️ В меню", "menu:show")]]),
        )
        return

    await state.update_data(appeal_answers=[], appeal_question_index=0)
    await callback.message.edit_text(APPEAL_QUESTIONS[0])
    await state.set_state(PublicFlow.appeal_answers)


@public_router.message(PublicFlow.appeal_answers)
async def public_appeal_answer(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    answers = list(data.get("appeal_answers", []))
    index = int(data.get("appeal_question_index", 0))
    answers.append(message.text or "")
    index += 1

    if index < len(APPEAL_QUESTIONS):
        await state.update_data(appeal_answers=answers, appeal_question_index=index)
        await message.answer(APPEAL_QUESTIONS[index])
        return

    text = "\n\n".join(
        f"<b>{html.escape(APPEAL_QUESTIONS[i])}</b>\n{html.escape(answer)}"
        for i, answer in enumerate(answers)
    )
    case_id = create_case("appeal", message.from_user.id, message.from_user.id, None, text)
    await state.clear()
    await message.answer("✅ Апелляция отправлена администраторам.", reply_markup=ikb([[("⬅️ В меню", "menu:show")]]))
    await notify_admins_about_case(case_id)


@public_router.callback_query(F.data == "menu:report")
async def public_report(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    await callback.message.edit_text(
        "🚩 На кого жалоба?\n\n"
        "Отправь ID пользователя, @username или перешли сообщение пользователя.\n"
        "Важно: если пользователь ни разу не заходил в бот, по username найти его может не получиться."
    )
    await state.set_state(PublicFlow.report_target)


@public_router.message(PublicFlow.report_target)
async def public_report_target(message: Message, state: FSMContext) -> None:
    target = None
    if message.forward_from:
        ensure_user(message.forward_from.id, message.forward_from.username, message.forward_from.full_name)
        target = get_user(message.forward_from.id)
    elif message.text:
        target = find_user(message.text)

    if not target:
        await message.answer("Не смог найти пользователя. Отправь его ID, @username или перешли его сообщение.")
        return

    await state.update_data(report_target_id=target.user_id)
    await message.answer(
        "👤 <b>Пользователь выбран</b>\n\n"
        f"ID: <code>{target.user_id}</code>\n"
        f"Профиль: {mention(target.user_id, target.full_name, target.username)}\n"
        f"Статус: <b>{STATUS_LABELS.get(target.status, target.status)}</b>\n\n"
        "Теперь напиши причину жалобы. Например: домогательство, педофилия, угрозы, спам и т.д."
    )
    await state.set_state(PublicFlow.report_reason)


@public_router.message(PublicFlow.report_reason)
async def public_report_reason(message: Message, state: FSMContext) -> None:
    if not message.text or len(message.text.strip()) < 5:
        await message.answer("Причина слишком короткая. Напиши подробнее.")
        return
    await state.update_data(report_reason=message.text.strip())
    await message.answer("📎 Можешь отправить фото-доказательство или нажать «Без фото».", reply_markup=skip_photo_keyboard())
    await state.set_state(PublicFlow.report_photo)


@public_router.callback_query(PublicFlow.report_photo, F.data == "report:skip_photo")
async def public_report_skip_photo(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    await finish_report(callback.message, state, None)


@public_router.message(PublicFlow.report_photo)
async def public_report_photo(message: Message, state: FSMContext) -> None:
    if not message.photo:
        await message.answer("Отправь фото или нажми «Без фото».", reply_markup=skip_photo_keyboard())
        return
    await finish_report(message, state, message.photo[-1].file_id)


async def finish_report(message: Message, state: FSMContext, photo_file_id: str | None) -> None:
    data = await state.get_data()
    target_id = int(data["report_target_id"])
    reason = str(data["report_reason"])
    case_id = create_case("complaint", message.chat.id, target_id, reason, photo_file_id=photo_file_id)
    await state.clear()
    await message.answer("✅ Жалоба отправлена администраторам.", reply_markup=ikb([[("⬅️ В меню", "menu:show")]]))
    await notify_admins_about_case(case_id)


async def notify_admins_about_case(case_id: int) -> None:
    case = get_case(case_id)
    if not case:
        return
    reporter = get_user(int(case["reporter_id"]))
    target = get_user(int(case["target_id"])) if case["target_id"] else None
    case_title = "🚩 Жалоба" if case["case_type"] == "complaint" else "📝 Апелляция"

    text = f"{case_title} <b>#{case_id}</b>\n\n"
    if reporter:
        text += f"Отправитель: {mention(reporter.user_id, reporter.full_name, reporter.username)}\n"
        text += f"ID отправителя: <code>{reporter.user_id}</code>\n\n"
    if target:
        text += f"На пользователя: {mention(target.user_id, target.full_name, target.username)}\n"
        text += f"ID пользователя: <code>{target.user_id}</code>\n"
        text += f"Статус: <b>{STATUS_LABELS.get(target.status, target.status)}</b>\n\n"
    if case["text"]:
        text += f"<b>Текст:</b>\n{html.escape(case['text'])}\n\n"
    if case["answers"]:
        text += f"<b>Ответы:</b>\n{case['answers']}\n\n"

    for admin_id in admin_ids():
        with suppress(TelegramBadRequest, TelegramForbiddenError):
            if case["photo_file_id"]:
                await admin_bot_send_photo(admin_id, case["photo_file_id"], text, admin_case_keyboard(case_id))
            else:
                await admin_bot_send_message(admin_id, text, admin_case_keyboard(case_id))


async def admin_bot_send_message(chat_id: int, text: str, reply_markup: InlineKeyboardMarkup | None = None) -> None:
    if not admin_bot_instance:
        return
    await admin_bot_instance.send_message(chat_id, text, reply_markup=reply_markup)


async def admin_bot_send_photo(
    chat_id: int,
    photo_file_id: str,
    caption: str,
    reply_markup: InlineKeyboardMarkup | None = None,
) -> None:
    if not admin_bot_instance:
        return
    await admin_bot_instance.send_photo(chat_id, photo_file_id, caption=caption, reply_markup=reply_markup)


async def notify_public_user(user_id: int, text: str) -> None:
    if not public_bot:
        return
    with suppress(TelegramBadRequest, TelegramForbiddenError):
        await public_bot.send_message(user_id, text)


@admin_router.message(CommandStart())
async def admin_start(message: Message, state: FSMContext) -> None:
    if is_admin(message.from_user.id):
        await message.answer("✅ Админ-панель открыта.", reply_markup=admin_reply_keyboard())
        return
    await message.answer("🔐 Введите пароль администратора:")
    await state.set_state(AdminFlow.password)


@admin_router.message(AdminFlow.password)
async def admin_password(message: Message, state: FSMContext) -> None:
    password = os.getenv("ADMIN_PASSWORD", ADMIN_PASSWORD_DEFAULT)
    if (message.text or "").strip() != password:
        await message.answer("❌ Неверный пароль.")
        return
    add_admin(message)
    await state.clear()
    await message.answer("✅ Доступ выдан.", reply_markup=admin_reply_keyboard())


@admin_router.message(F.text == "📋 Меню")
@admin_router.message(Command("menu"))
async def admin_menu(message: Message) -> None:
    if not is_admin(message.from_user.id):
        await message.answer("Нет доступа. Нажмите /start и введите пароль.")
        return
    await message.answer("📋 Админ-меню", reply_markup=admin_reply_keyboard())


@admin_router.message(F.text == "🚫 Заблокировать/предупредить")
async def admin_manual_start(message: Message, state: FSMContext) -> None:
    if not is_admin(message.from_user.id):
        return
    await message.answer("Отправь ID, @username или перешли сообщение пользователя.", reply_markup=ReplyKeyboardRemove())
    await state.set_state(AdminFlow.manual_target)


@admin_router.message(AdminFlow.manual_target)
async def admin_manual_target(message: Message, state: FSMContext) -> None:
    target = None
    if message.forward_from:
        ensure_user(message.forward_from.id, message.forward_from.username, message.forward_from.full_name)
        target = get_user(message.forward_from.id)
    elif message.text:
        target = find_user(message.text)
    if not target:
        await message.answer("Не нашел пользователя. Отправь ID, @username или перешли сообщение.")
        return
    await state.update_data(target_id=target.user_id)
    await message.answer(
        f"Пользователь: {mention(target.user_id, target.full_name, target.username)}\n"
        f"ID: <code>{target.user_id}</code>\n"
        f"Статус: <b>{STATUS_LABELS.get(target.status, target.status)}</b>\n\n"
        "Что сделать?",
        reply_markup=manual_action_keyboard(),
    )
    await state.set_state(AdminFlow.manual_action)


@admin_router.callback_query(AdminFlow.manual_action, F.data.startswith("manual:"))
async def admin_manual_action(callback: CallbackQuery, state: FSMContext) -> None:
    action = callback.data.split(":", 1)[1]
    await state.update_data(action=action)
    await callback.answer()
    await callback.message.edit_text("На сколько часов? Можно 0.5, 1, 24 и т.д.")
    await state.set_state(AdminFlow.manual_duration)


@admin_router.message(AdminFlow.manual_duration)
async def admin_manual_duration(message: Message, state: FSMContext) -> None:
    try:
        hours = float((message.text or "").replace(",", "."))
    except ValueError:
        await message.answer("Напиши число часов. Например 0.5 или 24.")
        return
    if hours <= 0:
        await message.answer("Время должно быть больше 0.")
        return
    await state.update_data(hours=hours)
    await message.answer("Напиши причину:")
    await state.set_state(AdminFlow.manual_reason)


@admin_router.message(AdminFlow.manual_reason)
async def admin_manual_reason(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    target_id = int(data["target_id"])
    action = str(data["action"])
    hours = float(data["hours"])
    reason = message.text or "Без причины"
    until = (datetime.utcnow() + timedelta(hours=hours)).replace(microsecond=0).isoformat()

    if action == "block":
        set_status(target_id, "punished", reason, until)
        text = f"🔴 Вам назначено наказание до {until}.\nПричина: {html.escape(reason)}"
    else:
        user = get_user(target_id)
        with db() as conn:
            conn.execute(
                "UPDATE users SET warnings_count = warnings_count + 1 WHERE user_id = ?",
                (target_id,),
            )
        set_status(target_id, "suspicious", reason, until)
        text = f"⚠️ Вам выдано предупреждение до {until}.\nПричина: {html.escape(reason)}"

    await notify_public_user(target_id, text)
    await state.clear()
    await message.answer("✅ Готово.", reply_markup=admin_reply_keyboard())


@admin_router.callback_query(F.data.startswith("case:"))
async def admin_case_action(callback: CallbackQuery, state: FSMContext) -> None:
    if not is_admin(callback.from_user.id):
        await callback.answer("Нет доступа", show_alert=True)
        return

    _, case_id_raw, action = callback.data.split(":")
    case_id = int(case_id_raw)
    case = get_case(case_id)
    if not case or case["status"] != "pending":
        await callback.answer("Кейс уже обработан или не найден", show_alert=True)
        return

    if action == "reject_reply":
        await state.update_data(reject_case_id=case_id)
        await callback.answer()
        await callback.message.answer("Напиши сообщение пользователю:")
        await state.set_state(AdminFlow.reject_reply)
        return

    await callback.answer()
    if action == "approve":
        await approve_case(case_id, callback.from_user.id)
        await callback.message.edit_reply_markup(reply_markup=None)
        await callback.message.answer(f"✅ Кейс #{case_id} подтвержден.")
    else:
        await reject_case(case_id, callback.from_user.id)
        await callback.message.edit_reply_markup(reply_markup=None)
        await callback.message.answer(f"❌ Кейс #{case_id} отклонен.")


@admin_router.message(AdminFlow.reject_reply)
async def admin_reject_reply(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    case_id = int(data["reject_case_id"])
    await reject_case(case_id, message.from_user.id, message.text or "")
    await state.clear()
    await message.answer(f"💬 Кейс #{case_id} отклонен, ответ отправлен.", reply_markup=admin_reply_keyboard())


async def approve_case(case_id: int, admin_id: int) -> None:
    case = get_case(case_id)
    if not case:
        return
    resolve_case(case_id, "approved", admin_id)
    reporter_id = int(case["reporter_id"])
    target_id = int(case["target_id"]) if case["target_id"] else reporter_id

    if case["case_type"] == "complaint":
        updated = increment_complaint(target_id, case["text"] or "Подтвержденная жалоба")
        await notify_public_user(reporter_id, f"✅ Ваша жалоба #{case_id} подтверждена.")
        await notify_public_user(
            target_id,
            "⚠️ На вас подтверждена жалоба.\n"
            f"Статус: {STATUS_LABELS.get(updated.status, updated.status)}\n"
            f"Жалоб: {updated.complaints_count}",
        )
    else:
        set_status(reporter_id, "clean", None, None)
        with db() as conn:
            conn.execute(
                "UPDATE users SET warnings_count = 0, complaints_count = 0 WHERE user_id = ?",
                (reporter_id,),
            )
        await notify_public_user(reporter_id, f"✅ Апелляция #{case_id} подтверждена. Статус очищен.")


async def reject_case(case_id: int, admin_id: int, reply: str | None = None) -> None:
    case = get_case(case_id)
    if not case:
        return
    resolve_case(case_id, "rejected", admin_id, reply)
    reporter_id = int(case["reporter_id"])
    if case["case_type"] == "complaint":
        text = f"❌ Ваша жалоба #{case_id} отклонена."
    else:
        text = f"❌ Ваша апелляция #{case_id} отклонена."
    if reply:
        text += f"\n\nОтвет администратора:\n{html.escape(reply)}"
    await notify_public_user(reporter_id, text)


async def main() -> None:
    global public_bot, admin_bot_instance
    load_dotenv(BASE_DIR / ".env")
    public_token = os.getenv("PUBLIC_BOT_TOKEN")
    admin_token = os.getenv("ADMIN_BOT_TOKEN")
    if not public_token:
        raise RuntimeError("PUBLIC_BOT_TOKEN не найден")
    if not admin_token:
        raise RuntimeError("ADMIN_BOT_TOKEN не найден")

    logging.basicConfig(level=logging.INFO)
    init_db()

    public_bot = Bot(public_token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    admin_bot_instance = Bot(admin_token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))

    public_dp = Dispatcher(storage=MemoryStorage())
    admin_dp = Dispatcher(storage=MemoryStorage())
    public_dp.include_router(public_router)
    admin_dp.include_router(admin_router)

    await public_bot.delete_webhook(drop_pending_updates=True)
    await admin_bot_instance.delete_webhook(drop_pending_updates=True)

    await asyncio.gather(
        public_dp.start_polling(public_bot),
        admin_dp.start_polling(admin_bot_instance),
    )


if __name__ == "__main__":
    asyncio.run(main())
