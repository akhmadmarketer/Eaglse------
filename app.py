import json
import os
import threading
import urllib.error
import urllib.parse
import urllib.request
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Deque, Dict, List, Optional, Tuple

from openai import OpenAI


ROOT = Path(__file__).resolve().parent
MAX_BODY_BYTES = 32 * 1024
MAX_MESSAGE_CHARS = 4_000
MAX_CONVERSATION_MESSAGES = 20
MAX_CONVERSATIONS = 500


def load_env_file(path: Path) -> None:
    """Load simple KEY=VALUE pairs without printing or replacing set variables."""
    if not path.is_file():
        return

    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue

        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip("\"").strip("'")
        if key:
            os.environ.setdefault(key, value)


load_env_file(ROOT / ".env.local")

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "").strip()
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-5.6-luna").strip()
PORT = int(os.getenv("PORT", "8000"))
B24_WEBHOOK_URL = os.getenv("B24_WEBHOOK_URL", "").strip().rstrip("/")
B24_BOT_ID = os.getenv("B24_BOT_ID", "").strip()
B24_BOT_TOKEN = os.getenv("B24_BOT_TOKEN", "").strip()
B24_APPLICATION_TOKEN = os.getenv("B24_APPLICATION_TOKEN", "").strip()

if not OPENAI_API_KEY:
    raise RuntimeError("OPENAI_API_KEY не найден в окружении или .env.local")

client = OpenAI(api_key=OPENAI_API_KEY)

# Защита от повторной обработки одного сообщения после повторной доставки webhook.
processed_message_ids = set()
processed_message_order = deque()
processed_message_lock = threading.Lock()
MAX_PROCESSED_MESSAGE_IDS = 2_000

conversation_histories: Dict[str, Deque[Dict[str, str]]] = {}
conversation_locks: Dict[str, threading.Lock] = {}
conversation_state_lock = threading.Lock()


def load_knowledge_base() -> str:
    knowledge_dir = ROOT / "knowledge"
    if not knowledge_dir.is_dir():
        return "База знаний ещё не подключена."

    chunks = []
    for path in sorted(knowledge_dir.rglob("*")):
        if path.suffix not in {".md", ".json"} or not path.is_file():
            continue
        relative = path.relative_to(ROOT)
        chunks.append(f"\n--- {relative} ---\n{path.read_text(encoding='utf-8')}")
    return "".join(chunks)


KNOWLEDGE_BASE = load_knowledge_base()

BOT_INSTRUCTIONS = f"""Ты — консультант Академии единоборств «ИГЛС».
Твоя задача — доброжелательно и активно вести диалог, понять потребность клиента
и помочь выбрать подходящее направление или следующий шаг.

Правила диалога:
1. Сначала прямо ответь на вопрос клиента, затем продолжи консультацию.
2. Учитывай всю историю беседы и не спрашивай повторно то, что клиент уже сообщил.
3. Если цель клиента ещё не ясна, в конце ответа задай ровно один короткий и
   уместный вопрос. Не задавай анкету из нескольких вопросов сразу.
4. Последовательно выясняй только нужное: цель, возраст занимающегося, опыт,
   предпочтение группы или персональных занятий и удобное время.
5. Не приветствуй клиента заново в каждом сообщении и не завершай разговор
   фразами вроде «обращайтесь», пока консультация не закончена.
6. Не задавай вопрос, если клиент явно попросил закончить разговор, отказался
   или попрощался.
7. Не дави на клиента и не отправляй сообщения без нового сообщения от него.

Правила достоверности:
- Используй только сведения из базы знаний ниже.
- Не выдумывай цены, расписание, свободные места, медицинские рекомендации,
  квалификацию сотрудников и условия договора.
- Данные со статусом conflict, incomplete, needs_confirmation или past не
  выдавай как подтверждённые. Кратко обозначь, что именно нужно уточнить.
- Не останавливай всю консультацию из-за одного неизвестного факта: продолжай
  выяснять потребность и предложи передачу конкретного вопроса сотруднику.
- На тестовом этапе не подтверждай запись и не принимай оплату.
- Отвечай по-русски, естественно и кратко, обычно 2–5 предложений.

БАЗА ЗНАНИЙ:
{KNOWLEDGE_BASE}
"""


