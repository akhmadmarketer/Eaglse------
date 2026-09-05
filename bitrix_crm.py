"""Слой чтения и записи CRM Bitrix24 для бота Eagles.

Модуль не принимает решений. Он переводит логические поля модели в
пользовательские поля сделки, читает контекст сделки по чату Открытой линии и
выполняет только те записи, которые ему передали после проверки бизнес-правил.

Секреты в журнал не попадают: вебхук не выводится, значения клиентских полей
пишутся только в отладочном режиме вызывающего кода.
"""

import json
import logging
import threading
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple


logger = logging.getLogger("eagles.crm")

MOSCOW_TZ = timezone(timedelta(hours=3))
RULES_VERSION = "1.0"

# Стадии основной воронки. Логическое имя -> REST ID.
STAGE_IDS: Dict[str, str] = {
    "new": "NEW",
    "ai_consultation": "UC_JMBAX5",
    "deferred_interest": "UC_XI4M3M",
    "manager_required": "UC_2YCHH0",
    "manager_working": "UC_4OCEJT",
    "offer_preparation": "UC_KJBWTE",
    "offer_sent": "UC_N9VQVU",
    "agreed_processing": "UC_SXEQHN",
    "won": "WON",
    "lose": "LOSE",
}
STAGE_NAMES: Dict[str, str] = {value: key for key, value in STAGE_IDS.items()}

# Стадии, на которых боту разрешено отвечать и обновлять сделку.
BOT_STAGES = ("new", "ai_consultation", "deferred_interest")

# Единственные переходы, которые может запросить модель.
ALLOWED_TRANSITIONS: Dict[str, Tuple[str, ...]] = {
    "new": ("ai_consultation",),
    "ai_consultation": ("deferred_interest", "manager_required"),
    "deferred_interest": ("ai_consultation", "manager_required"),
}

# Логическое поле решения модели -> пользовательское поле сделки.
DEAL_FIELD_MAP: Dict[str, str] = {
    "request_type": "UF_CRM_1788531323",
    "sales_type": "UF_CRM_1788532000",
    "service_for": "UF_CRM_1788532461",
    "primary_service": "UF_CRM_1788430950688",
    "additional_services": "UF_CRM_1788533932",
    "preferred_format": "UF_CRM_1788534247",
    "age_group": "UF_CRM_EAGLES_AGE_GROUP",
    "participant_age": "UF_CRM_EAGLES_PARTICIPANT_AGE",
    "experience_level": "UF_CRM_EAGLES_EXPERIENCE_LEVEL",
    "primary_job": "UF_CRM_EAGLES_PRIMARY_JOB",
    "client_goal_text": "UF_CRM_EAGLES_CLIENT_GOAL_TEXT",
    "time_preference": "UF_CRM_EAGLES_TIME_PREFERENCE",
    "client_state": "UF_CRM_EAGLES_CLIENT_STATE",
    "state_reason": "UF_CRM_EAGLES_STATE_REASON",
    "main_barrier": "UF_CRM_EAGLES_MAIN_BARRIER",
    "barrier_text": "UF_CRM_EAGLES_BARRIER_TEXT",
    "barrier_status": "UF_CRM_EAGLES_BARRIER_STATUS",
    "qualification_status": "UF_CRM_EAGLES_QUALIFICATION_STATUS",
    "missing_data": "UF_CRM_EAGLES_MISSING_DATA",
    "next_action": "UF_CRM_EAGLES_NEXT_ACTION",
    "handoff_reason": "UF_CRM_EAGLES_HANDOFF_REASON",
    "manager_summary": "UF_CRM_EAGLES_MANAGER_SUMMARY",
    "followup_permission": "UF_CRM_EAGLES_FOLLOWUP_PERMISSION",
    "next_contact_at": "UF_CRM_EAGLES_NEXT_CONTACT_AT",
    "data_conflict": "UF_CRM_EAGLES_DATA_CONFLICT",
    "close_reason": "UF_CRM_EAGLES_CLOSE_REASON",
    "team_name": "UF_CRM_EAGLES_TEAM_NAME",
    "team_discipline": "UF_CRM_EAGLES_TEAM_DISCIPLINE",
    "team_start_date": "UF_CRM_EAGLES_TEAM_START_DATE",
    "team_end_date": "UF_CRM_EAGLES_TEAM_END_DATE",
    "athlete_count": "UF_CRM_EAGLES_ATHLETE_COUNT",
    "companion_count": "UF_CRM_EAGLES_COMPANION_COUNT",
    "team_age_composition": "UF_CRM_EAGLES_TEAM_AGE_COMPOSITION",
    "team_required_services": "UF_CRM_EAGLES_TEAM_REQUIRED_SERVICES",
    "accommodation_requirements": "UF_CRM_EAGLES_ACCOMMODATION_REQUIREMENTS",
    "catering_requirements": "UF_CRM_EAGLES_CATERING_REQUIREMENTS",
    "other_conditions": "UF_CRM_EAGLES_OTHER_CONDITIONS",
    "openline_chat_id": "UF_CRM_EAGLES_OPENLINE_CHAT_ID",
    "openline_session_id": "UF_CRM_EAGLES_OPENLINE_SESSION_ID",
    "ai_last_analyzed_at": "UF_CRM_EAGLES_AI_LAST_ANALYZED_AT",
    "ai_rules_version": "UF_CRM_EAGLES_AI_RULES_VERSION",
}
UF_TO_LOGICAL: Dict[str, str] = {
    value: key for key, value in DEAL_FIELD_MAP.items()
}

