"""Проверки маппинга CRM и бизнес-правил решения модели без обращений к порталу."""

import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import bitrix_crm
import sales_decision


SCHEMA = json.loads(
    (ROOT / "schemas" / "eagles_sales_decision.schema.json").read_text(encoding="utf-8")
)
ANALYSIS_PROPERTIES = SCHEMA["properties"]["crm_analysis"]["properties"]

MULTIPLE_ENUM = {"additional_services", "team_required_services"}
INTEGER_FIELDS = {"participant_age", "athlete_count", "companion_count"}
DATE_FIELDS = {"team_start_date", "team_end_date"}
DATETIME_FIELDS = {"next_contact_at", "ai_last_analyzed_at"}
EXTRA_ENUMS = {
    "handoff_reason": SCHEMA["properties"]["handoff"]["properties"]["reason"]["enum"],
    "followup_permission": ["agreed", "client_will_contact", "not_agreed", "not_discussed"],
    "team_required_services": [
        "training_hall",
        "accommodation",
        "catering",
        "recovery_zone",
        "gym",
        "other",
    ],
}


def enum_values(logical: str):
    """Допустимые значения списочного поля по той же схеме, что видит модель."""
    if logical in EXTRA_ENUMS:
        return EXTRA_ENUMS[logical]
    prop = ANALYSIS_PROPERTIES.get(logical)
    if not prop:
        return None
    if prop.get("type") == "array":
        return prop["items"].get("enum")
    return prop.get("enum")


def fake_metadata():
    """Метаданные полей портала, воспроизводящие фактические типы и XML_ID."""
    metadata = {}
    counter = 100
    for logical, uf_name in bitrix_crm.DEAL_FIELD_MAP.items():
        values = enum_values(logical)
        if values:
            enum_to_id, id_to_enum = {}, {}
            for value in values:
                counter += 2
                enum_to_id[value] = str(counter)
                id_to_enum[str(counter)] = value
            metadata[uf_name] = {
                "type": "enumeration",
                "multiple": logical in MULTIPLE_ENUM,
                "enum_to_id": enum_to_id,
                "id_to_enum": id_to_enum,
            }
            continue
        if logical in INTEGER_FIELDS:
            field_type = "integer"
        elif logical in DATE_FIELDS:
            field_type = "date"
        elif logical in DATETIME_FIELDS:
            field_type = "datetime"
        else:
            field_type = "string"
        metadata[uf_name] = {
            "type": field_type,
            "multiple": False,
            "enum_to_id": {},
            "id_to_enum": {},
        }

    # У «Основной услуги» два значения сохранили случайные XML_ID старого поля.
    service = metadata["UF_CRM_1788430950688"]
    for xml_id, element_id in bitrix_crm.ENUM_ID_OVERRIDES["UF_CRM_1788430950688"].items():
        service["enum_to_id"].pop(xml_id, None)
        service["enum_to_id"][xml_id] = element_id
        service["id_to_enum"][element_id] = xml_id
    return metadata


def make_client():
    client = bitrix_crm.BitrixClient("")
    client._metadata = fake_metadata()
    return client


def decision(**overrides):
    """Минимальное согласованное решение, поверх которого задаются отличия."""
    base = {
        "decision_version": "1.0",
        "action": "reply_and_continue",
        "reply": {
            "should_send": True,
            "text": "Ответ",
            "purpose": "answer_and_continue",
            "asks_question": True,
            "question_topic": "goal",
        },
        "crm_analysis": {
            "request_type": "new_sale",
            "sales_type": "individual",
            "service_for": "self",
            "primary_service": "bjj",
            "additional_services": [],
            "preferred_format": "unknown",
            "age_group": "adult",
            "participant_age": None,
            "experience_level": "never_trained",
            "primary_job": "start_sport",
            "client_goal_text": "Хочу начать заниматься",
            "time_preference": "",
            "client_state": "aware_interest",
            "state_reason": "Клиент назвал направление",
            "main_barrier": "none",
            "barrier_text": "",
            "barrier_status": "absent",
            "qualification_status": "partial",
            "missing_data": ["предпочтительное время"],
            "next_action": "clarify_goal",
        },
        "extracted_facts": [],
        "stage_transition": {
            "requested": True,
            "target": "ai_consultation",
            "reason": "Клиент задал вопрос об услуге",
            "evidence": "Хочу BJJ",
        },
        "handoff": {
            "required": False,
            "reason": "none",
            "summary": "",
            "manager_task": "",
            "missing_data": [],
        },
        "conflicts": [],
        "safety": {"requires_human": False, "category": "none", "reason": ""},
    }
    for key, value in overrides.items():
        if isinstance(value, dict) and isinstance(base.get(key), dict):
            base[key] = {**base[key], **value}
        else:
            base[key] = value
    return base


