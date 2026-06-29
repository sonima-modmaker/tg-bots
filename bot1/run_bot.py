import asyncio
import os
import subprocess
import sys
from pathlib import Path


BASE_DIR = Path(__file__).resolve().parent
ENV_PATH = BASE_DIR / ".env"
BOT_PATH = BASE_DIR / "bot.py"


def read_saved_token() -> str:
    if not ENV_PATH.exists():
        return ""

    for line in ENV_PATH.read_text(encoding="utf-8").splitlines():
        if line.startswith("BOT_TOKEN="):
            return line.split("=", 1)[1].strip()
    return ""


def save_token(token: str) -> None:
    ENV_PATH.write_text(f"BOT_TOKEN={token}\n", encoding="utf-8")


def ask_yes_no(question: str) -> bool:
    answer = input(question).strip().lower()
    return answer in {"y", "yes", "д", "да"}


def install_requirements() -> None:
    requirements = BASE_DIR / "requirements.txt"
    if not requirements.exists():
        return

    print("Проверяю зависимости...")
    subprocess.check_call([sys.executable, "-m", "pip", "install", "-r", str(requirements)])


async def main() -> None:
    saved_token = read_saved_token()

    if saved_token:
        print("В .env уже есть сохраненный токен.")
        token = input("Введи новый токен или нажми Enter, чтобы использовать сохраненный: ").strip()
        if not token:
            token = saved_token
    else:
        token = input("Введи токен Telegram-бота от BotFather: ").strip()

    if not token:
        print("Токен не введен. Запуск отменен.")
        return

    if token != saved_token and ask_yes_no("Сохранить токен в .env? (да/нет): "):
        save_token(token)
        print("Токен сохранен.")

    os.environ["BOT_TOKEN"] = token
    install_requirements()

    print("Запускаю бота. Для остановки нажми Ctrl+C.")
    from bot import main as bot_main

    await bot_main()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nБот остановлен.")