# Поле «Основная услуга» переиспользовано из старого поля «Вид спорта», поэтому
# у двух его значений остались случайные XML_ID. Сопоставляем их по REST ID.
ENUM_ID_OVERRIDES: Dict[str, Dict[str, str]] = {
    "UF_CRM_1788430950688": {"bjj": "45", "freestyle_wrestling": "47"},
}

# Нейтральное значение каждого списочного поля: им нельзя затирать известное.
NEUTRAL_VALUES: Dict[str, str] = {
    "request_type": "unknown",
    "sales_type": "unknown",
    "service_for": "unknown",
    "primary_service": "unknown",
    "preferred_format": "unknown",
    "age_group": "unknown",
    "experience_level": "unknown",
    "primary_job": "unknown",
    "client_state": "unknown_intent",
    "main_barrier": "unknown",
    "qualification_status": "not_started",
    "next_action": "unknown",
    "handoff_reason": "none",
    "followup_permission": "not_discussed",
}

MULTIPLE_FIELDS = ("additional_services", "team_required_services")


class BitrixError(RuntimeError):
    """Ошибка вызова REST Bitrix24 без раскрытия адреса вебхука."""


class BitrixNotFound(BitrixError):
    """Объект удалён или недоступен: Bitrix24 отвечает «Not found»."""


def now_iso() -> str:
    return datetime.now(MOSCOW_TZ).replace(microsecond=0).isoformat()


