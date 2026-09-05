import json
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import app
import bitrix_crm

from test_decision import decision, fake_metadata


class FakeResponses:
    """Заглушка Responses API, возвращающая готовое решение по схеме."""

    def __init__(self, decisions=None):
        self.calls = []
        self.decisions = list(decisions or [])

    def create(self, **kwargs):
        self.calls.append(kwargs)
        value = self.decisions.pop(0) if self.decisions else decision()
        return SimpleNamespace(output_text=json.dumps(value), status="completed")


def fake_openai(decisions=None):
    return SimpleNamespace(responses=FakeResponses(decisions))


class FakeBitrix(bitrix_crm.BitrixClient):
    """Клиент Bitrix24 с заранее заданными ответами и записью обновлений."""

    def __init__(self, stage="NEW", operators=(27,), deal_id=13, session_id=17, deleted=False):
        super().__init__("https://example.invalid/rest/1/token")
        self._metadata = fake_metadata()
        self.stage = stage
        self.operators = list(operators)
        self.deal_id = deal_id
        self.session_id = session_id
        self.deleted = deleted
        self.updates = []
        self.created = []

    def call(self, method, payload=None):
        if method == "imopenlines.dialog.get":
            return {
                "entity_data_1": f"Y|DEAL|{self.deal_id}|N|N|{self.session_id}|1788613560|0|0|0",
                "entity_data_2": f"LEAD|0|COMPANY|0|CONTACT|9|DEAL|{self.deal_id}",
                "manager_list": self.operators,
            }
        if method == "crm.deal.list":
            # Поиск открытой сделки контакта; удалённая сделка не находится.
            if self.deleted or not self.deal_id:
                return []
            return [{"ID": str(self.deal_id)}]
        if method == "crm.deal.add":
            self.created.append(payload)
            self.deleted = False
            self.deal_id = 77
            return 77
        if method == "crm.deal.get":
            if self.deleted:
                raise bitrix_crm.BitrixNotFound("crm.deal.get: не найден")
            return {
                "ID": str(self.deal_id),
                "STAGE_ID": self.stage,
                "CONTACT_ID": "9",
                "DATE_MODIFY": "2026-09-05T16:10:00+03:00",
            }
        if method == "crm.contact.get":
            return {"NAME": "Шамиль", "HAS_PHONE": "N", "HAS_EMAIL": "N"}
        if method == "crm.deal.update":
            self.updates.append(payload)
            return True
        raise AssertionError(f"неожиданный метод {method}")


def bitrix_event(text="Здравствуйте", message_id="1001", chat_id="165"):
    return {
        "event": "ONIMBOTV2MESSAGEADD",
        "message_id": message_id,
        "chat_id": chat_id,
        "entity_type": "LINES",
        "message": text,
        "author_id": "33",
        "bot_id": "27",
        "application_token": "",
        "is_system": "0",
    }


class ConversationMemoryTest(unittest.TestCase):
    def setUp(self):
        with app.conversation_state_lock:
            app.conversation_histories.clear()
            app.conversation_locks.clear()

    def test_second_turn_carries_previous_exchange(self):
        fake = fake_openai()
        with patch.object(app, "client", fake):
            app.make_local_reply("Первое сообщение", "test-chat")
            app.make_local_reply("Второе сообщение", "test-chat")

        second_input = fake.responses.calls[1]["input"][0]["content"]
        self.assertIn("RECENT_MESSAGES", second_input)
        self.assertIn("Первое сообщение", second_input)
        self.assertIn("Второе сообщение", second_input)

    def test_reset_removes_history(self):
        fake = fake_openai()
        with patch.object(app, "client", fake):
            app.make_local_reply("До сброса", "test-chat")
            app.reset_conversation("test-chat")
            app.make_local_reply("После сброса", "test-chat")

        second_input = fake.responses.calls[1]["input"][0]["content"]
        self.assertNotIn("До сброса", second_input)

    def test_request_uses_strict_json_schema(self):
        fake = fake_openai()
        with patch.object(app, "client", fake):
            app.make_local_reply("Вопрос", "test-chat")

        text_format = fake.responses.calls[0]["text"]["format"]
        self.assertEqual(text_format["type"], "json_schema")
        self.assertTrue(text_format["strict"])
        self.assertIn("crm_analysis", text_format["schema"]["properties"])


