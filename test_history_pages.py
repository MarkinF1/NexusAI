"""История порциями: границы ходов, неизменяемый SSE-курсор и retry."""
import unittest
from unittest.mock import patch

import test_core  # изолированные данные и фиктивный CLI
import app
import core


class HistoryPages(unittest.TestCase):
    def page(self, events, before=None, limit=20):
        with patch.object(core, "history", return_value=events), patch.object(
                core, "load_chat", return_value={"engine": "codex"}):
            return core.history_page(1, "chat", before, limit)

    def turns(self, n):
        return [event for i in range(n) for event in (
            {"kind": "user", "text": f"question {i}"},
            {"kind": "tool", "name": "Read", "input": str(i)},
            {"kind": "text", "text": f"answer {i}"},
            {"kind": "done"})]

    def test_twenty_messages_and_complete_walk(self):
        events = self.turns(25)
        latest = self.page(events)
        self.assertEqual(latest["before"], 60)
        self.assertEqual(latest["total"], 100)
        self.assertEqual(sum(e["kind"] in ("user", "text") for e in latest["history"]), 20)
        middle = self.page(events, latest["before"])
        first = self.page(events, middle["before"])
        self.assertIsNone(first["before"])
        combined = first["history"] + middle["history"] + latest["history"]
        self.assertEqual(combined, [{"engine": "codex", **e} for e in events])

    def test_cursor_is_stable_when_new_events_arrive(self):
        events = self.turns(15)
        cursor = self.page(events)["before"]
        older = self.page(events, cursor)
        self.assertEqual(self.page(events + self.turns(3), cursor)["history"], older["history"])

    def test_retry_hides_replaced_turn_but_keeps_sse_position(self):
        events = self.turns(15) + [{"kind": "retry"}] + self.turns(1)
        page = self.page(events)
        self.assertEqual(page["total"], len(events))
        texts = [e.get("text") for e in page["history"]]
        self.assertNotIn("question 14", texts)
        self.assertNotIn("answer 14", texts)
        self.assertFalse(any(e["kind"] == "retry" for e in page["history"]))

    def test_page_does_not_split_turn_or_confirmed_voice(self):
        events = self.turns(1) + [
            {"kind": "voice", "id": "voice-1"},
            {"kind": "transcript", "voice": "voice-1", "text": "dictation"},
            {"kind": "voice_answer", "voice": "voice-1", "ok": True},
            {"kind": "user", "text": "dictation"},
            {"kind": "text", "text": "response"}, {"kind": "done"}]
        page = self.page(events, limit=1)
        self.assertEqual(page["before"], 4)
        self.assertEqual(page["history"][0]["kind"], "voice")

    def test_engine_provenance_survives_missing_transfer_on_page(self):
        events = [{"kind": "engine_changed", "engine": "claude"}] + self.turns(20)
        self.assertTrue(all(e["engine"] == "claude" for e in self.page(events)["history"]))

    def test_empty_history_and_zero_cursor(self):
        self.assertEqual(self.page([]), {"history": [], "before": None, "total": 0})
        self.assertEqual(self.page(self.turns(5), before=0)["history"], [])

    def test_endpoint_ownership_and_validation(self):
        owner = {"id": 1}
        with patch.object(app, "own_chat", return_value={"id": "chat"}) as own, patch.object(
                core, "history_page", return_value={"history": [], "before": None, "total": 42}):
            self.assertEqual(app.get_chat("chat", owner)["total"], 42)
            own.assert_called_with(owner, "chat")
            for before, limit in ((-1, 20), (None, 0), (None, 101)):
                with self.assertRaises(app.HTTPException):
                    app.get_chat("chat", owner, before, limit)


if __name__ == "__main__":
    unittest.main()
