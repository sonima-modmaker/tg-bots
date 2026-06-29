import json
import os
import ssl
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from dotenv import load_dotenv


BASE_DIR = Path(__file__).resolve().parent

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


class ServerClient:
    def __init__(self, base_url: str, secret: str) -> None:
        self.base_url = self.normalize_base_url(base_url)
        self.secret = secret
        self.validate_base_url()

    def normalize_base_url(self, base_url: str) -> str:
        value = base_url.strip().rstrip("/")
        for suffix in ("/health", "/tester/profile"):
            if value.lower().endswith(suffix):
                value = value[: -len(suffix)]
                break
        return value.rstrip("/")

    def validate_base_url(self) -> None:
        lowered = self.base_url.lower()
        if "/panel/" in lowered or lowered.endswith("/panel"):
            raise RuntimeError(
                "Ты указал ссылку на панель управления, а нужен публичный URL приложения. "
                "Открой JustRunMy, найди URL/Domain/Endpoint приложения, где /health отвечает 'Bot is running'."
            )

    def request(self, method: str, path: str, payload: dict | None = None) -> dict:
        url = f"{self.base_url}{path}"
        body = None
        headers = {"X-Tester-Secret": self.secret}

        if method == "GET":
            separator = "&" if "?" in url else "?"
            url = f"{url}{separator}{urllib.parse.urlencode({'secret': self.secret})}"
        else:
            body = json.dumps(payload or {}).encode("utf-8")
            headers["Content-Type"] = "application/json"

        request = urllib.request.Request(url, data=body, headers=headers, method=method)
        try:
            return self.open_json(request)
        except urllib.error.HTTPError as exc:
            text = exc.read().decode("utf-8", errors="replace")
            hint = ""
            if exc.code == 404:
                hint = (
                    "\nПохоже, URL неправильный. Нужна ссылка на само приложение, "
                    "а не на dashboard/panel и не ссылка с /health на конце. "
                    "Например: https://gitr_gs3m7-bc9.d.jrnm.app"
                )
            raise RuntimeError(f"Server returned {exc.code}: {text}{hint}") from exc
        except urllib.error.URLError as exc:
            if "CERTIFICATE_VERIFY_FAILED" in str(exc):
                print(
                    "SSL-сертификат хостинга не прошел проверку имени. "
                    "Повторяю запрос в тестовом режиме без проверки SSL."
                )
                try:
                    return self.open_json(request, verify_ssl=False)
                except urllib.error.HTTPError as fallback_exc:
                    text = fallback_exc.read().decode("utf-8", errors="replace")
                    hint = ""
                    if fallback_exc.code == 502:
                        hint = (
                            "\nСервер вернул 502 Bad Gateway. Обычно это значит, что контейнер бота "
                            "упал или еще не запустился. Проверь логи хостинга, BOT_TOKEN, TESTER_SECRET "
                            "и что локально не запущен второй экземпляр этого же бота."
                        )
                    raise RuntimeError(
                        f"Server returned {fallback_exc.code}: {text}{hint}"
                    ) from fallback_exc
            raise RuntimeError(f"Cannot connect to server: {exc}") from exc

    def open_json(self, request: urllib.request.Request, verify_ssl: bool = True) -> dict:
        context = None if verify_ssl else ssl._create_unverified_context()
        with urllib.request.urlopen(request, timeout=20, context=context) as response:
            return json.loads(response.read().decode("utf-8"))

    def get_profile(self) -> dict:
        return self.request("GET", "/tester/profile")["profile"]

    def save_profile(self, data: dict) -> dict:
        return self.request("POST", "/tester/profile", data)["profile"]

    def search(self) -> dict:
        return self.request("POST", "/tester/search", {})

    def cancel_search(self) -> None:
        self.request("POST", "/tester/cancel", {})

    def inbox(self) -> dict:
        return self.request("GET", "/tester/inbox")

    def send(self, text: str) -> None:
        self.request("POST", "/tester/send", {"text": text})

    def stop(self) -> int | None:
        return self.request("POST", "/tester/stop", {}).get("partner_id")

    def rate(self, partner_id: int, rating: str) -> None:
        self.request("POST", "/tester/rate", {"partner_id": partner_id, "rating": rating})


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


def fill_profile(client: ServerClient) -> None:
    print("\nЗаполним серверную тестовую анкету.")
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
    client.save_profile(
        {
            "gender": gender,
            "age": age,
            "name": name,
            "looking_for": looking_for,
            "purpose": purpose,
        }
    )
    print("Анкета сохранена на сервере.")


