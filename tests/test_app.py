import unittest
from types import SimpleNamespace
from unittest.mock import patch

import app


class FakeResponses:
    def __init__(self) -> None:
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        return SimpleNamespace(output_text=f"Ответ {len(self.calls)}")


class ConversationMemoryTest(unittest.TestCase):
    def setUp(self) -> None:
        with app.conversation_state_lock:
            app.conversation_histories.clear()
            app.conversation_locks.clear()

    def test_second_turn_contains_previous_exchange(self) -> None:
        responses = FakeResponses()
        fake_client = SimpleNamespace(responses=responses)

        with patch.object(app, "client", fake_client):
            app.make_openai_reply("Первое сообщение", "test-chat")
            app.make_openai_reply("Второе сообщение", "test-chat")

        second_input = responses.calls[1]["input"]
        self.assertEqual(
            [item["role"] for item in second_input],
            ["user", "assistant", "user"],
        )
        self.assertEqual(second_input[0]["content"], "Первое сообщение")
        self.assertEqual(second_input[1]["content"], "Ответ 1")
        self.assertEqual(second_input[2]["content"], "Второе сообщение")

    def test_reset_removes_history(self) -> None:
        responses = FakeResponses()
        fake_client = SimpleNamespace(responses=responses)

        with patch.object(app, "client", fake_client):
            app.make_openai_reply("До сброса", "test-chat")
            app.reset_conversation("test-chat")
            app.make_openai_reply("После сброса", "test-chat")

        self.assertEqual(len(responses.calls[1]["input"]), 1)
        self.assertEqual(responses.calls[1]["input"][0]["content"], "После сброса")


if __name__ == "__main__":
    unittest.main()
