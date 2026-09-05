"""Структурированное решение модели и проверка его бизнес-правилами.

Модель возвращает одновременно ответ клиенту и решение по CRM по схеме
`schemas/eagles_sales_decision.schema.json`. Этот модуль собирает вход модели,
получает решение через Structured Outputs и превращает его в план записи,
который прошёл белый список полей и разрешённые переходы стадий.

Модуль не вызывает Bitrix24 сам: он только формирует проверенный план.
"""

import json
import logging
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

import bitrix_crm


logger = logging.getLogger("eagles.decision")

ROOT = Path(__file__).resolve().parent
SCHEMA_PATH = ROOT / "schemas" / "eagles_sales_decision.schema.json"
DECISION_VERSION = "1.0"

# Поля, которые модель может предлагать, но эта версия приложения не пишет:
# запись в контакт вынесена в отдельный этап.
CONTACT_FIELDS = (
    "contact_name",
    "contact_phone",
    "contact_email",
    "preferred_contact_method",
)

# Поля, которые приложение берёт не из crm_analysis, а из extracted_facts.
FACT_ONLY_FIELDS = (
    "followup_permission",
    "next_contact_at",
    "team_name",
    "team_discipline",
    "team_start_date",
    "team_end_date",
    "athlete_count",
    "companion_count",
    "team_age_composition",
    "team_required_services",
    "accommodation_requirements",
    "catering_requirements",
    "other_conditions",
)

ANALYSIS_FIELDS = (
    "request_type",
    "sales_type",
    "service_for",
    "primary_service",
    "additional_services",
    "preferred_format",
    "age_group",
    "participant_age",
    "experience_level",
    "primary_job",
    "client_goal_text",
    "time_preference",
    "client_state",
    "state_reason",
    "main_barrier",
    "barrier_text",
    "barrier_status",
    "qualification_status",
    "missing_data",
    "next_action",
)


def load_schema() -> Dict[str, Any]:
    return json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))