def show_profile(client: ServerClient) -> None:
    profile = client.get_profile()
    if not profile.get("complete"):
        print("\nАнкета еще не заполнена.")
        return

    print("\n=== ✨ Твоя серверная анкета ===")
    print(f"Имя: {profile.get('name')}")
    print(f"Пол: {GENDERS.get(profile.get('gender'), profile.get('gender'))}")
    print(f"Возраст: {profile.get('age')}")
    print(f"Ищешь: {LOOKING_FOR.get(profile.get('looking_for'), profile.get('looking_for'))}")
    print(f"Цель: {PURPOSES.get(profile.get('purpose'), profile.get('purpose'))}")
    print(f"Подписка: {profile.get('subscription')}")
    print(f"Репутация: {profile.get('likes')} лайков, {profile.get('dislikes')} дизлайков")
    print(f"Активно сейчас: {profile.get('active_count')}")


def edit_profile(client: ServerClient) -> None:
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
    client.save_profile({field: value})
    print("Сохранено.")


def wait_for_match(client: ServerClient) -> bool:
    print("\nИщу активного собеседника на сервере.")
    print("Нажми Enter, чтобы проверить сразу, или введи /cancel для отмены.")
    while True:
        inbox = client.inbox()
        for message in inbox.get("messages", []):
            print(f"\n{message}")
        if inbox.get("partner_id"):
            print("\nСобеседник найден.")
            return True

        command = input("Ожидание... ").strip()
        if command == "/cancel":
            client.cancel_search()
            print("Поиск отменен.")
            return False


def inbox_worker(client: ServerClient, stop_event: threading.Event) -> None:
    while not stop_event.is_set():
        try:
            inbox = client.inbox()
            for message in inbox.get("messages", []):
                print(f"\n{message}")
                print("> ", end="", flush=True)
        except Exception as exc:
            print(f"\nОшибка получения сообщений: {exc}")
        time.sleep(1)


def rate_partner(client: ServerClient, partner_id: int) -> None:
    action = choose(
        "Оцени собеседника:",
        [("like", "👍 Лайк"), ("dislike", "👎 Дизлайк"), ("skip", "⏭️ Пропустить"), ("report", "🚩 Пожаловаться")],
    )
    if action in {"like", "dislike", "skip"}:
        client.rate(partner_id, action)
    if action == "report":
        print("Жалоба принята. Пока без функции.")
    else:
        print("Готово.")


def chat_loop(client: ServerClient) -> None:
    print("\nВы начали беседу.")
    print("Пиши текст и нажимай Enter. Команды: /stop")
    print("Файлы в серверном тестере пока не поддерживаются.")

    stop_event = threading.Event()
    thread = threading.Thread(target=inbox_worker, args=(client, stop_event), daemon=True)
    thread.start()

    try:
        while True:
            text = input("> ").strip()
            if not text:
                continue
            if text == "/stop":
                partner_id = client.stop()
                print("Чат завершен.")
                if partner_id:
                    rate_partner(client, int(partner_id))
                return
            if text.startswith("/file "):
                print("Файлы в серверном тестере пока не поддерживаются.")
                continue
            try:
                client.send(text)
            except RuntimeError as exc:
                print(exc)
                return
    finally:
        stop_event.set()
        thread.join(timeout=2)


def start_search(client: ServerClient) -> None:
    result = client.search()
    status = result.get("status")
    if status == "chat":
        chat_loop(client)
        return
    if status == "waiting" and wait_for_match(client):
        chat_loop(client)


def create_client() -> ServerClient:
    load_dotenv(BASE_DIR / ".env")
    server_url = os.getenv("CONSOLE_SERVER_URL") or input("Публичный URL приложения без /health, например https://gitr_gs3m7-bc9.d.jrnm.app: ").strip()
    secret = os.getenv("TESTER_SECRET") or input("TESTER_SECRET с сервера: ").strip()
    if not server_url or not secret:
        raise RuntimeError("Нужны CONSOLE_SERVER_URL и TESTER_SECRET.")
    return ServerClient(server_url, secret)


def main() -> None:
    client = create_client()
    profile = client.get_profile()
    if profile.get("complete"):
        use_saved = choose(
            "Нашел серверную тестовую анкету. Использовать?",
            [("yes", "Да"), ("no", "Заполнить заново")],
        )
        if use_saved == "no":
            fill_profile(client)
    else:
        fill_profile(client)

    while True:
        show_profile(client)
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
            start_search(client)
        elif action == "edit":
            edit_profile(client)
        elif action == "refresh":
            continue
        else:
            break


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nВыход.")
