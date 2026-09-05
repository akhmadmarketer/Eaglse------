import json
import logging
import logging.handlers
import os
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Deque, Dict, List, Optional, Tuple

from openai import OpenAI

import bitrix_crm
import sales_decision


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

LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
LOG_DIR = ROOT / "logs"
LOG_FILE = LOG_DIR / "app.log"

# По умолчанию тексты клиентов в журнал не попадают: остаются только длина
# сообщения и технические идентификаторы. Для разбора конкретного диалога
# запустите обработчик с LOG_MESSAGE_TEXT=1 и отключите обратно после разбора.
LOG_MESSAGE_TEXT = os.getenv("LOG_MESSAGE_TEXT", "0").strip().lower() in {"1", "true", "yes"}


def setup_logging() -> logging.Logger:
    """Журнал в файл и в терминал. Секреты и тексты клиентов не пишутся."""
    LOG_DIR.mkdir(exist_ok=True)
    log = logging.getLogger("eagles")
    log.setLevel(getattr(logging, LOG_LEVEL, logging.INFO))
    log.propagate = False
    if log.handlers:
        return log

    formatter = logging.Formatter(
        fmt="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    file_handler = logging.handlers.RotatingFileHandler(
        LOG_FILE, maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8"
    )
    file_handler.setFormatter(formatter)
    stream_handler = logging.StreamHandler()
    stream_handler.setFormatter(formatter)
    log.addHandler(file_handler)
    log.addHandler(stream_handler)
    return log


logger = setup_logging()


def describe_text(text: str) -> str:
    """Что можно записать о сообщении клиента, не раскрывая переписку."""
    if LOG_MESSAGE_TEXT:
        return f"chars={len(text)} text={text!r}"
    return f"chars={len(text)}"

B24_WEBHOOK_URL = os.getenv("B24_WEBHOOK_URL", "").strip().rstrip("/")
B24_BOT_ID = os.getenv("B24_BOT_ID", "").strip()
B24_BOT_TOKEN = os.getenv("B24_BOT_TOKEN", "").strip()
B24_APPLICATION_TOKEN = os.getenv("B24_APPLICATION_TOKEN", "").strip()

# Режим работы с CRM:
#   off   — сделка не читается и не пишется, бот только отвечает;
#   plan  — сделка читается, план записи попадает только в журнал;
#   apply — план записи выполняется в Bitrix24.
# По умолчанию plan: запись в рабочую воронку владелец включает осознанно.
CRM_MODE = os.getenv("EAGLES_CRM_MODE", "plan").strip().lower()
if CRM_MODE not in {"off", "plan", "apply"}:
    raise RuntimeError("EAGLES_CRM_MODE должен быть off, plan или apply")

crm = bitrix_crm.BitrixClient(B24_WEBHOOK_URL)

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


# В промпт попадают только каталоги с фактами о клубе. Служебные файлы
# `knowledge/README.md` и `knowledge/review-needed.md` — внутренние заметки о
# неподтверждённых и противоречивых данных, модель не должна их пересказывать
# клиенту.
#
# `knowledge/mock` — вымышленные цены, расписание и условия для отладки. В
# реальном снимке сайта нет ни одного факта со статусом confirmed, поэтому без
# подмены бот не может вести предметный разговор. Включать только на стенде:
# клиенту эти значения выдавать нельзя.
KNOWLEDGE_MOCK = os.getenv("EAGLES_KNOWLEDGE_MOCK", "0").strip().lower() in {
    "1",
    "true",
    "yes",
}
KNOWLEDGE_DIRS = ("static", "mock") if KNOWLEDGE_MOCK else ("static", "dynamic")


def load_knowledge_base() -> str:
    knowledge_dir = ROOT / "knowledge"
    if not knowledge_dir.is_dir():
        return "База знаний ещё не подключена."

    chunks = []
    for directory in KNOWLEDGE_DIRS:
        source = knowledge_dir / directory
        if not source.is_dir():
            continue
        for path in sorted(source.rglob("*")):
            if path.suffix not in {".md", ".json"} or not path.is_file():
                continue
            relative = path.relative_to(ROOT)
            chunks.append(f"\n--- {relative} ---\n{path.read_text(encoding='utf-8')}")
    if not chunks:
        return "База знаний ещё не подключена."
    return "".join(chunks)


KNOWLEDGE_BASE = load_knowledge_base()