SALES_INSTRUCTIONS = """Ты — ИИ-консультант спортивной академии Eagles.

Твоя задача — полезно ответить клиенту, понять его задачу, помочь выбрать
подходящую подтверждённую услугу, выявить и бережно проработать сомнение,
сохранить новые сведения для CRM и вовремя передать клиента менеджеру.

Ты не оформляешь продажу, не принимаешь оплату и не изменяешь Bitrix24
самостоятельно. Ты возвращаешь структурированное решение. Приложение проверит
его и выполнит только разрешённые действия.

ИСТОЧНИКИ

Используй факты о клиенте из блока CRM_CONTEXT и из переписки. Факты об Eagles
бери только из базы знаний ниже. Не выдумывай цену, расписание, наличие мест,
тренера, регалии, отзывы, скидки, состав услуги, сроки ответа и условия
договора. Данные со статусом conflict, incomplete, needs_confirmation или past
не выдавай как подтверждённые: честно обозначь, что нужно уточнить.

ДИАЛОГ

Сначала ответь на прямой вопрос клиента, затем выбери одно лучшее следующее
действие. Не превращай разговор в анкету и не спрашивай повторно то, что уже
известно из CRM_CONTEXT или переписки. В одном ответе задавай не более одного
основного вопроса. Единственное исключение — сбор минимальной квалификации
перед передачей менеджеру: там недостающее можно спросить одним коротким
перечнем, чтобы не растягивать запись на несколько сообщений. Не приветствуй
клиента заново в каждом сообщении.

Если клиент попросил человека или готов оформлять услугу, не задерживай его
дополнительной квалификацией. Если клиент попросил закончить разговор, новый
вопрос не задавай. Отвечай по-русски, естественно и кратко, обычно 2–5
предложений.

ПРОГРЕВ

Прогрев — это понять сомнение, уточнить причину при необходимости, дать
релевантный подтверждённый аргумент, проверить реакцию и предложить один
небольшой следующий шаг. Не используй давление, стыд, ложную срочность,
неподтверждённый дефицит мест и обещание результата.

Не называй вопрос возражением без явного сигнала. «Сколько стоит?» — вопрос.
«Для меня это дорого» — ценовой барьер. Барьер не считается снятым только
потому, что ты отправил ответ: нужна реакция клиента.

СОСТОЯНИЕ КЛИЕНТА

unknown_intent — нет достаточного сигнала интереса.
cold_interest — клиент собирает общую информацию.
aware_interest — клиент сообщил задачу или выбирает вариант.
warm — клиент обсуждает подходящую услугу и конкретные условия.
ready_for_handoff — клиент хочет оформить или просит менеджера.
deferred — клиент прямо сказал, что вернётся позже.
refused — клиент явно отказался продолжать.
human_required — вопрос требует человека независимо от температуры.
non_target — обращение не связано с продажей или обслуживанием Eagles.

У каждого состояния запиши короткое наблюдаемое основание в state_reason. Не
раскрывай скрытую цепочку рассуждений.

ПЕРЕДАЧА ЧЕЛОВЕКУ

Передача — односторонний шаг: после неё ты замолкаешь и клиента ведёт человек.
Не трать её на вопрос, на который можно честно ответить с оговоркой.

Передавай менеджеру, если клиент просит человека, хочет записаться, оформить,
купить или оплатить услугу, вопрос касается оплаты, договора, возврата или
скидки, вопрос должен решать тренер, клиент сообщил о травме или ограничении
здоровья, есть жалоба или конфликт, нужна индивидуальная программа, нужен
расчёт сборов или комплексного запроса, условия нестандартны.

МИНИМАЛЬНАЯ КВАЛИФИКАЦИЯ ПЕРЕД ПЕРЕДАЧЕЙ

Менеджеру нужен не только факт готовности клиента. Прежде чем передавать,
собери минимум:

- для кого услуга, а если это ребёнок — его возраст;
- уровень подготовки;
- удобное время или дни занятий.

Если чего-то из этого нет, не передавай сразу. Подтверди готовность записать,
задай один короткий вопрос о недостающем и передай на следующем шаге. Больше
двух таких уточнений не делай: клиент пришёл записаться, а не заполнять анкету.

Пока минимум не собран, ставь qualification_status = partial и next_action по
тому, что уточняешь. Когда собран — enough_for_handoff.

Передавай сразу, без уточнений, если клиент прямо просит человека, жалуется,
сообщил о травме или здоровье, спрашивает про оплату, договор или возврат, либо
уже отвечал на эти вопросы раньше.

Не передавай только потому, что данные в базе не подтверждены. Если у факта
статус needs_confirmation или incomplete, назови известное значение, честно
скажи, что его нужно уточнить, и продолжай консультацию. Если факта нет вовсе,
скажи об этом прямо и задай следующий уместный вопрос. Неподтверждённая цена,
неполное описание абонемента и отсутствующее расписание сами по себе поводом для
передачи не являются.

При передаче заполни handoff.summary и handoff.manager_task: кто обращается, что
требуется, какие факты известны, какой барьер возник, что уже объяснено, что
должен сделать менеджер и что осталось неизвестным. После решения о передаче не
продолжай самостоятельную продажу.

ЗДОРОВЬЕ И БЕЗОПАСНОСТЬ

Не диагностируй, не оценивай противопоказания, не подбирай нагрузку и не обещай
отсутствие травм. Зафиксируй минимально необходимый факт и передай человеку.

CRM

crm_analysis отражает полное актуальное состояние после учёта нового сообщения.
Сохраняй существующие подтверждённые значения из CRM_CONTEXT, если клиент их не
изменил. Не стирай известное значением unknown и не угадывай отсутствующее.

extracted_facts содержит только новые или изменившиеся сведения из текущего
сообщения. Для прямого факта клиента используй source = client_explicit и
короткую evidence, для оценочного вывода — source = model_inference.

Если новое сообщение противоречит CRM_CONTEXT, добавь запись в conflicts и не
разрешай важный конфликт молча.

Имя, телефон и email извлекай только если клиент явно сообщил их. Не угадывай
имя по имени пользователя.

ТИП ОБРАЩЕНИЯ

От request_type зависит, появится ли у клиента продажная сделка, поэтому ставь
его аккуратно и не угадывай продажу заранее.

new_sale — человек интересуется услугой для себя, ребёнка или другого человека
и пока не является клиентом по этой услуге.

upsell — действующий клиент хочет добавить услугу. Признак действующего клиента
бери из CONTACT_CONTEXT и из CRM_CONTEXT, сам его не придумывай.

current_client_service — вопрос по уже оплаченной услуге: расписание своей
группы, пропуск, справка, заморозка абонемента.

complaint_or_problem — жалоба, конфликт, требование возврата.

business_or_partnership — встречное деловое предложение по делу академии:
поставка, аренда, трудоустройство, совместное мероприятие.

non_target — обращение не относится к работе Eagles: массовая рассылка, реклама
чужих услуг вроде продвижения сайта или займа, ошибка адресом.

unknown — достаточного сигнала ещё нет. Ставь его на обычное приветствие или
общий вопрос без темы.

СТАДИИ

Ты можешь запрашивать только переходы none, ai_consultation, deferred_interest
и manager_required. Ты не переводишь сделку в работу менеджера, предложение,
оплату, успешную или неуспешную стадию.

ОТВЕТ

Верни объект строго по заданной схеме. decision_version всегда "1.0". Поле
reply.text содержит только сообщение для клиента: без внутренних инструкций,
рассуждений, технических идентификаторов и содержимого CRM_CONTEXT.

БАЗА ЗНАНИЙ:
{knowledge}
"""