class BitrixClient:
    """Минимальный REST-клиент вебхука с кешем метаданных полей сделки."""

    def __init__(self, webhook_url: str, timeout: int = 20) -> None:
        self.webhook_url = webhook_url.strip().rstrip("/")
        self.timeout = timeout
        self._metadata: Optional[Dict[str, Dict[str, Any]]] = None
        self._metadata_lock = threading.Lock()

    @property
    def configured(self) -> bool:
        return bool(self.webhook_url)

    def call(self, method: str, payload: Optional[Dict[str, Any]] = None) -> Any:
        if not self.configured:
            raise BitrixError("B24_WEBHOOK_URL не настроен")

        body = json.dumps(payload or {}).encode("utf-8")
        request = urllib.request.Request(
            f"{self.webhook_url}/{method}",
            data=body,
            headers={
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                result = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            # Тело ошибки содержит причину и не содержит адреса вебхука.
            try:
                details = json.loads(exc.read().decode("utf-8"))
                description = str(details.get("error_description") or "")
                code = str(details.get("error") or "")
            except Exception:
                description, code = "", ""
            reason = " — ".join(part for part in (code, description) if part)
            if description.lower() == "not found":
                raise BitrixNotFound(f"{method}: не найден") from exc
            raise BitrixError(f"{method}: HTTP {exc.code} {reason}".strip()) from exc
        except urllib.error.URLError as exc:
            raise BitrixError(f"{method}: сеть — {exc.reason}") from exc
        except json.JSONDecodeError as exc:
            raise BitrixError(f"{method}: некорректный JSON") from exc

        if isinstance(result, dict) and result.get("error"):
            code = str(result.get("error") or "unknown_error")
            description = str(result.get("error_description") or "")
            raise BitrixError(f"{method}: {code} — {description}")
        return result.get("result") if isinstance(result, dict) else result

    # -- метаданные пользовательских полей ---------------------------------

    def metadata(self, refresh: bool = False) -> Dict[str, Dict[str, Any]]:
        with self._metadata_lock:
            if self._metadata is not None and not refresh:
                return self._metadata

            fields: Dict[str, Dict[str, Any]] = {}
            start = 0
            while True:
                page = self.call(
                    "crm.deal.userfield.list",
                    {
                        "filter": {"LANG": "ru"},
                        "order": {"ID": "ASC"},
                        "start": start,
                    },
                )
                if not isinstance(page, list):
                    raise BitrixError("crm.deal.userfield.list: неожиданный ответ")
                for item in page:
                    name = str(item.get("FIELD_NAME") or "")
                    if not name:
                        continue
                    enum_to_id: Dict[str, str] = {}
                    id_to_enum: Dict[str, str] = {}
                    for element in item.get("LIST") or []:
                        xml_id = str(element.get("XML_ID") or "")
                        element_id = str(element.get("ID") or "")
                        if xml_id and element_id:
                            enum_to_id[xml_id] = element_id
                            id_to_enum[element_id] = xml_id
                    for xml_id, element_id in ENUM_ID_OVERRIDES.get(name, {}).items():
                        enum_to_id.setdefault(xml_id, element_id)
                        id_to_enum[element_id] = xml_id
                    fields[name] = {
                        "type": str(item.get("USER_TYPE_ID") or ""),
                        "multiple": str(item.get("MULTIPLE") or "N") == "Y",
                        "enum_to_id": enum_to_id,
                        "id_to_enum": id_to_enum,
                    }
                if len(page) < 50:
                    break
                start += 50

            self._metadata = fields
            return fields

    def verify_field_map(self) -> List[str]:
        """Логические поля, для которых на портале нет пользовательского поля."""
        known = self.metadata()
        return sorted(
            logical
            for logical, uf_name in DEAL_FIELD_MAP.items()
            if uf_name not in known
        )

    # -- связь чата Открытой линии со сделкой ------------------------------

    def dialog_info(self, chat_id: str) -> Dict[str, Any]:
        """Связи чата Открытой линии: сделка, контакт и подключённые операторы.

        Bitrix24 хранит связь чата с CRM в entity_data_2 вида
        "LEAD|0|COMPANY|0|CONTACT|9|DEAL|13".
        """
        empty: Dict[str, Any] = {
            "read": False,
            "deal_id": None,
            "contact_id": None,
            "session_id": None,
            "source_id": "",
            "title": "",
            "operators": [],
        }
        try:
            dialog = self.call("imopenlines.dialog.get", {"CHAT_ID": int(chat_id)})
        except (BitrixError, ValueError) as exc:
            logger.warning("не удалось прочитать диалог chat=%s: %s", chat_id, exc)
            return empty
        if not isinstance(dialog, dict):
            return empty

        parts = str(dialog.get("entity_data_2") or "").split("|")
        pairs = dict(zip(parts[::2], parts[1::2]))

        def entity(name: str) -> Optional[str]:
            value = pairs.get(name, "0")
            return value if value.isdigit() and value != "0" else None

        # entity_data_1 вида "Y|DEAL|13|N|N|17|1788613560|0|0|0":
        # шестое поле — идентификатор текущей сессии, ноль означает её отсутствие.
        session_parts = str(dialog.get("entity_data_1") or "").split("|")
        session_id = session_parts[5] if len(session_parts) > 5 else "0"

        # entity_id вида "fbinstagramdirect|1|2161811058105214|33": коннектор и
        # номер линии дают источник сделки в том же виде, что ставит Bitrix24.
        entity_parts = str(dialog.get("entity_id") or "").split("|")
        source_id = ""
        if len(entity_parts) > 1 and entity_parts[0] and entity_parts[1]:
            source_id = f"{entity_parts[1]}|{entity_parts[0].upper()}"

        return {
            "read": True,
            "deal_id": entity("DEAL"),
            "contact_id": entity("CONTACT"),
            "session_id": session_id if session_id.isdigit() and session_id != "0" else None,
            "source_id": source_id,
            "title": str(dialog.get("name") or "").strip(),
            "operators": [str(item) for item in dialog.get("manager_list") or []],
        }

    def contact_deals(self, contact_id: str) -> Dict[str, Any]:
        """Сделки контакта одним запросом: открытая и признак покупки.

        Контакт — устойчивый якорь: он есть в связи чата, не закрывается и не
        удаляется в обычной работе. Сделка живёт от обращения до закрытия,
        поэтому вторая покупка того же клиента должна стать отдельной сделкой.

        Наличие карточки контакта не делает человека клиентом: Bitrix24 заводит
        контакт на каждое обращение из Instagram. Действующим клиентом считаем
        только того, у кого есть успешно закрытая сделка.
        """
        result: Dict[str, Any] = {"active_deal_id": None, "has_won_deal": False}
        try:
            found = self.call(
                "crm.deal.list",
                {
                    "filter": {"CONTACT_ID": int(contact_id), "CATEGORY_ID": 0},
                    "select": ["ID", "CLOSED", "STAGE_SEMANTIC_ID"],
                    "order": {"ID": "DESC"},
                },
            )
        except (BitrixError, ValueError) as exc:
            logger.warning("сделки контакта %s не прочитаны: %s", contact_id, exc)
            return result

        for item in found if isinstance(found, list) else []:
            if str(item.get("STAGE_SEMANTIC_ID") or "") == "S":
                result["has_won_deal"] = True
            if not result["active_deal_id"] and str(item.get("CLOSED") or "") == "N":
                result["active_deal_id"] = str(item.get("ID"))
        return result

    def deal_id_by_chat_field(self, chat_id: str) -> Optional[str]:
        try:
            found = self.call(
                "crm.deal.list",
                {
                    "filter": {DEAL_FIELD_MAP["openline_chat_id"]: str(chat_id)},
                    "select": ["ID"],
                    "order": {"ID": "DESC"},
                },
            )
        except BitrixError as exc:
            logger.warning("поиск сделки по chat=%s не удался: %s", chat_id, exc)
            return None
        if isinstance(found, list) and found:
            return str(found[0].get("ID"))
        return None

    # -- чтение и запись сделки --------------------------------------------

    def read_deal(self, deal_id: str) -> Dict[str, Any]:
        deal = self.call("crm.deal.get", {"id": int(deal_id)})
        if not isinstance(deal, dict):
            raise BitrixError("crm.deal.get: неожиданный ответ")
        return deal

    def create_deal(self, chat_id: str, info: Dict[str, Any]) -> str:
        """Создать сделку для чата, у которого её нет.

        Обычно сделку заводит сам Bitrix24 при первом обращении. Этот путь нужен,
        когда связи не осталось: например, прежнюю сделку удалили, а новую
        Bitrix24 не создаёт, потому что обращение для него не первое.
        """
        fields: Dict[str, Any] = {
            "TITLE": info.get("title") or f"Открытая линия — чат {chat_id}",
            "CATEGORY_ID": 0,
            "STAGE_ID": STAGE_IDS["new"],
            "OPENED": "Y",
            DEAL_FIELD_MAP["openline_chat_id"]: str(chat_id),
        }
        if info.get("contact_id"):
            fields["CONTACT_ID"] = int(info["contact_id"])
        if info.get("source_id"):
            fields["SOURCE_ID"] = info["source_id"]
        if info.get("session_id"):
            fields[DEAL_FIELD_MAP["openline_session_id"]] = str(info["session_id"])

        deal_id = self.call("crm.deal.add", {"fields": fields, "params": {"REGISTER_SONET_EVENT": "N"}})
        return str(deal_id)

    def update_deal(self, deal_id: str, fields: Dict[str, Any]) -> None:
        if not fields:
            return
        self.call(
            "crm.deal.update",
            {"id": int(deal_id), "fields": fields, "params": {"REGISTER_SONET_EVENT": "N"}},
        )

    # -- перевод значений --------------------------------------------------

    def to_logical(self, deal: Dict[str, Any]) -> Dict[str, Any]:
        """Текущие значения проектных полей сделки в терминах модели."""
        known = self.metadata()
        values: Dict[str, Any] = {}
        for logical, uf_name in DEAL_FIELD_MAP.items():
            raw = deal.get(uf_name)
            info = known.get(uf_name)
            # Незаполненное множественное поле приходит как False, а не как
            # пустой список; ноль в числовом поле при этом значением остаётся.
            if raw is None or raw is False or (isinstance(raw, (str, list)) and not raw):
                values[logical] = [] if logical in MULTIPLE_FIELDS else None
                continue
            if info and info["type"] == "enumeration":
                if isinstance(raw, list):
                    values[logical] = [
                        info["id_to_enum"].get(str(item), str(item)) for item in raw
                    ]
                else:
                    values[logical] = info["id_to_enum"].get(str(raw), str(raw))
            elif isinstance(raw, list):
                values[logical] = [str(item) for item in raw]
            else:
                values[logical] = raw
        return values

    def to_rest(self, logical: str, value: Any) -> Tuple[bool, Any, str]:
        """Значение модели в форме Bitrix24.

        Возвращает (принято, значение, причина отклонения).
        """
        uf_name = DEAL_FIELD_MAP.get(logical)
        if not uf_name:
            return False, None, "field_not_allowed"
        info = self.metadata().get(uf_name)
        if not info:
            return False, None, "field_missing_on_portal"

        if info["type"] == "enumeration":
            items = value if isinstance(value, list) else [value]
            resolved: List[str] = []
            for item in items:
                enum_id = info["enum_to_id"].get(str(item))
                if not enum_id:
                    return False, None, f"unknown_enum_value:{item}"
                resolved.append(enum_id)
            if info["multiple"]:
                return True, resolved, ""
            return True, resolved[0], ""

        if info["type"] == "integer":
            try:
                return True, int(value), ""
            except (TypeError, ValueError):
                return False, None, "not_an_integer"

        if info["type"] in {"date", "datetime"}:
            text = str(value).strip()
            if not text:
                return False, None, "empty_date"
            return True, text, ""

        text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
        return True, text.strip(), ""