if KNOWLEDGE_MOCK:
    logger.warning(
        "ВНИМАНИЕ: включены вымышленные данные knowledge/mock. Цены, расписание "
        "и условия в ответах бота недостоверны. Не оставляйте этот режим "
        "включённым на канале, где пишут настоящие клиенты."
    )

SALES_INSTRUCTIONS = sales_decision.build_instructions(KNOWLEDGE_BASE)


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


def build_recent_messages(conversation_id: str) -> List[Dict[str, str]]:
    """История беседы в терминах блока RECENT_MESSAGES."""
    return [
        {
            "role": "client" if item["role"] == "user" else "bot",
            "text": item["content"],
        }
        for item in get_conversation_history(conversation_id)
    ]


def make_decision(
    message: str,
    conversation_id: str,
    *,
    crm_context: Dict[str, Any],
    contact_context: Dict[str, Any],
    message_id: str = "",
) -> Dict[str, Any]:
    """Одно структурированное решение модели: ответ клиенту и план для CRM."""
    user_input = sales_decision.build_model_input(
        current_time=bitrix_crm.now_iso(),
        crm_context=crm_context,
        contact_context=contact_context,
        recent_messages=build_recent_messages(conversation_id),
        current_message={"message_id": message_id, "text": message},
    )
    logger.info(
        "openai request conversation=%s deal=%s stage=%s history=%d %s",
        conversation_id,
        crm_context.get("deal_id") or "-",
        crm_context.get("stage") or "-",
        len(get_conversation_history(conversation_id)),
        describe_text(message),
    )

    started = time.monotonic()
    try:
        decision = sales_decision.request_decision(
            client,
            model=OPENAI_MODEL,
            instructions=SALES_INSTRUCTIONS,
            user_input=user_input,
        )
    except Exception as exc:
        logger.error(
            "openai failed conversation=%s after=%.1fs %s: %s",
            conversation_id,
            time.monotonic() - started,
            type(exc).__name__,
            exc,
        )
        raise

    analysis = decision.get("crm_analysis", {})
    logger.info(
        "openai decision conversation=%s after=%.1fs action=%s state=%s barrier=%s "
        "next=%s stage=%s handoff=%s/%s safety=%s facts=%d conflicts=%d",
        conversation_id,
        time.monotonic() - started,
        decision.get("action"),
        analysis.get("client_state"),
        analysis.get("main_barrier"),
        analysis.get("next_action"),
        decision.get("stage_transition", {}).get("target"),
        bool(decision.get("handoff", {}).get("required")),
        decision.get("handoff", {}).get("reason"),
        decision.get("safety", {}).get("category"),
        len(decision.get("extracted_facts") or []),
        len(decision.get("conflicts") or []),
    )

    problems = sales_decision.check_semantics(decision)
    if problems:
        logger.warning(
            "decision inconsistent conversation=%s problems=%s",
            conversation_id,
            ",".join(problems),
        )
    return decision


EMPTY_CONTACT_CONTEXT: Dict[str, Any] = {
    "name": "",
    "current_channel_available": True,
    "has_phone": False,
    "has_email": False,
    "is_existing_client": False,
}

# Технические поля интеграции модели не нужны.
TECHNICAL_FIELDS = (
    "openline_chat_id",
    "openline_session_id",
    "ai_last_analyzed_at",
    "ai_rules_version",
)


def read_contact_context(contact_id: str) -> Dict[str, Any]:
    """Минимальные сведения о контакте: имя и наличие каналов связи."""
    try:
        record = crm.call("crm.contact.get", {"id": int(contact_id)})
    except (bitrix_crm.BitrixError, ValueError) as exc:
        logger.warning("контакт %s не прочитан: %s", contact_id, exc)
        return {}
    if not isinstance(record, dict):
        return {}
    return {
        "name": str(record.get("NAME") or "").strip(),
        "has_phone": str(record.get("HAS_PHONE") or "N") == "Y",
        "has_email": str(record.get("HAS_EMAIL") or "N") == "Y",
    }