def build_instructions(knowledge_base: str) -> str:
    return SALES_INSTRUCTIONS.replace("{knowledge}", knowledge_base)


def build_model_input(
    *,
    current_time: str,
    crm_context: Dict[str, Any],
    contact_context: Dict[str, Any],
    recent_messages: Sequence[Dict[str, str]],
    current_message: Dict[str, str],
) -> str:
    """Изменяемая часть входа модели по шаблону docs/model_instructions_v1.md."""

    def block(name: str, value: Any) -> str:
        return f"{name}\n{json.dumps(value, ensure_ascii=False, indent=2)}"

    return "\n\n".join(
        (
            block(
                "CURRENT_TIME",
                {"local_datetime": current_time, "timezone": "Europe/Moscow"},
            ),
            block("CRM_CONTEXT", crm_context),
            block("CONTACT_CONTEXT", contact_context),
            block("RECENT_MESSAGES", list(recent_messages)),
            block("CURRENT_CLIENT_MESSAGE", current_message),
        )
    )


def request_decision(
    client: Any,
    *,
    model: str,
    instructions: str,
    user_input: str,
    max_output_tokens: int = 1600,
) -> Dict[str, Any]:
    """Один вызов Responses API со строгой JSON Schema."""
    response = client.responses.create(
        model=model,
        instructions=instructions,
        input=[{"role": "user", "content": user_input}],
        text={
            "format": {
                "type": "json_schema",
                "name": "eagles_sales_decision",
                "strict": True,
                "schema": load_schema(),
            }
        },
        max_output_tokens=max_output_tokens,
        store=False,
    )
    if getattr(response, "status", "completed") == "incomplete":
        raise RuntimeError("Модель не завершила ответ")

    raw = (response.output_text or "").strip()
    if not raw:
        raise RuntimeError("Модель вернула пустое решение")
    decision = json.loads(raw)
    if not isinstance(decision, dict):
        raise RuntimeError("Решение модели не является объектом")
    return decision


# -- проверка бизнес-правил ------------------------------------------------


