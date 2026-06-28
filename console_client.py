import asyncio
import msvcrt
import os
import time
from contextlib import suppress
from pathlib import Path

from aiogram import Bot
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram.types import FSInputFile
from dotenv import load_dotenv

from bot import (
    AGES,
    DB_PATH,
    GENDERS,
    LOOKING_FOR,
    PURPOSES,
    add_to_queue,
    close_chat,
    create_chat,
    db,
    ensure_user,
    find_partner_for,
    get_active_users_count,
    get_partner,
    get_profile,
    increment_reputation,
    init_db,
    profile_is_complete,
    push_console_message,
    rating_keyboard,
    remove_from_queue,
    update_user,
)


BASE_DIR = Path(__file__).resolve().parent
CONSOLE_USER_ID = -1


def choose(title: str, options: list[tuple[str, str]]) -> str:
    while True:
        print(f"\n{title}")
        for index, (_, label) in enumerate(options, start=1):
            print(f"{index}. {label}")
        answer = input("Выбери номер: ").strip()
        if answer.isdigit() and 1 <= int(answer) <= len(options):
            return options[int(answer) - 1][0]
        print("Нет такого варианта, попробуй еще раз.")


def ask_name() -> str:
    while True:
        name = input("\nВведи имя для анкеты: ").strip()
        if 2 <= len(name) <= 32:
            return name
        print("Имя должно быть от 2 до 32 символов.")


def fill_profile() -> bool:
    print("\nЗаполним тестовую анкету терминального пользователя.")
    gender = choose("Твой пол:", [("male", "Парень"), ("female", "Девушка")])
    age = choose("Возраст, не меньше 13:", [(age, age) for age in AGES])
    name = ask_name()
    looking_for = choose(
        "Кого ищешь?",
        [("male", "Парня"), ("female", "Девушку"), ("any", "Не важно")],
    )
    purpose = choose(
        "Для чего ищешь?",
        [("chat", "Общение"), ("relationship", "Отношения")],
    )

    ensure_user(CONSOLE_USER_ID)
    update_user(
        CONSOLE_USER_ID,
        gender=gender,
        age=age,
        name=name,
        looking_for=looking_for,
        purpose=purpose,
        accepted_rules=1,
    )

    save = choose("Сохранить анкету для следующих запусков?", [("yes", "Да"), ("no", "Нет")])
    return save == "yes"


def show_profile() -> None:
    profile = get_profile(CONSOLE_USER_ID)
    if not profile_is_complete(profile):
        print("\nАнкета не заполнена.")
        return

    print("\n=== ✨ Твоя анкета ===")
    print(f"Имя: {profile.name}")
    print(f"Пол: {GENDERS.get(profile.gender, profile.gender)}")
    print(f"Возраст: {profile.age}")
    print(f"Ищешь: {LOOKING_FOR.get(profile.looking_for, profile.looking_for)}")
    print(f"Цель: {PURPOSES.get(profile.purpose, profile.purpose)}")
    print(f"Подписка: {profile.subscription}")
    print(f"Репутация: {profile.likes} лайков, {profile.dislikes} дизлайков")
    print(f"Активно сейчас: {get_active_users_count()}")


def edit_profile() -> None:
    field = choose(
        "Что переделать?",
        [
            ("gender", "Пол"),
            ("age", "Возраст"),
            ("name", "Имя"),
            ("looking_for", "Кого ищешь"),
            ("purpose", "Цель"),
            ("back", "Назад"),
        ],
    )
    if field == "back":
        return
    if field == "gender":
        value = choose("Новый пол:", [("male", "Парень"), ("female", "Девушка")])
    elif field == "age":
        value = choose("Новый возраст:", [(age, age) for age in AGES])
    elif field == "name":
        value = ask_name()
    elif field == "looking_for":
        value = choose("Кого ищешь?", [("male", "Парня"), ("female", "Девушку"), ("any", "Не важно")])
    else:
        value = choose("Новая цель:", [("chat", "Общение"), ("relationship", "Отношения")])
    update_user(CONSOLE_USER_ID, **{field: value})
    print("Сохранено.")


def fetch_console_messages() -> list[str]:
    with db() as conn:
        rows = conn.execute(
            "SELECT id, message FROM console_inbox WHERE user_id = ? ORDER BY id ASC",
            (CONSOLE_USER_ID,),
        ).fetchall()
        if rows:
            conn.execute(
                "DELETE FROM console_inbox WHERE id IN (%s)"
                % ",".join("?" for _ in rows),
                [row["id"] for row in rows],
            )
    return [row["message"] for row in rows]


async def inbox_printer(stop_event: asyncio.Event) -> None:
    while not stop_event.is_set():
        for message in fetch_console_messages():
            print(f"\n{message}")
            print("> ", end="", flush=True)
        await asyncio.sleep(1)


async def wait_for_match() -> bool:
    print("\nИщу активного собеседника. Оставь это окно открытым.")
    print("Нажми C, чтобы отменить поиск.")
    last_dot = time.monotonic()
    while True:
        if get_partner(CONSOLE_USER_ID):
            print("\nСобеседник найден.")
            return True

        for message in fetch_console_messages():
            print(f"\n{message}")

        if msvcrt.kbhit():
            key = msvcrt.getwch().lower()
            if key in {"c", "с"}:
                remove_from_queue(CONSOLE_USER_ID)
                print("\nПоиск отменен.")
                return False

        if time.monotonic() - last_dot >= 5:
            print("Ищу дальше...")
            last_dot = time.monotonic()
        await asyncio.sleep(1)


