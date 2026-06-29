# Telegram Chat Roulette Bot

Telegram bot in chat-roulette format: a user fills out a profile, chooses who they are looking for and why, then the bot matches them with an active compatible partner.

The project is built with **Python**, **aiogram 3**, and **SQLite**. It also includes a terminal test client that works like a second user, so you can test matching without a second Telegram account.

## Features

- Rules confirmation on the first `/start`.
- User profile: gender, age 13+, name, target partner, and purpose.
- Main menu with profile info, subscription status, and reputation.
- Active users counter in the main menu.
- Editing separate profile fields.
- Partner search by gender, purpose, and mutual preferences.
- Anonymous chat between two users through the bot.
- Forwarding text, photos, videos, voice messages, documents, and other Telegram messages.
- Chat ending flow.
- Partner rating: like, dislike, or skip.
- Complaint button after chat ending.
- Server-level terminal test user for checking the hosted bot without a second Telegram account.

## Tech Stack

- Python 3.11+
- aiogram 3
- SQLite
- python-dotenv

## Installation

Install dependencies:

```powershell
pip install -r requirements.txt
```

Create a Telegram bot with [BotFather](https://t.me/BotFather) and copy the bot token.

## Quick Start

Run the launcher:

```powershell
python run_bot.py
```

The launcher will:

- ask for your Telegram bot token;
- ask whether to save it into `.env`;
- install dependencies;
- start the bot immediately.

On Windows, you can also run:

```powershell
start_bot.bat
```

## Manual Setup

Create `.env` manually:

```env
BOT_TOKEN=your_telegram_bot_token_here
```

Then start the bot:

```powershell
python bot.py
```

## Server-Level Terminal Test Client

The terminal client lets you test the hosted bot without a second Telegram account.

It does not log in to Telegram as a real user. Instead, it connects to private `/tester/...` endpoints on your running bot server and creates a server-side test user.

On the hosting panel, set an environment variable:

```env
TESTER_SECRET=any_private_password_here
```

Then run the terminal client locally:

```powershell
python console_client.py
```

The script asks for:

- server URL, for example `https://your-app.example.com`;
- `TESTER_SECRET`.

You can also put them into your local `.env`:

```env
CONSOLE_SERVER_URL=https://your-app.example.com
TESTER_SECRET=any_private_password_here
```

On Windows:

```powershell
console_client.bat
```

The terminal profile is filled by choosing numbers. After that, the terminal user gets a menu similar to Telegram:

- start search;
- edit profile;
- refresh profile;
- exit.

If the server-side test user and a Telegram user match by profile settings, the bot connects them into one chat.

Terminal chat commands:

```text
/stop
```

End the current chat.

Files are not supported in this server-level tester yet, because the file is on your PC while the bot is running on the server.

## Local Bot Launch

For local development, you can still run the bot directly:

```powershell
python run_bot.py
```


## Matching Logic

A user enters the waiting queue. When another active user appears, the bot checks:

- whether both users have the same purpose;
- whether the first user's gender matches the second user's preference;
- whether the second user's gender matches the first user's preference.

If everything is compatible, the bot creates an active chat and starts relaying messages between both users.

## Project Structure

```text
.
|-- bot.py              # Main Telegram bot
|-- console_client.py   # Server-level terminal test user
|-- run_bot.py          # Launcher with token prompt
|-- start_bot.bat       # Windows launcher for the bot
|-- console_client.bat  # Windows launcher for the terminal client
|-- requirements.txt    # Python dependencies
|-- .env.example        # Example environment file
`-- README.md
```

After the first run, `bot.sqlite3` will appear in the project folder. It stores profiles, the waiting queue, active chats, reputation, and terminal inbox messages.

## Useful Commands

Check Python syntax:

```powershell
python -m py_compile bot.py run_bot.py console_client.py
```

Stop the bot:

```text
Ctrl+C
```

## Important

`.env` contains your bot token. Do not publish it to GitHub.

`bot.sqlite3` contains local user data. Usually it should not be committed either.

## Roadmap

- Full complaint system.
- User blocking.
- Admin panel.
- Paid subscriptions.
- More search filters.
- Complaint history.
- Spam protection.