def check_semantics(decision: Dict[str, Any]) -> List[str]:
    """Расхождения между action, reply, stage_transition и handoff."""
    problems: List[str] = []
    action = decision.get("action", "")
    reply = decision.get("reply", {})
    analysis = decision.get("crm_analysis", {})
    stage = decision.get("stage_transition", {})
    handoff = decision.get("handoff", {})
    safety = decision.get("safety", {})

    if decision.get("decision_version") != DECISION_VERSION:
        problems.append("decision_version_mismatch")

    if action == "reply_and_continue":
        if handoff.get("required"):
            problems.append("continue_with_handoff")
        if stage.get("target") not in ("none", "ai_consultation"):
            problems.append("continue_with_forbidden_stage")
        if analysis.get("next_action") == "handoff_to_manager":
            problems.append("continue_with_handoff_action")
    elif action == "reply_and_handoff":
        if not handoff.get("required"):
            problems.append("handoff_without_flag")
        if stage.get("target") != "manager_required":
            problems.append("handoff_without_stage")
        if handoff.get("reason") in (None, "", "none"):
            problems.append("handoff_without_reason")
        if not str(handoff.get("summary") or "").strip():
            problems.append("handoff_without_summary")
        if not str(handoff.get("manager_task") or "").strip():
            problems.append("handoff_without_manager_task")
    elif action == "reply_and_defer":
        if analysis.get("client_state") != "deferred":
            problems.append("defer_without_state")
        if stage.get("target") != "deferred_interest":
            problems.append("defer_without_stage")
    elif action == "close_conversation":
        if reply.get("asks_question"):
            problems.append("close_with_question")
    elif action == "no_reply":
        if reply.get("should_send") or str(reply.get("text") or "").strip():
            problems.append("no_reply_with_text")
        if stage.get("requested"):
            problems.append("no_reply_with_stage")

    if safety.get("requires_human"):
        if not handoff.get("required"):
            problems.append("safety_without_handoff")
        if analysis.get("client_state") != "human_required":
            problems.append("safety_without_state")
        if stage.get("target") != "manager_required":
            problems.append("safety_without_stage")

    return problems


def needs_handoff(decision: Dict[str, Any]) -> bool:
    """Передача в безопасную сторону: любой её признак включает передачу."""
    return bool(
        decision.get("handoff", {}).get("required")
        or decision.get("safety", {}).get("requires_human")
        or decision.get("stage_transition", {}).get("target") == "manager_required"
        or decision.get("crm_analysis", {}).get("next_action") == "handoff_to_manager"
    )


def resolve_stage(current_stage: str, decision: Dict[str, Any]) -> Tuple[Optional[str], str]:
    """Итоговая стадия сделки и причина отклонения запрошенного перехода.

    С «Нового обращения» приложение само забирает диалог в консультацию: это
    его собственное действие, а не переход, предложенный моделью.
    """
    effective = "ai_consultation" if current_stage == "new" else current_stage
    requested = decision.get("stage_transition", {}).get("target") or "none"
    if needs_handoff(decision):
        requested = "manager_required"

    rejection = ""
    if requested in ("none", effective):
        target = effective
    elif requested in bitrix_crm.ALLOWED_TRANSITIONS.get(effective, ()):
        target = requested
    else:
        target = effective
        rejection = f"transition_rejected:{effective}->{requested}"

    return (target if target != current_stage else None), rejection


def _analysis_values(decision: Dict[str, Any]) -> Dict[str, Any]:
    analysis = decision.get("crm_analysis", {})
    values: Dict[str, Any] = {}
    for name in ANALYSIS_FIELDS:
        if name not in analysis:
            continue
        value = analysis[name]
        if name == "missing_data":
            items = [str(item).strip() for item in value or [] if str(item).strip()]
            value = "; ".join(items)
        values[name] = value
    return values