class BitrixPipelineTest(unittest.TestCase):
    def setUp(self):
        with app.conversation_state_lock:
            app.conversation_histories.clear()
            app.conversation_locks.clear()

    def run_message(self, crm, mode="apply", decisions=None, text="Хочу BJJ"):
        fake = fake_openai(decisions)
        with patch.object(app, "client", fake), patch.object(
            app, "crm", crm
        ), patch.object(app, "CRM_MODE", mode), patch.object(app, "B24_BOT_ID", "27"):
            reply = app.process_bitrix_message(bitrix_event(text), deliver=False)
        return reply, fake

    def test_new_deal_is_moved_to_consultation_and_filled(self):
        crm = FakeBitrix(stage="NEW")
        reply, _ = self.run_message(crm)

        self.assertEqual(reply, "Ответ")
        self.assertEqual(len(crm.updates), 1)
        fields = crm.updates[0]["fields"]
        self.assertEqual(fields["STAGE_ID"], bitrix_crm.STAGE_IDS["ai_consultation"])
        self.assertIn(bitrix_crm.DEAL_FIELD_MAP["client_state"], fields)
        self.assertEqual(fields[bitrix_crm.DEAL_FIELD_MAP["openline_chat_id"]], "165")
        self.assertEqual(
            fields[bitrix_crm.DEAL_FIELD_MAP["ai_rules_version"]],
            bitrix_crm.RULES_VERSION,
        )

    def test_handoff_moves_deal_to_manager_required(self):
        crm = FakeBitrix(stage="UC_JMBAX5")
        handoff_decision = decision(
            action="reply_and_handoff",
            reply={"purpose": "answer_and_handoff", "asks_question": False, "question_topic": "none"},
            crm_analysis={"client_state": "ready_for_handoff", "next_action": "handoff_to_manager"},
            stage_transition={"requested": True, "target": "manager_required"},
            handoff={
                "required": True,
                "reason": "client_ready",
                "summary": "Взрослый новичок, интерес к BJJ",
                "manager_task": "Связаться и записать",
                "missing_data": [],
            },
        )
        reply, _ = self.run_message(crm, decisions=[handoff_decision])

        self.assertEqual(reply, "Ответ")
        fields = crm.updates[0]["fields"]
        self.assertEqual(fields["STAGE_ID"], bitrix_crm.STAGE_IDS["manager_required"])
        self.assertIn(bitrix_crm.DEAL_FIELD_MAP["manager_summary"], fields)
        self.assertIn(bitrix_crm.DEAL_FIELD_MAP["handoff_reason"], fields)

    def test_session_id_is_written(self):
        crm = FakeBitrix(stage="UC_JMBAX5", session_id=17)
        self.run_message(crm)

        fields = crm.updates[0]["fields"]
        self.assertEqual(
            fields[bitrix_crm.DEAL_FIELD_MAP["openline_session_id"]], "17"
        )

    def test_missing_deal_is_replaced_by_a_new_one(self):
        """Открытой сделки у контакта нет — заводим новую и работаем с ней."""
        crm = FakeBitrix(stage="NEW", deleted=True)
        reply, _ = self.run_message(crm)

        self.assertEqual(reply, "Ответ")
        self.assertEqual(len(crm.created), 1)
        created = crm.created[0]["fields"]
        self.assertEqual(created["STAGE_ID"], bitrix_crm.STAGE_IDS["new"])
        self.assertEqual(created["CONTACT_ID"], 9)
        self.assertEqual(
            created[bitrix_crm.DEAL_FIELD_MAP["openline_chat_id"]], "165"
        )
        self.assertEqual(len(crm.updates), 1)

    def test_non_sales_request_does_not_create_a_deal(self):
        """Жалоба и сервисный вопрос не должны заводить продажную сделку."""
        for request_type in (
            "complaint_or_problem",
            "current_client_service",
            "business_or_partnership",
            "non_target",
            "unknown",
        ):
            with self.subTest(request_type=request_type):
                crm = FakeBitrix(stage="NEW", deleted=True)
                reply, _ = self.run_message(
                    crm,
                    decisions=[decision(crm_analysis={"request_type": request_type})],
                )
                self.assertEqual(reply, "Ответ")
                self.assertEqual(crm.created, [])
                self.assertEqual(crm.updates, [])

    def test_upsell_creates_a_deal(self):
        crm = FakeBitrix(stage="NEW", deleted=True)
        self.run_message(crm, decisions=[decision(crm_analysis={"request_type": "upsell"})])
        self.assertEqual(len(crm.created), 1)

    def test_deal_is_found_through_the_contact(self):
        """Сделку ищем по контакту, а не по одноразовой связи чата."""
        crm = FakeBitrix(stage="UC_JMBAX5")
        calls = []
        original = crm.call

        def spy(method, payload=None):
            calls.append((method, payload))
            return original(method, payload)

        crm.call = spy
        self.run_message(crm)

        deal_lists = [p for m, p in calls if m == "crm.deal.list"]
        self.assertTrue(deal_lists)
        self.assertEqual(deal_lists[0]["filter"]["CONTACT_ID"], 9)
        self.assertEqual(deal_lists[0]["filter"]["CLOSED"], "N")

    def test_plan_mode_reports_the_deal_it_would_create(self):
        """В режиме plan сделка не создаётся, но намерение видно в журнале."""
        crm = FakeBitrix(stage="NEW", deleted=True)
        with self.assertLogs("eagles", level="INFO") as logs:
            reply, fake = self.run_message(crm, mode="plan")

        self.assertEqual(reply, "Ответ")
        self.assertEqual(crm.created, [])
        self.assertEqual(crm.updates, [])
        self.assertEqual(len(fake.responses.calls), 1)
        self.assertTrue(
            any("создал бы сделку" in line for line in logs.output),
            logs.output,
        )

    def test_bot_is_silent_on_manager_stage(self):
        crm = FakeBitrix(stage="UC_4OCEJT")
        reply, fake = self.run_message(crm)

        self.assertIsNone(reply)
        self.assertEqual(fake.responses.calls, [])
        self.assertEqual(crm.updates, [])

    def test_bot_is_silent_when_manager_joined_chat(self):
        crm = FakeBitrix(stage="UC_JMBAX5", operators=(27, 33))
        reply, fake = self.run_message(crm)

        self.assertIsNone(reply)
        self.assertEqual(fake.responses.calls, [])

    def test_plan_mode_does_not_write(self):
        crm = FakeBitrix(stage="NEW")
        reply, _ = self.run_message(crm, mode="plan")

        self.assertEqual(reply, "Ответ")
        self.assertEqual(crm.updates, [])

    def test_handoff_reply_is_withheld_when_crm_write_fails(self):
        crm = FakeBitrix(stage="UC_JMBAX5")

        def failing_update(deal_id, fields):
            raise bitrix_crm.BitrixError("crm.deal.update: отказ")

        crm.update_deal = failing_update
        handoff_decision = decision(
            action="reply_and_handoff",
            crm_analysis={"client_state": "ready_for_handoff", "next_action": "handoff_to_manager"},
            stage_transition={"requested": True, "target": "manager_required"},
            handoff={
                "required": True,
                "reason": "client_ready",
                "summary": "Резюме",
                "manager_task": "Связаться",
                "missing_data": [],
            },
        )
        reply, _ = self.run_message(crm, decisions=[handoff_decision])
        self.assertIsNone(reply)

    def test_deal_context_reaches_the_model(self):
        crm = FakeBitrix(stage="UC_JMBAX5")
        _, fake = self.run_message(crm)

        sent = fake.responses.calls[0]["input"][0]["content"]
        self.assertIn('"stage": "ai_consultation"', sent)
        self.assertIn('"deal_id": "13"', sent)
        self.assertIn("CONTACT_CONTEXT", sent)