def get_conversation_lock(conversation_id: str) -> threading.Lock:
    with conversation_state_lock:
        return conversation_locks.setdefault(conversation_id, threading.Lock())


def get_conversation_history(conversation_id: str) -> List[Dict[str, str]]:
    with conversation_state_lock:
        history = conversation_histories.get(conversation_id, deque())
        return list(history)


def save_conversation_turn(conversation_id: str, message: str, reply: str) -> None:
    with conversation_state_lock:
        if conversation_id not in conversation_histories:
            if len(conversation_histories) >= MAX_CONVERSATIONS:
                oldest_id = next(iter(conversation_histories))
                conversation_histories.pop(oldest_id, None)
                conversation_locks.pop(oldest_id, None)
            conversation_histories[conversation_id] = deque(
                maxlen=MAX_CONVERSATION_MESSAGES
            )
        history = conversation_histories[conversation_id]
        history.append({"role": "user", "content": message})
        history.append({"role": "assistant", "content": reply})


def reset_conversation(conversation_id: str) -> None:
    with conversation_state_lock:
        conversation_histories.pop(conversation_id, None)


def make_openai_reply(message: str, conversation_id: str = "local-default") -> str:
    conversation_lock = get_conversation_lock(conversation_id)
    with conversation_lock:
        input_messages = get_conversation_history(conversation_id)
        input_messages.append({"role": "user", "content": message})
        response = client.responses.create(
            model=OPENAI_MODEL,
            instructions=BOT_INSTRUCTIONS,
            input=input_messages,
            max_output_tokens=450,
            store=False,
        )
        reply = response.output_text.strip()
        if not reply:
            raise RuntimeError("OpenAI вернул пустой ответ")
        save_conversation_turn(conversation_id, message, reply)
        return reply


def remember_message(message_id: str) -> bool:
    """Return False when this Bitrix message was already accepted."""
    with processed_message_lock:
        if message_id in processed_message_ids:
            return False
        processed_message_ids.add(message_id)
        processed_message_order.append(message_id)
        if len(processed_message_order) > MAX_PROCESSED_MESSAGE_IDS:
            oldest = processed_message_order.popleft()
            processed_message_ids.discard(oldest)
        return True


def nested_get(payload: Dict[str, Any], *path: str) -> Any:
    value: Any = payload
    for part in path:
        if not isinstance(value, dict):
            return None
        value = value.get(part)
    return value


def first_form_value(payload: Dict[str, Any], key: str) -> Optional[str]:
    value = payload.get(key)
    if isinstance(value, list):
        return value[0] if value else None
    return value if isinstance(value, str) else None


def normalize_bitrix_event(
    payload: Dict[str, Any], is_json: bool
) -> Dict[str, str]:
    """Normalize Bitrix JSON or PHP-style form fields into one small object."""
    if is_json:
        return {
            "event": str(payload.get("event") or ""),
            "message_id": str(nested_get(payload, "data", "message", "id") or ""),
            "chat_id": str(
                nested_get(payload, "data", "message", "chatId")
                or nested_get(payload, "data", "chat", "id")
                or ""
            ),
            "entity_type": str(
                nested_get(payload, "data", "chat", "entityType") or ""
            ),
            "message": str(nested_get(payload, "data", "message", "text") or ""),
            "author_id": str(
                nested_get(payload, "data", "message", "authorId") or ""
            ),
            "bot_id": str(nested_get(payload, "data", "bot", "id") or ""),
            "application_token": str(
                nested_get(payload, "auth", "application_token") or ""
            ),
            "is_system": str(
                nested_get(payload, "data", "message", "isSystem") or "0"
            ),
        }

    return {
        "event": first_form_value(payload, "event") or "",
        "message_id": first_form_value(payload, "data[message][id]") or "",
        "chat_id": (
            first_form_value(payload, "data[message][chatId]")
            or first_form_value(payload, "data[chat][id]")
            or ""
        ),
        "entity_type": first_form_value(payload, "data[chat][entityType]") or "",
        "message": first_form_value(payload, "data[message][text]") or "",
        "author_id": first_form_value(payload, "data[message][authorId]") or "",
        "bot_id": first_form_value(payload, "data[bot][id]") or "",
        "application_token": first_form_value(
            payload, "auth[application_token]"
        )
        or "",
        "is_system": first_form_value(payload, "data[message][isSystem]") or "0",
    }