def read_crm_context(
    chat_id: str,
) -> Tuple[Optional[str], Dict[str, Any], Dict[str, Any], Dict[str, Any], Dict[str, Any]]:
    """Сделка чата: (deal_id, CRM_CONTEXT, CONTACT_CONTEXT, значения, связи чата).

    Якорь — контакт, а не сделка. Связь чата с CRM в Bitrix24 одноразовая: её
    нельзя переписать через REST, она указывает на первую созданную сделку и
    остаётся указывать на неё даже после удаления. Поэтому сделку ищем по
    контакту среди открытых, а связь чата используем только как запасной путь.
    """
    context: Dict[str, Any] = {
        "deal_id": None,
        "stage": "not_linked",
        "manager_has_joined": False,
        "bot_may_reply": True,
        "last_modified_at": None,
        "session_id": None,
        "fields": {},
    }
    contact = dict(EMPTY_CONTACT_CONTEXT)

    if CRM_MODE == "off" or not crm.configured:
        context["stage"] = "crm_disabled"
        return None, context, contact, {}, {}

    info = crm.dialog_info(chat_id)
    if not info["read"]:
        # Актуальное состояние диалога неизвестно: вмешиваться нельзя.
        context["stage"] = "unreadable"
        context["bot_may_reply"] = False
        return None, context, contact, {}, info

    context["manager_has_joined"] = any(
        operator != B24_BOT_ID for operator in info["operators"]
    )
    context["session_id"] = info["session_id"]

    contact_id = info["contact_id"]
    deal_id = None
    if contact_id:
        contact.update(read_contact_context(contact_id))
        # Карточка контакта есть у каждого написавшего, поэтому действующим
        # клиентом считаем только того, у кого есть успешно закрытая сделка.
        deals = crm.contact_deals(contact_id)
        contact["is_existing_client"] = deals["has_won_deal"]
        deal_id = deals["active_deal_id"]
    if not deal_id:
        deal_id = crm.deal_id_by_chat_field(chat_id)

    deal = None
    if deal_id:
        try:
            deal = crm.read_deal(deal_id)
        except bitrix_crm.BitrixNotFound:
            logger.warning("сделка %s чата %s больше не существует", deal_id, chat_id)
            deal_id = None
        except bitrix_crm.BitrixError as exc:
            logger.error("сделка %s не прочитана: %s", deal_id, exc)
            context["stage"] = "unreadable"
            context["bot_may_reply"] = False
            return None, context, contact, {}, info

    if deal is None:
        # Открытой сделки нет. Заводить её сейчас нельзя: сначала нужно понять,
        # продажное ли это обращение. Решение принимается после ответа модели.
        context["bot_may_reply"] = not context["manager_has_joined"]
        return None, context, contact, {}, info

    values = crm.to_logical(deal)
    stage_id = str(deal.get("STAGE_ID") or "")
    stage = bitrix_crm.STAGE_NAMES.get(stage_id, stage_id or "unknown")
    context.update(
        deal_id=deal_id,
        stage=stage,
        last_modified_at=deal.get("DATE_MODIFY"),
        bot_may_reply=(
            stage in bitrix_crm.BOT_STAGES and not context["manager_has_joined"]
        ),
        fields={
            name: value
            for name, value in values.items()
            if value not in (None, "", []) and name not in TECHNICAL_FIELDS
        },
    )
    return deal_id, context, contact, values, info


# Обращения, ради которых заводится продажная сделка. Жалоба, сервисный вопрос
# действующего клиента, деловое предложение и нецелевое обращение продажами не
# являются и воронку засорять не должны.
SALES_REQUEST_TYPES = ("new_sale", "upsell")


def create_deal_if_sales_request(
    chat_id: str, decision: Dict[str, Any], info: Dict[str, Any]
) -> Tuple[Optional[str], Dict[str, Any]]:
    """Завести сделку, если открытой нет, а обращение оказалось продажным."""
    request_type = str(decision.get("crm_analysis", {}).get("request_type") or "")
    if request_type not in SALES_REQUEST_TYPES:
        logger.info(
            "сделка не создаётся chat=%s request_type=%s — обращение не продажное",
            chat_id,
            request_type or "-",
        )
        return None, {}

    if CRM_MODE != "apply":
        logger.info(
            "crm plan chat=%s mode=%s создал бы сделку request_type=%s contact=%s",
            chat_id,
            CRM_MODE,
            request_type,
            info.get("contact_id") or "-",
        )
        return None, {}

    try:
        deal_id = crm.create_deal(chat_id, info)
        deal = crm.read_deal(deal_id)
    except bitrix_crm.BitrixError as exc:
        logger.error("сделка для чата %s не создана: %s", chat_id, exc)
        return None, {}

    logger.info(
        "создана сделка %s chat=%s contact=%s request_type=%s",
        deal_id,
        chat_id,
        info.get("contact_id") or "-",
        request_type,
    )
    return deal_id, crm.to_logical(deal)