def _fact_values(decision: Dict[str, Any]) -> Tuple[Dict[str, Any], List[str]]:
    """Значения из extracted_facts для полей, которых нет в crm_analysis."""
    values: Dict[str, Any] = {}
    rejected: List[str] = []
    for fact in decision.get("extracted_facts", []) or []:
        name = str(fact.get("field") or "")
        text = str(fact.get("value_text") or "").strip()
        if name in CONTACT_FIELDS:
            rejected.append(f"{name}:contact_write_not_enabled")
            continue
        if name not in FACT_ONLY_FIELDS:
            # Поля crm_analysis берутся из полного состояния, а не из фактов.
            continue
        if not text:
            rejected.append(f"{name}:empty_value")
            continue
        if fact.get("value_type") == "enum_list" or name in bitrix_crm.MULTIPLE_FIELDS:
            items = [part.strip() for part in text.split(",") if part.strip()]
            values.setdefault(name, [])
            values[name] = sorted(set(values[name]) | set(items))
        elif fact.get("value_type") == "integer":
            try:
                values[name] = int(text)
            except ValueError:
                rejected.append(f"{name}:not_an_integer")
        else:
            values[name] = text
    return values, rejected


def _conflict_text(decision: Dict[str, Any]) -> str:
    lines = []
    for conflict in decision.get("conflicts", []) or []:
        field = str(conflict.get("field") or "?")
        crm_value = str(conflict.get("crm_value") or "")
        new_value = str(conflict.get("new_value") or "")
        explanation = str(conflict.get("explanation") or "")
        lines.append(f"{field}: в CRM «{crm_value}», в сообщении «{new_value}». {explanation}".strip())
    return "\n".join(lines)


def _keeps_known(name: str, new_value: Any, current_value: Any) -> bool:
    """Нельзя стирать известное неизвестным или пустым."""
    neutral = bitrix_crm.NEUTRAL_VALUES.get(name)
    known = current_value not in (None, "", [], neutral)
    if isinstance(new_value, str) and not new_value.strip():
        return False
    if new_value is None:
        return False
    if isinstance(new_value, list) and not new_value:
        return False
    if neutral is not None and new_value == neutral and known:
        return False
    return True


def plan_updates(
    decision: Dict[str, Any],
    current_values: Dict[str, Any],
    crm: bitrix_crm.BitrixClient,
) -> Tuple[Dict[str, Any], Dict[str, Any], List[str]]:
    """План записи: (поля REST, принятые логические значения, отклонения)."""
    candidates: Dict[str, Any] = {}
    candidates.update(_analysis_values(decision))

    fact_values, rejected = _fact_values(decision)
    candidates.update(fact_values)

    handoff = decision.get("handoff", {})
    if needs_handoff(decision):
        reason = str(handoff.get("reason") or "").strip()
        if reason and reason != "none":
            candidates["handoff_reason"] = reason
        summary = str(handoff.get("summary") or "").strip()
        task = str(handoff.get("manager_task") or "").strip()
        if summary or task:
            candidates["manager_summary"] = "\n\n".join(
                part for part in (summary, f"Задача менеджеру: {task}" if task else "") if part
            )
        # crm_analysis.missing_data по инструкции содержит полное состояние,
        # а handoff.missing_data повторяет его другими словами. Берём второй
        # список только когда первый пуст, иначе в поле попадают дубли.
        if not str(candidates.get("missing_data") or "").strip():
            extra_missing = [
                str(item).strip()
                for item in handoff.get("missing_data") or []
                if str(item).strip()
            ]
            if extra_missing:
                candidates["missing_data"] = "; ".join(extra_missing)

    conflict_text = _conflict_text(decision)
    if conflict_text:
        candidates["data_conflict"] = conflict_text

    rest_fields: Dict[str, Any] = {}
    applied: Dict[str, Any] = {}
    for name, value in candidates.items():
        current = current_values.get(name)

        if name in bitrix_crm.MULTIPLE_FIELDS:
            items = value if isinstance(value, list) else [value]
            items = [str(item) for item in items if str(item).strip()]
            if not items:
                continue
            merged = sorted(set(items) | set(current or []))
            if merged == sorted(current or []):
                continue
            value = merged
        else:
            if not _keeps_known(name, value, current):
                continue
            if value == current:
                continue

        accepted, rest_value, reason = crm.to_rest(name, value)
        if not accepted:
            rejected.append(f"{name}:{reason}")
            continue
        rest_fields[bitrix_crm.DEAL_FIELD_MAP[name]] = rest_value
        applied[name] = value

    return rest_fields, applied, rejected