def validate_bitrix_event(event: Dict[str, str], local_test: bool) -> Tuple[bool, str]:
    if event["event"].upper() != "ONIMBOTV2MESSAGEADD":
        return False, "event_ignored"
    if event["entity_type"] != "LINES":
        return False, "not_open_line"
    if event["is_system"] in {"1", "true", "True"}:
        return False, "system_message_ignored"
    if not event["message_id"] or not event["chat_id"] or not event["message"].strip():
        return False, "required_event_data_missing"
    if event["author_id"] and event["bot_id"] == event["author_id"]:
        return False, "own_message_ignored"

    # Для короткого теста токен приложения может быть не задан. Если он указан,
    # проверка становится обязательной и события с другим токеном отклоняются.
    if not local_test and B24_APPLICATION_TOKEN:
        if event["application_token"] != B24_APPLICATION_TOKEN:
            return False, "invalid_application_token"

    return True, "accepted"


def send_bitrix_reply(chat_id: str, reply: str) -> None:
    if not all((B24_WEBHOOK_URL, B24_BOT_ID, B24_BOT_TOKEN)):
        raise RuntimeError("Bitrix24 credentials are not configured")

    url = f"{B24_WEBHOOK_URL}/imbot.v2.Chat.Message.send"
    request_body = json.dumps(
        {
            "botId": int(B24_BOT_ID),
            "botToken": B24_BOT_TOKEN,
            "dialogId": f"chat{chat_id}",
            "fields": {"message": reply},
        }
    ).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=request_body,
        headers={"Content-Type": "application/json", "Accept": "application/json"},
        method="POST",
    )

    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            result = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f"Bitrix24 HTTP error: {exc.code}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"Bitrix24 network error: {exc.reason}") from exc
    except json.JSONDecodeError as exc:
        raise RuntimeError("Bitrix24 returned invalid JSON") from exc

    if result.get("error"):
        error_code = str(result.get("error") or "unknown_error")
        error_description = str(result.get("error_description") or "")
        raise RuntimeError(
            f"Bitrix24 rejected the reply: {error_code} — {error_description}"
        )


def process_bitrix_message(event: Dict[str, str], deliver: bool = True) -> str:
    reply = make_openai_reply(
        event["message"].strip(), conversation_id=f"bitrix:{event['chat_id']}"
    )
    if deliver:
        send_bitrix_reply(event["chat_id"], reply)
    return reply