def recheck_deal(deal_id: str, chat_id: str) -> Tuple[str, bool]:
    """Стадия и признак подключения менеджера непосредственно перед записью."""
    deal = crm.read_deal(deal_id)
    stage_id = str(deal.get("STAGE_ID") or "")
    info = crm.dialog_info(chat_id)
    manager_joined = bool(info["read"]) and any(
        operator != B24_BOT_ID for operator in info["operators"]
    )
    return bitrix_crm.STAGE_NAMES.get(stage_id, stage_id or "unknown"), manager_joined


def apply_decision(
    deal_id: str,
    chat_id: str,
    decision: Dict[str, Any],
    current_values: Dict[str, Any],
    current_stage: str,
    session_id: Optional[str] = None,
) -> None:
    """Записать разрешённые поля и разрешённый переход стадии одним вызовом."""
    fields, applied, rejected = sales_decision.plan_updates(
        decision, current_values, crm
    )
    stage_target, stage_rejection = sales_decision.resolve_stage(
        current_stage, decision
    )
    if stage_rejection:
        logger.warning("stage rejected deal=%s %s", deal_id, stage_rejection)
    if rejected:
        logger.warning("fields rejected deal=%s %s", deal_id, "; ".join(rejected))

    technical = {
        "openline_chat_id": str(chat_id),
        "ai_last_analyzed_at": bitrix_crm.now_iso(),
        "ai_rules_version": bitrix_crm.RULES_VERSION,
    }
    if session_id:
        technical["openline_session_id"] = str(session_id)
    for name, value in technical.items():
        if current_values.get(name) == value:
            continue
        accepted, rest_value, _ = crm.to_rest(name, value)
        if accepted:
            fields[bitrix_crm.DEAL_FIELD_MAP[name]] = rest_value

    if stage_target:
        fields["STAGE_ID"] = bitrix_crm.STAGE_IDS[stage_target]

    logger.info(
        "crm plan deal=%s mode=%s stage=%s->%s fields=%s",
        deal_id,
        CRM_MODE,
        current_stage,
        stage_target or current_stage,
        ",".join(sorted(applied)) or "-",
    )
    if CRM_MODE == "apply" and fields:
        crm.update_deal(deal_id, fields)
        logger.info("crm applied deal=%s fields=%d", deal_id, len(fields))


def make_local_reply(message: str, conversation_id: str) -> str:
    """Локальный чат без CRM: тот же промпт и та же схема решения."""
    with get_conversation_lock(conversation_id):
        decision = make_decision(
            message,
            conversation_id,
            crm_context={
                "deal_id": None,
                "stage": "local_test",
                "manager_has_joined": False,
                "bot_may_reply": True,
                "fields": {},
            },
            contact_context=dict(EMPTY_CONTACT_CONTEXT),
        )
        reply = str(decision.get("reply", {}).get("text") or "").strip()
        if reply:
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

    logger.info("bitrix send reply chat=%s chars=%d", chat_id, len(reply))
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
        logger.error(
            "bitrix rejected reply chat=%s error=%s %s",
            chat_id,
            error_code,
            error_description,
        )
        raise RuntimeError(
            f"Bitrix24 rejected the reply: {error_code} — {error_description}"
        )


