import argparse
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

import bitrix_openline as bitrix


ROOT = Path(__file__).resolve().parent

EnumItem = Tuple[str, str]


def enum_items(
    items: Sequence[EnumItem], default: Optional[str] = None
) -> List[Dict[str, Any]]:
    return [
        {
            "VALUE": label,
            "XML_ID": code,
            "SORT": index * 100,
            "DEF": "Y" if code == default else "N",
        }
        for index, (code, label) in enumerate(items, start=1)
    ]


def field(
    code: str,
    label: str,
    user_type: str,
    sort: int,
    *,
    multiple: bool = False,
    values: Sequence[EnumItem] = (),
    default: Optional[str] = None,
    rows: Optional[int] = None,
    show_filter: bool = True,
    editable: bool = True,
) -> Dict[str, Any]:
    result: Dict[str, Any] = {
        "FIELD_NAME": code,
        "XML_ID": code,
        "LABEL": label,
        "USER_TYPE_ID": user_type,
        "SORT": sort,
        "MULTIPLE": "Y" if multiple else "N",
        "MANDATORY": "N",
        "SHOW_FILTER": "Y" if show_filter else "N",
        "SHOW_IN_LIST": "N",
        "EDIT_IN_LIST": "Y" if editable else "N",
        "IS_SEARCHABLE": "N",
    }
    if user_type == "enumeration":
        result["LIST"] = enum_items(values, default)
        result["SETTINGS"] = {
            "DISPLAY": "UI",
            "LIST_HEIGHT": min(max(len(values), 2), 12),
        }
    elif user_type == "string" and rows:
        result["SETTINGS"] = {"ROWS": rows}
    return result