class RequestHandler(BaseHTTPRequestHandler):
    server_version = "EaglesBot/0.1"

    def send_json(self, status: int, payload: Dict[str, Any]) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        if self.path == "/health":
            self.send_json(200, {"status": "ok", "model": OPENAI_MODEL})
            return
        self.send_json(404, {"error": "not_found"})

    def do_POST(self) -> None:
        path = urllib.parse.urlsplit(self.path).path
        if path == "/bitrix/events":
            self.handle_bitrix_event()
            return
        if path not in {"/chat", "/chat/reset"}:
            self.send_json(404, {"error": "not_found"})
            return

        try:
            content_length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            self.send_json(400, {"error": "invalid_content_length"})
            return

        if content_length <= 0 or content_length > MAX_BODY_BYTES:
            self.send_json(413, {"error": "request_too_large_or_empty"})
            return

        try:
            payload = json.loads(self.rfile.read(content_length))
        except (json.JSONDecodeError, UnicodeDecodeError):
            self.send_json(400, {"error": "invalid_json"})
            return

        session_id = payload.get("session_id", "local-default")
        if not isinstance(session_id, str) or not session_id.strip():
            self.send_json(400, {"error": "session_id_invalid"})
            return
        session_id = session_id.strip()
        if len(session_id) > 128:
            self.send_json(400, {"error": "session_id_too_long"})
            return

        if path == "/chat/reset":
            reset_conversation(session_id)
            self.send_json(200, {"status": "reset", "session_id": session_id})
            return

        message = payload.get("message") if isinstance(payload, dict) else None
        if not isinstance(message, str) or not message.strip():
            self.send_json(400, {"error": "message_required"})
            return

        message = message.strip()
        if len(message) > MAX_MESSAGE_CHARS:
            self.send_json(400, {"error": "message_too_long"})
            return

        try:
            reply = make_openai_reply(message, conversation_id=f"local:{session_id}")
        except Exception as exc:
            print(f"OpenAI request failed: {type(exc).__name__}")
            self.send_json(502, {"error": "openai_request_failed"})
            return

        self.send_json(200, {"reply": reply, "session_id": session_id})

    def read_payload(self) -> Tuple[Optional[Dict[str, Any]], bool, Optional[str]]:
        try:
            content_length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            return None, False, "invalid_content_length"

        if content_length <= 0 or content_length > MAX_BODY_BYTES:
            return None, False, "request_too_large_or_empty"

        body = self.rfile.read(content_length)
        content_type = self.headers.get("Content-Type", "").split(";", 1)[0].strip()
        try:
            if content_type == "application/json":
                payload = json.loads(body.decode("utf-8"))
                return payload if isinstance(payload, dict) else None, True, None
            payload = urllib.parse.parse_qs(
                body.decode("utf-8"), keep_blank_values=True
            )
            return payload, False, None
        except (json.JSONDecodeError, UnicodeDecodeError):
            return None, False, "invalid_payload"

    def handle_bitrix_event(self) -> None:
        payload, is_json, error = self.read_payload()
        if error or payload is None:
            self.send_json(400, {"error": error or "invalid_payload"})
            return

        local_test = (
            self.client_address[0] in {"127.0.0.1", "::1"}
            and self.headers.get("X-Local-Test") == "1"
        )
        event = normalize_bitrix_event(payload, is_json)
        valid, reason = validate_bitrix_event(event, local_test)
        if not valid:
            status = 200 if reason.endswith("ignored") or reason == "not_open_line" else 403
            self.send_json(status, {"status": reason})
            return

        if not remember_message(event["message_id"]):
            self.send_json(200, {"status": "duplicate_ignored"})
            return

        if local_test:
            try:
                reply = process_bitrix_message(event, deliver=False)
            except Exception as exc:
                print(f"Local Bitrix event test failed: {type(exc).__name__}")
                self.send_json(502, {"error": "processing_failed"})
                return
            self.send_json(200, {"status": "simulated", "reply": reply})
            return

        threading.Thread(
            target=self.process_bitrix_in_background,
            args=(event,),
            daemon=True,
        ).start()
        self.send_json(202, {"status": "accepted"})

    @staticmethod
    def process_bitrix_in_background(event: Dict[str, str]) -> None:
        try:
            process_bitrix_message(event, deliver=True)
            print("Bitrix24 message processed")
        except Exception as exc:
            print(f"Bitrix24 processing failed: {type(exc).__name__}: {exc}")

    def log_message(self, format: str, *args: Any) -> None:
        # Не записываем тексты клиентов или секреты в лог.
        print(f"{self.command} {self.path} -> {args[1] if len(args) > 1 else '-'}")


if __name__ == "__main__":
    server = ThreadingHTTPServer(("127.0.0.1", PORT), RequestHandler)
    print(f"Eagles bot listening on http://127.0.0.1:{PORT}")
    print("Health: GET /health | OpenAI test: POST /chat | Bitrix: POST /bitrix/events")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
