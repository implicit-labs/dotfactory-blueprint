import unittest

from dotfactory.live_runner import CodexAdapter, _durable_event_payload


class CodexErrorDiagnosticsTests(unittest.TestCase):
    def payload(self, frame, sensitive=()):
        event = CodexAdapter().frame_event(frame)
        return event, _durable_event_payload(frame, event, sensitive=sensitive)

    def test_skill_budget_notice_is_nonfatal_but_terminal_errors_remain_errors(self):
        notice = ("Skill descriptions were shortened to fit the skills context budget. "
                  "Codex can still see every skill, but some descriptions are shorter. "
                  "Disable unused skills or plugins to leave more room for the rest.")
        event, payload = self.payload({"type": "item.completed", "item": {
            "type": "error", "message": notice}})
        self.assertEqual("warning", event.kind)
        self.assertEqual(notice, payload["excerpt"])
        for kind in ("exec_command.completed", "tool_call.completed"):
            event, _ = self.payload({"type": kind, "item": {"type": "error", "message": notice}})
            self.assertEqual("error", event.kind)
        for kind in ("turn.failed", "error"):
            event, _ = self.payload({"type": kind, "message": notice})
            self.assertEqual("error", event.kind)
        event, _ = self.payload({"type": "item.completed", "item": {
            "type": "error", "message": notice + " Another error."}})
        self.assertEqual("error", event.kind)

    def test_explicit_warning_retains_redacted_message(self):
        event, payload = self.payload({"type": "warning", "message": "fixture-secret notice"}, ("fixture-secret",))
        self.assertEqual("warning", event.kind)
        self.assertNotIn("fixture-secret", payload["excerpt"])

    def test_nested_error_retains_redacted_bounded_message(self):
        event, payload = self.payload({"type": "item.completed", "item": {
            "id": "failure", "type": "error",
            "message": "fixture-secret: provider rejected request " + "x" * 4096,
        }}, ("fixture-secret",))
        self.assertEqual("error", event.kind)
        self.assertEqual("item.completed", event.protocol_type)
        self.assertEqual("failure", payload["item_id"])
        self.assertIn("provider rejected request", payload["excerpt"])
        self.assertNotIn("fixture-secret", payload["excerpt"])
        self.assertEqual(2048, len(payload["excerpt"]))
        self.assertTrue(payload["excerpt_truncated"])

    def test_secret_crossing_truncation_boundary_is_redacted_first(self):
        _, payload = self.payload({"type": "error", "message":
            "x" * 2045 + "fixture-secret"}, ("fixture-secret",))
        self.assertEqual("x" * 2045 + "[RE", payload["excerpt"])

    def test_top_level_error_precedence_and_nested_fallback(self):
        for message in (None, "", "outer"):
            with self.subTest(message=message):
                _, payload = self.payload({"type": "item.completed", "message": message,
                    "item": {"type": "error", "message": "inner"}})
                self.assertEqual(message or "inner", payload["excerpt"])

    def test_missing_or_structured_message_is_not_serialized(self):
        for value in (None, {}, ["private"], 42):
            with self.subTest(value=value):
                event, payload = self.payload({"type": "item.completed", "item": {
                    "type": "error", "message": value}})
                self.assertEqual("error", event.kind)
                self.assertNotIn("excerpt", payload)

    def test_normal_tool_and_assistant_classification_is_unchanged(self):
        for item_type, expected in (("command_execution", "tool_result"),
                                    ("agent_message", "assistant")):
            event, payload = self.payload({"type": "item.completed", "item": {
                "type": item_type, "message": "not an error"}})
            self.assertEqual(expected, event.kind)
            self.assertNotIn("excerpt", payload)


if __name__ == "__main__":
    unittest.main()
