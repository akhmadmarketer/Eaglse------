import argparse
import json
import os
import urllib.error
import urllib.request
from pathlib import Path


ROOT = Path(__file__).resolve().parent


def load_env_file(path: Path) -> None:
    if not path.is_file():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def call_bitrix(method: str, payload: dict) -> object:
    webhook_url = os.getenv("B24_WEBHOOK_URL", "").strip().rstrip("/")
    if not webhook_url:
        raise SystemExit("B24_WEBHOOK_URL не настроен")

    request = urllib.request.Request(
        f"{webhook_url}/{method}",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", "Accept": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            result = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raise SystemExit(f"Bitrix24 HTTP error: {exc.code}") from exc
    except (urllib.error.URLError, json.JSONDecodeError) as exc:
        raise SystemExit("Не удалось получить корректный ответ Bitrix24") from exc

    if result.get("error"):
        error_code = result.get("error", "unknown_error")
        error_description = result.get("error_description", "")
        raise SystemExit(f"Bitrix24 error: {error_code} — {error_description}")
    return result.get("result")


def list_lines() -> None:
    result = call_bitrix("imopenlines.config.list.get", {})
    lines = result if isinstance(result, list) else []
    if not lines:
        print("open_lines=none")
        return
    for item in lines:
        line_id = item.get("ID", item.get("id", "?"))
        line_name = item.get("LINE_NAME", item.get("lineName", "Без названия"))
        active = item.get("ACTIVE", item.get("active", "?"))
        print(f"id={line_id} active={active} name={line_name}")


def connect_bot(config_id: int) -> None:
    bot_id = os.getenv("B24_BOT_ID", "").strip()
    if not bot_id.isdigit():
        raise SystemExit("B24_BOT_ID не настроен")

    result = call_bitrix(
        "imopenlines.config.update",
        {
            "CONFIG_ID": config_id,
            "PARAMS": {
                "WELCOME_BOT_ENABLE": "Y",
                "WELCOME_BOT_JOIN": "always",
                "WELCOME_BOT_ID": int(bot_id),
                "WELCOME_BOT_TIME": 3600,
                "WELCOME_BOT_LEFT": "close",
            },
        },
    )
    print(f"status={'connected' if result is True else 'not_connected'} config_id={config_id}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Настройка чат-бота Открытой линии Bitrix24")
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("list", help="Показать список Открытых линий")
    connect_parser = subparsers.add_parser("connect", help="Подключить бота к Открытой линии")
    connect_parser.add_argument("config_id", type=int)
    args = parser.parse_args()

    load_env_file(ROOT / ".env.local")
    if args.command == "list":
        list_lines()
    elif args.command == "connect":
        connect_bot(args.config_id)


if __name__ == "__main__":
    main()