FIELDS: List[Dict[str, Any]] = [
    field(
        "EAGLES_AGE_GROUP",
        "Возрастная категория занимающегося",
        "enumeration",
        700,
        values=(
            ("child_6_12", "ребёнок 6–12 лет"),
            ("teen_13_17", "подросток 13–17 лет"),
            ("young_adult_18_24", "молодой взрослый 18–24 года"),
            ("adult", "взрослый"),
            ("mixed_team", "смешанная команда"),
            ("not_applicable", "не применимо"),
            ("unknown", "возраст не указан"),
        ),
        default="unknown",
    ),
    field(
        "EAGLES_PARTICIPANT_AGE",
        "Возраст занимающегося",
        "integer",
        800,
    ),
    field(
        "EAGLES_EXPERIENCE_LEVEL",
        "Уровень подготовки",
        "enumeration",
        900,
        values=(
            ("never_trained", "никогда не занимался"),
            ("beginner", "начинающий"),
            ("returning", "возвращается после перерыва"),
            ("regular", "занимается регулярно"),
            ("experienced_athlete", "опытный спортсмен"),
            ("competition_athlete", "готовится к соревнованиям"),
            ("mixed_team", "смешанный уровень команды"),
            ("not_applicable", "не применимо"),
            ("unknown", "пока не определено"),
        ),
        default="unknown",
    ),
    field(
        "EAGLES_PRIMARY_JOB",
        "Основная задача клиента",
        "enumeration",
        1000,
        values=(
            ("start_sport", "начать заниматься спортом"),
            ("improve_fitness", "улучшить физическую форму"),
            ("learn_martial_art", "освоить единоборство"),
            ("gain_confidence", "стать увереннее"),
            ("child_discipline_and_development", "дисциплина и развитие ребёнка"),
            ("channel_teen_energy", "направить энергию подростка"),
            ("find_sport_community", "найти спортивное окружение"),
            ("improve_technical_level", "повысить технический уровень"),
            ("prepare_for_competition", "подготовиться к соревнованиям"),
            ("return_after_break", "вернуться после перерыва"),
            ("use_gym", "заниматься в тренажёрном зале"),
            ("choose_personal_format", "подобрать персональный формат"),
            ("recover_after_training", "восстановление после нагрузки"),
            ("organize_camp", "организовать сборы"),
            ("organize_accommodation", "организовать проживание"),
            ("organize_event", "организовать мероприятие"),
            ("unknown", "задача пока не определена"),
            ("other", "другая задача"),
        ),
        default="unknown",
    ),
    field(
        "EAGLES_CLIENT_GOAL_TEXT",
        "Задача словами клиента",
        "string",
        1100,
        rows=4,
    ),
    field(
        "EAGLES_TIME_PREFERENCE",
        "Предпочтения по времени",
        "string",
        1200,
        rows=3,
    ),
    field(
        "EAGLES_CLIENT_STATE",
        "Состояние клиента",
        "enumeration",
        1300,
        values=(
            ("unknown_intent", "намерение неизвестно"),
            ("cold_interest", "холодный интерес"),
            ("aware_interest", "осознанный интерес"),
            ("warm", "тёплый клиент"),
            ("ready_for_handoff", "готов к передаче"),
            ("deferred", "отложил решение"),
            ("refused", "отказался"),
            ("human_required", "требуется человек"),
            ("non_target", "нецелевое обращение"),
        ),
        default="unknown_intent",
    ),
    field(
        "EAGLES_STATE_REASON",
        "Основание состояния",
        "string",
        1400,
        rows=4,
    ),
    field(
        "EAGLES_MAIN_BARRIER",
        "Главный барьер",
        "enumeration",
        1500,
        values=(
            ("none", "барьер не выявлен"),
            ("choice_uncertainty", "не понимает, что выбрать"),
            ("price", "цена"),
            ("schedule", "расписание или время"),
            ("injury_or_load_fear", "страх травм или нагрузки"),
            ("age_or_low_fitness", "возраст или плохая форма"),
            ("academy_distrust", "недоверие к академии"),
            ("trainer_doubt", "сомнение в тренере"),
            ("atmosphere_or_judgment_fear", "атмосфера или страх оценки"),
            ("competitor_comparison", "сравнение с конкурентом"),
            ("needs_other_person_decision", "нужно посоветоваться"),
            ("not_ready_now", "не готов сейчас"),
            ("unconfirmed_conditions", "нет подтверждённых условий"),
            ("nonstandard_request", "нестандартный запрос"),
            ("health_requires_human", "здоровье — требуется человек"),
            ("other", "другое"),
            ("unknown", "пока не определено"),
        ),
        default="unknown",
    ),
    field(
        "EAGLES_BARRIER_TEXT",
        "Формулировка барьера клиента",
        "string",
        1600,
        rows=4,
    ),
    field(
        "EAGLES_BARRIER_STATUS",
        "Статус барьера",
        "enumeration",
        1700,
        values=(
            ("absent", "отсутствует"),
            ("detected", "обнаружен"),
            ("clarifying", "уточняется"),
            ("answered", "дан ответ"),
            ("resolved", "снят"),
            ("unresolved", "не снят"),
            ("requires_human", "требует человека"),
        ),
    ),
    field(
        "EAGLES_QUALIFICATION_STATUS",
        "Статус квалификации",
        "enumeration",
        1800,
        values=(
            ("not_started", "не начата"),
            ("partial", "частичная"),
            ("enough_for_ai", "достаточно для продолжения ботом"),
            ("enough_for_handoff", "достаточно для передачи"),
            ("handoff_incomplete", "передача без полной квалификации"),
            ("not_applicable", "не применимо"),
        ),
        default="not_started",
    ),
    field(
        "EAGLES_MISSING_DATA",
        "Недостающие сведения",
        "string",
        1900,
        rows=4,
    ),
    field(
        "EAGLES_NEXT_ACTION",
        "Следующее лучшее действие",
        "enumeration",
        2000,
        values=(
            ("answer_question", "ответить на вопрос"),
            ("clarify_goal", "уточнить задачу"),
            ("help_choose_service", "помочь выбрать услугу"),
            ("clarify_barrier", "уточнить главный барьер"),
            ("provide_verified_evidence", "дать подтверждённое доказательство"),
            ("check_barrier_resolution", "проверить, снят ли барьер"),
            ("request_staff_confirmation", "запросить актуальные данные у сотрудника"),
            ("handoff_to_manager", "передать менеджеру"),
            ("manager_contact_client", "связаться менеджеру"),
            ("wait_until_agreed_date", "дождаться согласованной даты"),
            ("suggest_close", "предложить закрытие"),
            ("none", "действие не требуется"),
            ("unknown", "действие пока не определено"),
        ),
        default="unknown",
    ),
    field(
        "EAGLES_HANDOFF_REASON",
        "Причина передачи менеджеру",
        "enumeration",
        2100,
        values=(
            ("client_ready", "клиент готов к оформлению"),
            ("client_requested_human", "клиент попросил человека"),
            ("needs_current_confirmation", "требуется актуальное подтверждение"),
            ("financial_or_contract", "финансовый или договорной вопрос"),
            ("trainer_question", "вопрос тренеру"),
            ("health_or_injury", "здоровье или травма"),
            ("complaint_or_conflict", "жалоба или конфликт"),
            ("individual_program", "индивидуальная программа"),
            ("team_or_complex_quote", "командный или комплексный расчёт"),
            ("nonstandard_conditions", "нестандартные условия"),
            ("other", "другое"),
            ("none", "передача пока не требуется"),
        ),
        default="none",
    ),
    field(
        "EAGLES_MANAGER_SUMMARY",
        "Резюме для менеджера",
        "string",
        2200,
        rows=6,
    ),
    field(
        "EAGLES_FOLLOWUP_PERMISSION",
        "Согласованный повторный контакт",
        "enumeration",
        2300,
        values=(
            ("agreed", "согласован"),
            ("client_will_contact", "клиент напишет сам"),
            ("not_agreed", "не согласован"),
            ("not_discussed", "не обсуждался"),
        ),
        default="not_discussed",
    ),
    field(
        "EAGLES_NEXT_CONTACT_AT",
        "Дата следующего контакта",
        "datetime",
        2400,
    ),
    field(
        "EAGLES_DATA_CONFLICT",
        "Конфликт данных",
        "string",
        2500,
        rows=4,
    ),
    field(
        "EAGLES_CLOSE_REASON",
        "Причина закрытия",
        "enumeration",
        2600,
        values=(
            ("client_refused", "клиент явно отказался"),
            ("price", "не подходит цена"),
            ("schedule", "не подходит расписание"),
            ("no_suitable_service", "нет подходящей услуги"),
            ("availability_unconfirmed", "нет подтверждённой доступности"),
            ("chose_other", "выбрал другой вариант"),
            ("unreachable_after_agreed_attempts", "не удалось связаться после согласованных попыток"),
            ("duplicate", "дубль"),
            ("non_target", "нецелевое обращение"),
            ("spam", "спам"),
            ("other", "другое с комментарием"),
        ),
    ),
    field("EAGLES_TEAM_NAME", "Название команды или организации", "string", 3000),
    field("EAGLES_TEAM_DISCIPLINE", "Дисциплина команды", "string", 3100),
    field("EAGLES_TEAM_START_DATE", "Дата начала", "date", 3200),
    field("EAGLES_TEAM_END_DATE", "Дата окончания", "date", 3300),
    field("EAGLES_ATHLETE_COUNT", "Количество спортсменов", "integer", 3400),
    field("EAGLES_COMPANION_COUNT", "Количество сопровождающих", "integer", 3500),
    field("EAGLES_TEAM_AGE_COMPOSITION", "Возрастной состав", "string", 3600, rows=3),
    field(
        "EAGLES_TEAM_REQUIRED_SERVICES",
        "Необходимые услуги",
        "enumeration",
        3700,
        multiple=True,
        values=(
            ("training_hall", "зал для тренировок"),
            ("accommodation", "проживание"),
            ("catering", "питание"),
            ("recovery_zone", "восстановительная зона"),
            ("gym", "тренажёрный зал"),
            ("other", "другая услуга"),
        ),
    ),
    field(
        "EAGLES_ACCOMMODATION_REQUIREMENTS",
        "Требования к проживанию",
        "string",
        3800,
        rows=4,
    ),
    field(
        "EAGLES_CATERING_REQUIREMENTS",
        "Требования к питанию",
        "string",
        3900,
        rows=4,
    ),
    field("EAGLES_OTHER_CONDITIONS", "Другие условия", "string", 4000, rows=4),
    field(
        "EAGLES_OPENLINE_CHAT_ID",
        "ID чата Открытой линии",
        "string",
        5000,
        show_filter=True,
        editable=False,
    ),
    field(
        "EAGLES_OPENLINE_SESSION_ID",
        "ID сессии Открытой линии",
        "string",
        5100,
        show_filter=True,
        editable=False,
    ),
    field(
        "EAGLES_AI_LAST_ANALYZED_AT",
        "Время последнего анализа ИИ",
        "datetime",
        5200,
        show_filter=True,
        editable=False,
    ),
    field(
        "EAGLES_AI_RULES_VERSION",
        "Версия правил ИИ",
        "string",
        5300,
        show_filter=True,
        editable=False,
    ),
]


