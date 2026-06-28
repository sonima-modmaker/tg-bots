import asyncio
import os
from pathlib import Path

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError
from dotenv import load_dotenv
from telethon import TelegramClient, events


BASE_DIR = Path(__file__).resolve().parent
SESSION_NAME = str(BASE_DIR / "real_tg_tester")


class BotTester:
    def __init__(self, client: TelegramClient, bot_username: str) -> None:
        self.client = client
        self.bot_username = bot_username
        self.bot_entity = None
        self.buttons: dict[int, tuple[object, int, int, str]] = {}

    async def start(self) -> None:
        self.bot_entity = await self.client.get_entity(self.bot_username)
        self.client.add_event_handler(
            self.on_bot_message,
            events.NewMessage(chats=self.bot_entity),
        )

        print(f"\nConnected to @{self.bot_username}.")
        print("Type text and press Enter to send it to the bot.")
        print("When buttons appear, type their number: 1, 2, 3...")
        print("Commands: /start, /exit\n")

        await self.client.send_message(self.bot_entity, "/start")
        await self.input_loop()

    async def on_bot_message(self, event: events.NewMessage.Event) -> None:
        message = event.message
        text = message.raw_text or "[non-text message]"
        print(f"\nBOT: {text}")
        self.show_buttons(message)
        print("> ", end="", flush=True)

    def show_buttons(self, message: object) -> None:
        self.buttons.clear()
        rows = getattr(message, "buttons", None)
        if not rows:
            return

        index = 1
        print("\nButtons:")
        for row_index, row in enumerate(rows):
            for button_index, button in enumerate(row):
                label = getattr(button, "text", str(button))
                self.buttons[index] = (message, row_index, button_index, label)
                print(f"{index}. {label}")
                index += 1

    async def input_loop(self) -> None:
        while True:
            user_input = await asyncio.to_thread(input, "> ")
            user_input = user_input.strip()
            if not user_input:
                continue
            if user_input == "/exit":
                print("Exit.")
                return
            if user_input.isdigit() and int(user_input) in self.buttons:
                await self.click_button(int(user_input))
                continue
            await self.client.send_message(self.bot_entity, user_input)

    async def click_button(self, number: int) -> None:
        message, row_index, button_index, label = self.buttons[number]
        print(f"Clicked: {label}")
        try:
            await message.click(row_index, button_index)
        except Exception as exc:
            print(f"Could not click button: {exc}")


async def get_bot_username(bot_token_or_username: str) -> str:
    value = bot_token_or_username.strip()
    if not value:
        raise RuntimeError("Bot token or username is empty.")

    if ":" not in value:
        return value.removeprefix("@")

    bot = Bot(value)
    try:
        me = await bot.get_me()
    except TelegramAPIError as exc:
        raise RuntimeError(f"Could not get bot username from token: {exc}") from exc
    finally:
        await bot.session.close()

    if not me.username:
        raise RuntimeError("This bot does not have a username.")
    return me.username


def ask_value(title: str, env_key: str | None = None) -> str:
    saved = os.getenv(env_key, "") if env_key else ""
    if saved:
        answer = input(f"{title} [press Enter to use saved]: ").strip()
        return answer or saved
    return input(f"{title}: ").strip()


async def main() -> None:
    load_dotenv(BASE_DIR / ".env")

    print("Real Telegram bot tester")
    print("You need Telegram API credentials from https://my.telegram.org/apps")
    print("This logs in as your Telegram user account, not as a bot.\n")

    bot_token_or_username = ask_value("Bot token or @username", "BOT_TOKEN")
    api_id_raw = ask_value("API_ID", "TELEGRAM_API_ID")
    api_hash = ask_value("API_HASH", "TELEGRAM_API_HASH")
    phone = ask_value("Phone number, for example +380...", "TELEGRAM_PHONE")

    if not api_id_raw.isdigit():
        print("API_ID must be a number.")
        return

    bot_username = await get_bot_username(bot_token_or_username)

    client = TelegramClient(SESSION_NAME, int(api_id_raw), api_hash)
    await client.start(phone=phone)

    tester = BotTester(client, bot_username)
    try:
        await tester.start()
    finally:
        await client.disconnect()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nStopped.")