class EnumMappingTest(unittest.TestCase):
    def setUp(self):
        self.crm = make_client()

    def test_bjj_uses_legacy_element_id(self):
        accepted, value, reason = self.crm.to_rest("primary_service", "bjj")
        self.assertTrue(accepted, reason)
        self.assertEqual(value, "45")

    def test_freestyle_wrestling_uses_legacy_element_id(self):
        accepted, value, _ = self.crm.to_rest("primary_service", "freestyle_wrestling")
        self.assertTrue(accepted)
        self.assertEqual(value, "47")

    def test_unknown_enum_value_is_rejected(self):
        accepted, _, reason = self.crm.to_rest("primary_service", "sambo")
        self.assertFalse(accepted)
        self.assertIn("unknown_enum_value", reason)

    def test_field_outside_whitelist_is_rejected(self):
        accepted, _, reason = self.crm.to_rest("OPPORTUNITY", "50000")
        self.assertFalse(accepted)
        self.assertEqual(reason, "field_not_allowed")

    def test_multiple_enum_returns_list(self):
        accepted, value, _ = self.crm.to_rest("additional_services", ["gym", "bjj"])
        self.assertTrue(accepted)
        self.assertIsInstance(value, list)
        self.assertEqual(len(value), 2)

    def test_to_logical_reverses_enum_ids(self):
        values = self.crm.to_logical(
            {
                "UF_CRM_1788430950688": "45",
                "UF_CRM_EAGLES_PARTICIPANT_AGE": "9",
                "UF_CRM_EAGLES_STATE_REASON": "Основание",
            }
        )
        self.assertEqual(values["primary_service"], "bjj")
        self.assertEqual(values["participant_age"], "9")
        self.assertEqual(values["state_reason"], "Основание")
        self.assertIsNone(values["client_state"])


class StageTransitionTest(unittest.TestCase):
    def test_new_deal_is_taken_into_consultation(self):
        target, rejection = sales_decision.resolve_stage("new", decision())
        self.assertEqual(target, "ai_consultation")
        self.assertEqual(rejection, "")

    def test_handoff_moves_to_manager_required(self):
        target, rejection = sales_decision.resolve_stage(
            "ai_consultation",
            decision(
                action="reply_and_handoff",
                handoff={"required": True, "reason": "client_ready", "summary": "Резюме", "manager_task": "Связаться"},
                stage_transition={"target": "manager_required"},
            ),
        )
        self.assertEqual(target, "manager_required")
        self.assertEqual(rejection, "")

    def test_forbidden_target_is_blocked(self):
        target, rejection = sales_decision.resolve_stage(
            "ai_consultation", decision(stage_transition={"target": "won"})
        )
        self.assertIsNone(target)
        self.assertIn("transition_rejected", rejection)

    def test_no_transition_when_already_on_target(self):
        target, rejection = sales_decision.resolve_stage("ai_consultation", decision())
        self.assertIsNone(target)
        self.assertEqual(rejection, "")

    def test_deferred_returns_to_consultation(self):
        target, _ = sales_decision.resolve_stage(
            "deferred_interest",
            decision(stage_transition={"target": "ai_consultation"}),
        )
        self.assertEqual(target, "ai_consultation")

    def test_manager_stage_is_not_changed_by_model(self):
        target, rejection = sales_decision.resolve_stage(
            "manager_working", decision(stage_transition={"target": "ai_consultation"})
        )
        self.assertIsNone(target)
        self.assertIn("transition_rejected", rejection)