class KnowledgeBaseTest(unittest.TestCase):
    def test_service_notes_are_not_in_prompt(self):
        """Внутренние заметки не должны попадать в системный промпт модели."""
        for service_file in ("knowledge/README.md", "knowledge/review-needed.md"):
            self.assertNotIn(service_file, app.KNOWLEDGE_BASE)
        self.assertNotIn("Что нужно подтвердить у владельца", app.KNOWLEDGE_BASE)

    def test_facts_are_in_prompt(self):
        for facts_file in ("knowledge/static/services.md", "knowledge/dynamic/prices.json"):
            self.assertIn(facts_file, app.KNOWLEDGE_BASE)

    def test_mock_data_is_off_by_default(self):
        """Вымышленные цены не должны попадать в промпт без явного включения."""
        self.assertFalse(app.KNOWLEDGE_MOCK)
        self.assertNotIn("knowledge/mock/", app.KNOWLEDGE_BASE)
        self.assertIn("knowledge/dynamic/", app.KNOWLEDGE_BASE)

    def test_mock_dataset_is_complete_and_confirmed(self):
        """Мокапы должны закрывать то, чего нет в реальном снимке сайта."""
        import json

        mock_dir = ROOT / "knowledge" / "mock"
        names = {path.name for path in mock_dir.glob("*.json")}
        self.assertEqual(
            names, {"prices.json", "schedule.json", "trainers.json", "rules.json"}
        )
        for path in sorted(mock_dir.glob("*.json")):
            data = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(data.get("dataset"), "mock", path.name)
            records = data.get("items") or data.get("groups") or data.get("trainers")
            self.assertTrue(records, path.name)
            for record in records:
                self.assertEqual(record.get("status"), "confirmed", path.name)

    def test_instructions_contain_stage_and_crm_rules(self):
        self.assertIn("СТАДИИ", app.SALES_INSTRUCTIONS)
        self.assertIn("crm_analysis", app.SALES_INSTRUCTIONS)
        self.assertIn(app.KNOWLEDGE_BASE[:200], app.SALES_INSTRUCTIONS)


if __name__ == "__main__":
    unittest.main()
