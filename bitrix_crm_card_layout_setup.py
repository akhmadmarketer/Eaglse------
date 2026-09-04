import argparse
import copy
import json
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Sequence, Tuple

import bitrix_openline as bitrix


ROOT = Path(__file__).resolve().parent
ENTITY_TYPE_ID = 2
DEAL_CATEGORY_ID = 0
SCOPE = "C"

SectionDefinition = Tuple[str, str, Sequence[str]]


SECTIONS: Sequence[SectionDefinition] = (
    (
        "eagles_main_request",
        "Основное о запросе",
        (
            "UF_CRM_1788531323",
            "UF_CRM_1788532000",
            "UF_CRM_1788532461",
            "UF_CRM_1788430950688",
            "UF_CRM_1788533932",
            "UF_CRM_1788534247",
        ),
    ),
    (
        "eagles_client_and_goal",
        "Клиент и задача",
        (
            "UF_CRM_EAGLES_AGE_GROUP",
            "UF_CRM_EAGLES_PARTICIPANT_AGE",
            "UF_CRM_EAGLES_EXPERIENCE_LEVEL",
            "UF_CRM_EAGLES_PRIMARY_JOB",
            "UF_CRM_EAGLES_CLIENT_GOAL_TEXT",
            "UF_CRM_EAGLES_TIME_PREFERENCE",
        ),
    ),
    (
        "eagles_state_barrier_qualification",
        "Состояние, барьер и квалификация",
        (
            "UF_CRM_EAGLES_CLIENT_STATE",
            "UF_CRM_EAGLES_STATE_REASON",
            "UF_CRM_EAGLES_MAIN_BARRIER",
            "UF_CRM_EAGLES_BARRIER_TEXT",
            "UF_CRM_EAGLES_BARRIER_STATUS",
            "UF_CRM_EAGLES_QUALIFICATION_STATUS",
            "UF_CRM_EAGLES_MISSING_DATA",
            "UF_CRM_EAGLES_DATA_CONFLICT",
        ),
    ),
    (
        "eagles_handoff_and_followup",
        "Передача и следующий контакт",
        (
            "UF_CRM_EAGLES_NEXT_ACTION",
            "UF_CRM_EAGLES_HANDOFF_REASON",
            "UF_CRM_EAGLES_MANAGER_SUMMARY",
            "UF_CRM_EAGLES_FOLLOWUP_PERMISSION",
            "UF_CRM_EAGLES_NEXT_CONTACT_AT",
            "UF_CRM_EAGLES_CLOSE_REASON",
        ),
    ),
    (
        "eagles_team_or_complex",
        "Командный или комплексный запрос",
        (
            "UF_CRM_EAGLES_TEAM_NAME",
            "UF_CRM_EAGLES_TEAM_DISCIPLINE",
            "UF_CRM_EAGLES_TEAM_START_DATE",
            "UF_CRM_EAGLES_TEAM_END_DATE",
            "UF_CRM_EAGLES_ATHLETE_COUNT",
            "UF_CRM_EAGLES_COMPANION_COUNT",
            "UF_CRM_EAGLES_TEAM_AGE_COMPOSITION",
            "UF_CRM_EAGLES_TEAM_REQUIRED_SERVICES",
            "UF_CRM_EAGLES_ACCOMMODATION_REQUIREMENTS",
            "UF_CRM_EAGLES_CATERING_REQUIREMENTS",
            "UF_CRM_EAGLES_OTHER_CONDITIONS",
        ),
    ),
    (
        "eagles_integration_technical",
        "Технические данные интеграции",
        (
            "UF_CRM_EAGLES_OPENLINE_CHAT_ID",
            "UF_CRM_EAGLES_OPENLINE_SESSION_ID",
            "UF_CRM_EAGLES_AI_LAST_ANALYZED_AT",
            "UF_CRM_EAGLES_AI_RULES_VERSION",
        ),
    ),
)

PROJECT_TITLES = {title for _, title, _ in SECTIONS}
PROJECT_FIELDS = {
    field_name
    for _, _, field_names in SECTIONS
    for field_name in field_names
}


def get_configuration() -> List[Dict[str, Any]]:
    result = bitrix.call_bitrix(
        "crm.item.details.configuration.get",
        {
            "entityTypeId": ENTITY_TYPE_ID,
            "scope": SCOPE,
            "extras": {"dealCategoryId": DEAL_CATEGORY_ID},
        },
    )
    if not isinstance(result, list):
        raise SystemExit("Bitrix24 не вернул конфигурацию общей карточки сделки")
    return result


