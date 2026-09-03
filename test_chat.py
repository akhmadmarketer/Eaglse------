import json
import os
import urllib.error
import urllib.request
import uuid


SERVER_URL = os.getenv("EAGLES_BOT_URL", "http://127.0.0.1:8000").rstrip("/")


def post(path: str, payload: dict) -> dict:
    request = urllib.request.Request(
        f"{SERVER_URL}{path}",
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=90) as response:
        return json.loads(response.read().decode("utf-8"))


def main() -> None:
    session_id = f"manual-{uuid.uuid4().hex}"
    print("Тестовый диалог с Eagles Bot")
    print("Команды: /reset — новый диалог, /exit — завершить тест.\n")

    while True:
        try:
            message = input("Вы: ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\nТест завершён.")
            return

        if not message:
            continue
        if message == "/exit":
            print("Тест завершён.")
            return
        if message == "/reset":
            post("/chat/reset", {"session_id": session_id})
            print("История диалога очищена.\n")
            continue

        try:
            result = post(
                "/chat", {"session_id": session_id, "message": message}
            )
            print(f"Бот: {result['reply']}\n")
        except urllib.error.HTTPError as error:
            print(f"Ошибка сервера: HTTP {error.code}\n")
        except urllib.error.URLError:
            print("Сервер недоступен. Сначала запустите: python3 app.py\n")


if __name__ == "__main__":
    main()