def process_bitrix_message(
    event: Dict[str, str], deliver: bool = True
) -> Optional[str]:
    """Полный цикл одного сообщения: контекст сделки, решение, запись, ответ."""
    chat_id = event["chat_id"]
    message = event["message"].strip()
    conversation_id = f"bitrix:{chat_id}"

    with get_conversation_lock(conversation_id):
        deal_id, crm_context, contact_context, current_values, chat_info = read_crm_context(
            chat_id
        )
        if not crm_context["bot_may_reply"]:
            logger.info(
                "бот молчит chat=%s deal=%s stage=%s manager=%s",
                chat_id,
                deal_id or "-",
                crm_context["stage"],
                crm_context["manager_has_joined"],
            )
            return None

        decision = make_decision(
            message,
            conversation_id,
            crm_context=crm_context,
            contact_context=contact_context,
            message_id=event["message_id"],
        )

        reply = str(decision.get("reply", {}).get("text") or "").strip()
        should_send = bool(decision.get("reply", {}).get("should_send")) and bool(reply)
        handoff = sales_decision.needs_handoff(decision)

        # Открытой сделки не было: заводим её только теперь, когда модель
        # определила, что обращение продажное.
        if deal_id is None and CRM_MODE != "off" and chat_info.get("read"):
            deal_id, current_values = create_deal_if_sales_request(
                chat_id, decision, chat_info
            )

        crm_written = True
        if deal_id and CRM_MODE != "off":
            try:
                fresh_stage, manager_joined = recheck_deal(deal_id, chat_id)
            except bitrix_crm.BitrixError as exc:
                logger.error("повторное чтение сделки %s не удалось: %s", deal_id, exc)
                crm_written = False
            else:
                if manager_joined or fresh_stage not in bitrix_crm.BOT_STAGES:
                    logger.warning(
                        "менеджер перехватил диалог chat=%s deal=%s stage=%s",
                        chat_id,
                        deal_id,
                        fresh_stage,
                    )
                    return None
                try:
                    apply_decision(
                        deal_id,
                        chat_id,
                        decision,
                        current_values,
                        fresh_stage,
                        crm_context.get("session_id"),
                    )
                except bitrix_crm.BitrixError as exc:
                    crm_written = False
                    logger.error("запись в сделку %s не удалась: %s", deal_id, exc)

        # При передаче менеджеру клиент узнаёт об этом только после успешной
        # записи в CRM: иначе бот пообещал бы то, чего в сделке нет.
        if handoff and not crm_written:
            logger.error(
                "ответ о передаче не отправлен chat=%s deal=%s", chat_id, deal_id or "-"
            )
            return None

        if not should_send:
            logger.info(
                "модель не отправляет ответ chat=%s action=%s",
                chat_id,
                decision.get("action"),
            )
            return None

        if deliver:
            send_bitrix_reply(chat_id, reply)
        save_conversation_turn(conversation_id, message, reply)
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
            reply = make_local_reply(message, f"local:{session_id}")
        except Exception as exc:
            logger.error("local chat request failed: %s: %s", type(exc).__name__, exc)
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
        logger.info(
            "bitrix event=%s chat=%s message=%s author=%s bot=%s entity=%s local_test=%s %s",
            event["event"] or "-",
            event["chat_id"] or "-",
            event["message_id"] or "-",
            event["author_id"] or "-",
            event["bot_id"] or "-",
            event["entity_type"] or "-",
            local_test,
            describe_text(event["message"]),
        )
        valid, reason = validate_bitrix_event(event, local_test)
        if not valid:
            status = 200 if reason.endswith("ignored") or reason == "not_open_line" else 403
            logger.warning(
                "bitrix event rejected reason=%s chat=%s message=%s",
                reason,
                event["chat_id"] or "-",
                event["message_id"] or "-",
            )
            self.send_json(status, {"status": reason})
            return

        if not remember_message(event["message_id"]):
            logger.warning(
                "bitrix duplicate ignored chat=%s message=%s",
                event["chat_id"] or "-",
                event["message_id"] or "-",
            )
            self.send_json(200, {"status": "duplicate_ignored"})
            return

        if local_test:
            try:
                reply = process_bitrix_message(event, deliver=False)
            except Exception as exc:
                logger.error(
                    "local bitrix test failed chat=%s %s: %s",
                    event["chat_id"] or "-",
                    type(exc).__name__,
                    exc,
                )
                self.send_json(502, {"error": "processing_failed"})
                return
            if reply is None:
                self.send_json(200, {"status": "no_reply"})
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
            logger.info(
                "bitrix message processed chat=%s message=%s",
                event["chat_id"] or "-",
                event["message_id"] or "-",
            )
        except Exception:
            logger.exception(
                "bitrix processing failed chat=%s message=%s",
                event["chat_id"] or "-",
                event["message_id"] or "-",
            )

    def log_message(self, format: str, *args: Any) -> None:
        # Не записываем тексты клиентов или секреты в лог.
        logger.info(
            "http %s %s -> %s",
            self.command,
            self.path,
            args[1] if len(args) > 1 else "-",
        )


if __name__ == "__main__":
    server = ThreadingHTTPServer(("127.0.0.1", PORT), RequestHandler)
    logger.info(
        "eagles bot started port=%s model=%s crm_mode=%s knowledge=%s "
        "knowledge_chars=%d log_message_text=%s",
        PORT,
        OPENAI_MODEL,
        CRM_MODE,
        "mock" if KNOWLEDGE_MOCK else "real",
        len(KNOWLEDGE_BASE),
        LOG_MESSAGE_TEXT,
    )
    logger.info("журнал: %s", LOG_FILE)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