class PlanUpdatesTest(unittest.TestCase):
    def setUp(self):
        self.crm = make_client()

    def plan(self, model_decision, current=None):
        return sales_decision.plan_updates(model_decision, current or {}, self.crm)

    def test_known_value_is_not_erased_by_unknown(self):
        current = {"primary_service": "bjj", "experience_level": "regular"}
        _, applied, _ = self.plan(
            decision(crm_analysis={"primary_service": "unknown", "experience_level": "unknown"}),
            current,
        )
        self.assertNotIn("primary_service", applied)
        self.assertNotIn("experience_level", applied)

    def test_unknown_is_written_when_field_is_empty(self):
        _, applied, _ = self.plan(decision(crm_analysis={"preferred_format": "unknown"}))
        self.assertEqual(applied["preferred_format"], "unknown")

    def test_unchanged_value_is_not_rewritten(self):
        current = {"primary_service": "bjj"}
        fields, applied, _ = self.plan(decision(), current)
        self.assertNotIn("primary_service", applied)
        self.assertNotIn(bitrix_crm.DEAL_FIELD_MAP["primary_service"], fields)

    def test_additional_services_are_merged_not_replaced(self):
        current = {"additional_services": ["gym"]}
        _, applied, _ = self.plan(
            decision(crm_analysis={"additional_services": ["recovery_zone"]}), current
        )
        self.assertEqual(applied["additional_services"], ["gym", "recovery_zone"])

    def test_empty_additional_services_do_not_erase(self):
        current = {"additional_services": ["gym"]}
        _, applied, _ = self.plan(decision(), current)
        self.assertNotIn("additional_services", applied)

    def test_missing_data_list_becomes_text(self):
        _, applied, _ = self.plan(decision())
        self.assertEqual(applied["missing_data"], "предпочтительное время")

    def test_handoff_writes_reason_and_summary(self):
        _, applied, _ = self.plan(
            decision(
                action="reply_and_handoff",
                handoff={
                    "required": True,
                    "reason": "client_requested_human",
                    "summary": "Взрослый новичок, интерес к BJJ",
                    "manager_task": "Позвонить и записать",
                    "missing_data": ["телефон"],
                },
            )
        )
        self.assertEqual(applied["handoff_reason"], "client_requested_human")
        self.assertIn("Взрослый новичок", applied["manager_summary"])
        self.assertIn("Позвонить и записать", applied["manager_summary"])
        self.assertIn("телефон", applied["missing_data"])

    def test_conflicts_are_written_to_conflict_field(self):
        _, applied, _ = self.plan(
            decision(
                conflicts=[
                    {
                        "field": "participant_age",
                        "crm_value": "9",
                        "new_value": "12",
                        "explanation": "Клиент назвал другой возраст",
                        "needs_confirmation": True,
                    }
                ]
            )
        )
        self.assertIn("participant_age", applied["data_conflict"])
        self.assertIn("12", applied["data_conflict"])

    def test_contact_facts_are_rejected_in_this_version(self):
        _, applied, rejected = self.plan(
            decision(
                extracted_facts=[
                    {
                        "field": "contact_phone",
                        "operation": "set",
                        "value_type": "string",
                        "value_text": "+70000000000",
                        "source": "client_explicit",
                        "evidence": "Мой номер",
                    }
                ]
            )
        )
        self.assertNotIn("contact_phone", applied)
        self.assertTrue(any("contact_phone" in item for item in rejected))

    def test_team_facts_are_written_from_extracted_facts(self):
        _, applied, _ = self.plan(
            decision(
                extracted_facts=[
                    {
                        "field": "athlete_count",
                        "operation": "set",
                        "value_type": "integer",
                        "value_text": "18",
                        "source": "client_explicit",
                        "evidence": "Нас 18 спортсменов",
                    },
                    {
                        "field": "team_required_services",
                        "operation": "add",
                        "value_type": "enum_list",
                        "value_text": "accommodation, catering",
                        "source": "client_explicit",
                        "evidence": "Нужны проживание и питание",
                    },
                ]
            )
        )
        self.assertEqual(applied["athlete_count"], 18)
        self.assertEqual(applied["team_required_services"], ["accommodation", "catering"])

    def test_rest_payload_uses_portal_field_names(self):
        fields, _, _ = self.plan(decision())
        self.assertIn(bitrix_crm.DEAL_FIELD_MAP["client_state"], fields)
        self.assertIn(bitrix_crm.DEAL_FIELD_MAP["state_reason"], fields)


class SemanticsTest(unittest.TestCase):
    def test_consistent_decision_has_no_problems(self):
        self.assertEqual(sales_decision.check_semantics(decision()), [])

    def test_handoff_without_summary_is_reported(self):
        problems = sales_decision.check_semantics(
            decision(
                action="reply_and_handoff",
                handoff={"required": True, "reason": "client_ready", "summary": "", "manager_task": ""},
                stage_transition={"target": "manager_required"},
            )
        )
        self.assertIn("handoff_without_summary", problems)
        self.assertIn("handoff_without_manager_task", problems)

    def test_continue_with_handoff_flag_is_reported(self):
        problems = sales_decision.check_semantics(
            decision(handoff={"required": True, "reason": "client_ready"})
        )
        self.assertIn("continue_with_handoff", problems)

    def test_safety_requires_handoff(self):
        problems = sales_decision.check_semantics(
            decision(safety={"requires_human": True, "category": "health_or_injury", "reason": "травма"})
        )
        self.assertIn("safety_without_handoff", problems)

    def test_needs_handoff_detects_any_signal(self):
        self.assertFalse(sales_decision.needs_handoff(decision()))
        self.assertTrue(
            sales_decision.needs_handoff(
                decision(safety={"requires_human": True, "category": "complaint", "reason": "жалоба"})
            )
        )
        self.assertTrue(
            sales_decision.needs_handoff(
                decision(crm_analysis={"next_action": "handoff_to_manager"})
            )
        )


class ChatBindingTest(unittest.TestCase):
    def test_entity_data_is_parsed_into_deal_and_contact(self):
        client = make_client()
        client.call = lambda method, payload=None: {
            "entity_data_2": "LEAD|0|COMPANY|0|CONTACT|9|DEAL|13",
            "manager_list": [27, 33],
        }
        info = client.dialog_info("165")
        self.assertEqual(info["deal_id"], "13")
        self.assertEqual(info["contact_id"], "9")
        self.assertEqual(info["operators"], ["27", "33"])

    def test_missing_deal_is_reported_as_none(self):
        client = make_client()
        client.call = lambda method, payload=None: {
            "entity_data_2": "LEAD|7|COMPANY|0|CONTACT|0|DEAL|0",
            "manager_list": [],
        }
        info = client.dialog_info("165")
        self.assertIsNone(info["deal_id"])
        self.assertIsNone(info["contact_id"])


if __name__ == "__main__":
    unittest.main()
