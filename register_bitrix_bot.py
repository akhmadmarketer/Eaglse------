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

webhook_url = os.getenv("B24_WEBHOOK_URL", "").strip().rstrip("/")
bot_token = os.getenv("B24_BOT_TOKEN", "").strip()
handler_url = sys.argv[1].strip() if len(sys.argv) > 1 else ""

if not webhook_url or not bot_token:
    raise SystemExit("B24_WEBHOOK_URL или B24_BOT_TOKEN не настроен")
if not handler_url.startswith("https://"):
    raise SystemExit("Передайте публичный HTTPS URL обработчика первым аргументом")

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
    f"{webhook_url}/imbot.v2.Bot.register",
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

bot_id = result.get("result", {}).get("bot", {}).get("id")
if not bot_id:
    raise SystemExit("Bitrix24 не вернул ID зарегистрированного бота")

print(f"status=registered bot_id={bot_id}")