async def send_text_to_partner(bot: Bot, text: str) -> None:
    partner_id = get_partner(CONSOLE_USER_ID)
    if not partner_id:
        print("Активного чата уже нет.")
        return
    if partner_id < 0:
        push_console_message(partner_id, f"Собеседник: {text}")
        return
    with suppress(TelegramBadRequest, TelegramForbiddenError):
        await bot.send_message(partner_id, text)


async def send_file_to_partner(bot: Bot, path_raw: str) -> None:
    partner_id = get_partner(CONSOLE_USER_ID)
    path = Path(path_raw.strip('"')).expanduser()
    if not partner_id:
        print("Активного чата уже нет.")
        return
    if not path.exists() or not path.is_file():
        print("Файл не найден.")
        return
    if partner_id < 0:
        push_console_message(partner_id, f"Собеседник отправил файл: {path.name}")
        return
    with suppress(TelegramBadRequest, TelegramForbiddenError):
        await bot.send_document(partner_id, document=FSInputFile(path))


def rate_partner(partner_id: int) -> None:
    action = choose(
        "Оцени собеседника:",
        [("like", "Лайк"), ("dislike", "Дизлайк"), ("skip", "Пропустить"), ("report", "Пожаловаться")],
    )
    if action == "like":
        increment_reputation(partner_id, "likes")
        print("Лайк засчитан.")
    elif action == "dislike":
        increment_reputation(partner_id, "dislikes")
        print("Дизлайк засчитан.")
    elif action == "report":
        print("Жалоба принята. Пока без функции.")
    else:
        print("Оценка пропущена.")


async def chat_loop(bot: Bot) -> None:
    print("\nВы начали беседу.")
    print("Пиши текст и нажимай Enter. Команды: /stop, /file путь_к_файлу")
    stop_event = asyncio.Event()
    printer_task = asyncio.create_task(inbox_printer(stop_event))
    try:
        while get_partner(CONSOLE_USER_ID):
            text = await asyncio.to_thread(input, "> ")
            text = text.strip()
            if not text:
                continue
            if text == "/stop":
                partner_id = close_chat(CONSOLE_USER_ID)
                if partner_id:
                    if partner_id < 0:
                        push_console_message(partner_id, "Собеседник завершил чат.")
                    else:
                        with suppress(TelegramBadRequest, TelegramForbiddenError):
                            await bot.send_message(
                                partner_id,
                                "Собеседник завершил чат. Оцени общение:",
                                reply_markup=rating_keyboard(CONSOLE_USER_ID),
                            )
                    print("Чат завершен.")
                    rate_partner(partner_id)
                break
            if text.startswith("/file "):
                await send_file_to_partner(bot, text.removeprefix("/file ").strip())
                continue
            await send_text_to_partner(bot, text)
        if not get_partner(CONSOLE_USER_ID):
            print("\nАктивный чат завершен.")
    finally:
        stop_event.set()
        await printer_task


async def start_search(bot: Bot) -> None:
    profile = get_profile(CONSOLE_USER_ID)
    if not profile_is_complete(profile):
        print("Сначала заполни анкету.")
        return
    if get_partner(CONSOLE_USER_ID):
        await chat_loop(bot)
        return

    partner_id = find_partner_for(profile)
    if partner_id:
        create_chat(CONSOLE_USER_ID, partner_id)
        if partner_id >= 0:
            with suppress(TelegramBadRequest, TelegramForbiddenError):
                await bot.send_message(
                    partner_id,
                    "Собеседник найден. Вы начали беседу. Пиши сообщение, а я передам его дальше.",
                )
        await chat_loop(bot)
        return

    add_to_queue(CONSOLE_USER_ID)
    print("Когда пользователь из Telegram нажмет поиск и подойдет по анкете, чат начнется.")
    if await wait_for_match():
        await chat_loop(bot)


def cleanup_console_user() -> None:
    partner_id = close_chat(CONSOLE_USER_ID)
    remove_from_queue(CONSOLE_USER_ID)
    with db() as conn:
        conn.execute("DELETE FROM console_inbox WHERE user_id = ?", (CONSOLE_USER_ID,))
        conn.execute("DELETE FROM users WHERE user_id = ?", (CONSOLE_USER_ID,))
    if partner_id:
        print("Временная анкета удалена, активный чат закрыт.")


async def main() -> None:
    load_dotenv(BASE_DIR / ".env")
    token = os.getenv("BOT_TOKEN") or input("Введи BOT_TOKEN: ").strip()
    if not token:
        print("Токен не введен.")
        return

    init_db()
    bot = Bot(token=token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))

    keep_profile = profile_is_complete(get_profile(CONSOLE_USER_ID))
    if keep_profile:
        use_saved = choose("Нашел сохраненную терминальную анкету. Использовать?", [("yes", "Да"), ("no", "Заполнить заново")])
        keep_profile = use_saved == "yes"
    if not keep_profile:
        keep_profile = fill_profile()

    try:
        while True:
            show_profile()
            action = choose(
                "Главное меню:",
                [
                    ("search", "🔎 Начать поиск"),
                    ("edit", "✏️ Переделать информацию"),
                    ("refresh", "🔄 Обновить анкету"),
                    ("exit", "🚪 Выход"),
                ],
            )
            if action == "search":
                await start_search(bot)
            elif action == "edit":
                edit_profile()
            elif action == "refresh":
                continue
            else:
                break
    finally:
        await bot.session.close()
        if not keep_profile:
            cleanup_console_user()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nВыход.")
