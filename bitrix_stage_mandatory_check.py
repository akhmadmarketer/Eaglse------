"""Read-only проверка настройки обязательности полей по стадиям.

Скрипт ничего не записывает в Bitrix24. Он не выводит URL вебхука, токены,
значения CRM-полей, сделки, контакты и переписки.

REST не отдаёт перечень стадий, на которых поле помечено обязательным, поэтому
сам факт отметки проверяется глазами в интерфейсе. Скрипт подтверждает
отсутствие побочных эффектов ручной настройки:

- глобальная обязательность проектных полей осталась `MANDATORY=N`;
- в общей конфигурации карточки сделки по-прежнему шесть проектных разделов;
- все 41 проектное поле размещены ровно по одному разу;
- стадии основной воронки не изменились.
"""

import sys
from pathlib import Path
from typing import Any, Dict, List

import bitrix_openline as bitrix
from bitrix_crm_card_layout_setup import DEAL_CATEGORY_ID, ENTITY_TYPE_ID, SCOPE, SECTIONS


ROOT = Path(__file__).resolve().parent

EXPECTED_STAGES = (
    "NEW",
    "UC_JMBAX5",
    "UC_XI4M3M",
    "UC_2YCHH0",
    "UC_4OCEJT",
    "UC_KJBWTE",
    "UC_N9VQVU",
    "UC_SXEQHN",
    "WON",
    "LOSE",
)

# Поля, у которых обязательность по стадиям настраивается вручную,
# и стадии, которые должны быть отмечены в интерфейсе.
STAGE_MANDATORY_PLAN = (
    ("UF_CRM_1788531323", "Тип обращения", ("UC_JMBAX5",)),
    ("UF_CRM_1788430950688", "Основная услуга", ("UC_JMBAX5",)),
    ("UF_CRM_EAGLES_PRIMARY_JOB", "Основная задача клиента", ("UC_JMBAX5",)),
    ("UF_CRM_EAGLES_CLIENT_STATE", "Состояние клиента", ("UC_JMBAX5", "UC_XI4M3M")),
    ("UF_CRM_EAGLES_NEXT_ACTION", "Следующее лучшее действие", ("UC_JMBAX5", "UC_2YCHH0")),
    ("UF_CRM_EAGLES_STATE_REASON", "Основание состояния", ("UC_XI4M3M",)),
    ("UF_CRM_EAGLES_FOLLOWUP_PERMISSION", "Согласованный повторный контакт", ("UC_XI4M3M",)),
    ("UF_CRM_EAGLES_HANDOFF_REASON", "Причина передачи менеджеру", ("UC_2YCHH0",)),
    ("UF_CRM_EAGLES_MANAGER_SUMMARY", "Резюме для менеджера", ("UC_2YCHH0",)),
    ("UF_CRM_EAGLES_QUALIFICATION_STATUS", "Статус квалификации", ("UC_2YCHH0",)),
    ("UF_CRM_EAGLES_MISSING_DATA", "Недостающие сведения", ("UC_2YCHH0",)),
    ("UF_CRM_EAGLES_CLOSE_REASON", "Причина закрытия", ("LOSE",)),
)


def project_field_names() -> List[str]:
    names: List[str] = []
    for _, _, fields in SECTIONS:
        names.extend(fields)
    return names


def collect_card_fields(configuration: Any) -> Dict[str, int]:
    counts: Dict[str, int] = {}

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            name = node.get("name")
            if isinstance(name, str) and node.keys() <= {"name", "optionFlags", "options", "title"}:
                counts[name] = counts.get(name, 0) + 1
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)

    walk(configuration)
    return counts


def check_stages() -> List[str]:
    problems: List[str] = []
    stages = bitrix.call_bitrix("crm.dealcategory.stage.list", {"id": DEAL_CATEGORY_ID}) or []
    actual = tuple(stage.get("STATUS_ID") for stage in stages)
    if actual != EXPECTED_STAGES:
        problems.append(f"состав или порядок стадий изменился: {actual}")
    print(f"стадии основной воронки: {len(actual)}, ожидалось {len(EXPECTED_STAGES)}")
    return problems