def list_all_fields() -> List[Dict[str, Any]]:
    result: List[Dict[str, Any]] = []
    start = 0
    while True:
        page = bitrix.call_bitrix(
            "crm.deal.userfield.list",
            {
                "filter": {"LANG": "ru"},
                "order": {"ID": "ASC"},
                "start": start,
            },
        )
        if not isinstance(page, list):
            raise SystemExit("Bitrix24 вернул неожиданный список полей")
        result.extend(page)
        if len(page) < 50:
            return result
        start += 50


def normalized_label(item: Dict[str, Any]) -> str:
    value = item.get("EDIT_FORM_LABEL") or item.get("LIST_COLUMN_LABEL") or ""
    return str(value).strip().casefold()


def target_field_name(definition: Dict[str, Any]) -> str:
    return f"UF_CRM_{definition['FIELD_NAME']}"


def create_missing_fields(apply: bool) -> None:
    existing = list_all_fields()
    by_name = {str(item.get("FIELD_NAME", "")).upper(): item for item in existing}
    by_label = {normalized_label(item): item for item in existing}

    missing: List[Dict[str, Any]] = []
    for definition in FIELDS:
        name = target_field_name(definition).upper()
        label = str(definition["LABEL"]).strip().casefold()
        if name in by_name or label in by_label:
            found = by_name.get(name) or by_label[label]
            print(
                f"skip id={found.get('ID')} field={found.get('FIELD_NAME')} "
                f"label={definition['LABEL']}"
            )
            continue
        missing.append(definition)

    print(f"existing={len(existing)} missing={len(missing)}")
    if not apply:
        for definition in missing:
            print(
                f"would_create field={target_field_name(definition)} "
                f"type={definition['USER_TYPE_ID']} label={definition['LABEL']}"
            )
        return

    for definition in missing:
        new_id = bitrix.call_bitrix(
            "crm.deal.userfield.add", {"fields": definition}
        )
        print(
            f"created id={new_id} field={target_field_name(definition)} "
            f"label={definition['LABEL']}"
        )


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Идемпотентная настройка пользовательских полей сделок Eagles"
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Создать отсутствующие поля; без флага выполняется только план",
    )
    args = parser.parse_args()
    bitrix.load_env_file(ROOT / ".env.local")
    create_missing_fields(args.apply)


if __name__ == "__main__":
    main()
