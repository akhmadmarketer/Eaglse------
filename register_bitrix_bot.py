import json
import os
import sys
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


load_env_file(ROOT / ".env.local")

USAGE = """Использование:
  python3 register_bitrix_bot.py <https URL обработчика>            регистрация нового бота
  python3 register_bitrix_bot.py --update <https URL обработчика>   смена адреса у бота из B24_BOT_ID

Публичный адрес меняется при каждом перезапуске туннеля, поэтому обычный режим
работы — `--update`. Повторная регистрация создаёт в портале второго бота-дубля.
"""

webhook_url = os.getenv("B24_WEBHOOK_URL", "").strip().rstrip("/")
bot_token = os.getenv("B24_BOT_TOKEN", "").strip()

args = [value.strip() for value in sys.argv[1:]]
update_mode = "--update" in args
positional = [value for value in args if not value.startswith("--")]
handler_url = positional[0] if positional else ""

if not webhook_url or not bot_token:
    raise SystemExit("B24_WEBHOOK_URL или B24_BOT_TOKEN не настроен")
if not handler_url.startswith("https://"):
    raise SystemExit(USAGE)

if update_mode:
    bot_id = os.getenv("B24_BOT_ID", "").strip()
    if not bot_id.isdigit():
        raise SystemExit("B24_BOT_ID не настроен — обновлять нечего")
    method = "imbot.v2.Bot.update"
    payload = {
        "botId": int(bot_id),
        "botToken": bot_token,
        "fields": {"webhookUrl": handler_url},
    }
else:
    method = "imbot.v2.Bot.register"
    payload = {
        "fields": {
            "code": "eagles_openai_bot",
            "botToken": bot_token,
            "type": "bot",
            "isSupportOpenline": True,
            "eventMode": "webhook",
            "webhookUrl": handler_url,
            "properties": {
                "name": "Eagles OpenAI Bot",
                "workPosition": "AI-консультант первой линии",
                "color": "GREEN",
            },
        }
    }

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

returned_bot_id = result.get("result", {}).get("bot", {}).get("id")
if not returned_bot_id:
    raise SystemExit("Bitrix24 не вернул ID бота")

if update_mode:
    print(f"status=updated bot_id={returned_bot_id}")
    print("Адрес обработчика заменён. B24_BOT_ID менять не нужно.")
else:
    print(f"status=registered bot_id={returned_bot_id}")
    print("Сохраните этот bot_id в B24_BOT_ID файла .env.local.")