def check_global_mandatory() -> List[str]:
    problems: List[str] = []
    fields = bitrix.call_bitrix("crm.deal.userfield.list", {}) or []
    by_name = {field.get("FIELD_NAME"): field for field in fields}

    expected = project_field_names()
    missing = [name for name in expected if name not in by_name]
    if missing:
        problems.append(f"поля отсутствуют на портале: {', '.join(missing)}")

    for name in expected:
        field = by_name.get(name)
        if field and field.get("MANDATORY") != "N":
            problems.append(f"{name}: MANDATORY={field.get('MANDATORY')}, ожидалось N")

    print(f"проектных полей найдено: {len(expected) - len(missing)} из {len(expected)}")
    print("глобальная обязательность: " + ("нарушена" if problems else "везде N, как и задумано"))
    return problems


def check_card_layout() -> List[str]:
    problems: List[str] = []
    configuration = bitrix.call_bitrix(
        "crm.item.details.configuration.get",
        {"entityTypeId": ENTITY_TYPE_ID, "dealCategoryId": DEAL_CATEGORY_ID, "scope": SCOPE},
    ) or []

    # Разделы сопоставляются по названию, а не по коду: три раздела владелец
    # создал вручную, и Bitrix24 выдал им коды вида `user_*`. По этой же логике
    # работает bitrix_crm_card_layout_setup.py.
    section_titles = {
        str(block.get("title", "")) for block in configuration if isinstance(block, dict)
    }
    project_titles = {title for _, title, _ in SECTIONS}
    for _, title, _ in SECTIONS:
        if title not in section_titles:
            problems.append(f"раздел карточки пропал: {title}")

    counts = collect_card_fields(configuration)
    for name in project_field_names():
        placed = counts.get(name, 0)
        if placed == 0:
            problems.append(f"{name}: нет в карточке")
        elif placed > 1:
            problems.append(f"{name}: размещено {placed} раза, ожидался один")

    # Раздел, в котором поле лежит сейчас, против проектного раздела.
    actual_section: Dict[str, str] = {}
    for block in configuration:
        if not isinstance(block, dict):
            continue
        for element in block.get("elements") or []:
            if isinstance(element, dict) and element.get("name"):
                actual_section[str(element["name"])] = str(block.get("title", ""))

    moved: List[str] = []
    for _, title, field_names in SECTIONS:
        for name in field_names:
            where = actual_section.get(name)
            if where and where != title:
                moved.append(f"{name}: «{where}» вместо «{title}»")

    if moved:
        print(f"\nПоля вне своего проектного раздела: {len(moved)}")
        for line in moved:
            print(f"  - {line}")
        print("Bitrix24 поднимает поле в главный раздел карточки при включении\n"
              "обязательности. Проверьте, ожидаемо ли это, прежде чем править раскладку.")

    print(f"проектных разделов в карточке: {len(section_titles & project_titles)} из {len(SECTIONS)}")
    return problems


def print_manual_checklist() -> None:
    print("\nПеречень стадий REST не отдаёт. Проверить глазами в интерфейсе:")
    for index, (name, title, stages) in enumerate(STAGE_MANDATORY_PLAN, start=1):
        print(f"  {index:2}. {title} ({name}): {', '.join(stages)}")


def main() -> None:
    bitrix.load_env_file(ROOT / ".env.local")

    problems: List[str] = []
    problems.extend(check_stages())
    problems.extend(check_global_mandatory())
    problems.extend(check_card_layout())

    print_manual_checklist()

    if problems:
        print("\nПроблемы:")
        for problem in problems:
            print(f"  - {problem}")
        sys.exit(1)
    print("\nКритичных проблем не обнаружено. Предупреждения выше, если они есть,\n"
          "требуют вашего решения, но сами по себе поломкой не являются.")


if __name__ == "__main__":
    main()