def cleaned_standard_sections(
    configuration: Sequence[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    result: List[Dict[str, Any]] = []
    for section in configuration:
        if str(section.get("title", "")) in PROJECT_TITLES:
            continue
        cleaned = copy.deepcopy(section)
        cleaned["elements"] = [
            element
            for element in cleaned.get("elements", [])
            if str(element.get("name", "")) not in PROJECT_FIELDS
        ]
        result.append(cleaned)
    return result


def build_configuration(
    current: Sequence[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    existing_sections = {
        str(section.get("title", "")): section for section in current
    }
    existing_elements: Dict[str, Dict[str, Any]] = {}
    for section in current:
        for element in section.get("elements", []):
            name = str(element.get("name", ""))
            if name in PROJECT_FIELDS:
                existing_elements[name] = copy.deepcopy(element)

    result = cleaned_standard_sections(current)
    used_names = {str(section.get("name", "")) for section in result}

    for default_name, title, field_names in SECTIONS:
        existing = existing_sections.get(title, {})
        section_name = str(existing.get("name", "")) or default_name
        if section_name in used_names:
            section_name = default_name
        suffix = 2
        candidate = section_name
        while candidate in used_names:
            candidate = f"{section_name}_{suffix}"
            suffix += 1
        used_names.add(candidate)

        elements = [
            existing_elements.get(field_name, {"name": field_name})
            for field_name in field_names
        ]
        result.append(
            {
                "name": candidate,
                "title": title,
                "type": "section",
                "elements": elements,
            }
        )
    return result


def project_signature(
    configuration: Sequence[Dict[str, Any]],
) -> List[Tuple[str, List[str]]]:
    return [
        (
            str(section.get("title", "")),
            [str(element.get("name", "")) for element in section.get("elements", [])],
        )
        for section in configuration
        if str(section.get("title", "")) in PROJECT_TITLES
    ]


def standard_signature(
    configuration: Sequence[Dict[str, Any]],
) -> List[Tuple[str, str, List[Dict[str, Any]]]]:
    return [
        (
            str(section.get("name", "")),
            str(section.get("title", "")),
            copy.deepcopy(section.get("elements", [])),
        )
        for section in cleaned_standard_sections(configuration)
    ]


def expected_project_signature() -> List[Tuple[str, List[str]]]:
    return [(title, list(field_names)) for _, title, field_names in SECTIONS]


def validate_configuration(
    before: Sequence[Dict[str, Any]],
    candidate: Sequence[Dict[str, Any]],
) -> None:
    if project_signature(candidate) != expected_project_signature():
        raise SystemExit("План проектных разделов не прошёл внутреннюю проверку")
    if standard_signature(before) != standard_signature(candidate):
        raise SystemExit("План изменяет стандартные разделы карточки")

    placed = [
        field_name
        for _, fields in project_signature(candidate)
        for field_name in fields
    ]
    if len(placed) != len(PROJECT_FIELDS) or set(placed) != PROJECT_FIELDS:
        raise SystemExit("Проектные поля размещены не полностью или с дублями")


def save_backup(configuration: Sequence[Dict[str, Any]]) -> Path:
    backup_dir = ROOT / "backups"
    backup_dir.mkdir(exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    path = backup_dir / f"bitrix_deal_card_before_{timestamp}.json"
    path.write_text(
        json.dumps(configuration, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return path


def set_configuration(configuration: Sequence[Dict[str, Any]]) -> None:
    result = bitrix.call_bitrix(
        "crm.item.details.configuration.set",
        {
            "entityTypeId": ENTITY_TYPE_ID,
            "scope": SCOPE,
            "data": list(configuration),
            "extras": {"dealCategoryId": DEAL_CATEGORY_ID},
        },
    )
    if result is not True:
        raise SystemExit("Bitrix24 не подтвердил запись конфигурации карточки")


def print_plan(configuration: Sequence[Dict[str, Any]]) -> None:
    for title, fields in project_signature(configuration):
        print(f"section={title} fields={len(fields)}")
    print(f"project_fields={len(PROJECT_FIELDS)}")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Настройка общего вида карточки сделки Eagles"
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Записать проверенную конфигурацию; без флага показать только план",
    )
    args = parser.parse_args()

    bitrix.load_env_file(ROOT / ".env.local")
    before = get_configuration()
    candidate = build_configuration(before)
    validate_configuration(before, candidate)
    print_plan(candidate)

    if not args.apply:
        print("mode=plan")
        return

    backup_path = save_backup(before)
    print(f"backup={backup_path.relative_to(ROOT)}")
    set_configuration(candidate)

    after = get_configuration()
    try:
        validate_configuration(before, after)
    except SystemExit:
        set_configuration(before)
        raise SystemExit("Проверка после записи не прошла; исходная конфигурация восстановлена")

    print("mode=applied verified=true")


if __name__ == "__main__":
    main()
