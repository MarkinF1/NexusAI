"""Provider handoff: local only, survives quota exhaustion, keeps provenance."""
import asyncio
import json
from pathlib import Path
import unittest
from unittest.mock import patch

import test_core  # isolated data directory and fake CLI, before application imports
import app
import core
import jobs
import users


class Transfer(unittest.TestCase):
    uid = 891

    def setUp(self):
        self.meta = core.create_chat(self.uid, "codex", "old-model", "high")
        self.meta.update(title="Проект", sid="original-thread")
        core.save_chat(self.uid, self.meta)
        self.root = Path(core.workspace(self.uid, self.meta["id"]))
        (self.root / "files/general/report.txt").write_text("important", encoding="utf-8")
        for event in [{"kind": "user", "text": "Реши задачу", "files": ["input.txt"]},
                      {"kind": "tool", "name": "Read", "input": "input.txt"},
                      {"kind": "text", "text": "Прежний ответ"}, {"kind": "done"}]:
            core.append_history(self.uid, self.meta["id"], event)
        self.avail = {"claude": [{"slug": "sonnet", "efforts": ["medium"], "auto": False}],
                      "codex": [{"slug": "new-codex", "efforts": ["high"], "auto": True}]}
        mocked = patch.object(users, "available", return_value=self.avail)
        mocked.start()
        self.addCleanup(mocked.stop)

    def transfer(self, meta=None, engine="claude"):
        meta = meta or self.meta
        return jobs.transfer(self.uid, meta, engine, meta["engine"], meta["updated"])

    def test_preserves_history_files_identity_and_provenance(self):
        journal = Path(core.chat_file(self.uid, self.meta["id"], ".jsonl"))
        before = journal.read_bytes()
        with patch.object(jobs.usage, "check", side_effect=AssertionError("must not check exhausted source")), \
                patch.object(core.subprocess, "run", side_effect=AssertionError("no CLI call")):
            new = self.transfer()
        self.assertEqual(new["id"], self.meta["id"])
        self.assertEqual(new["title"], "Проект")
        self.assertEqual(new["created"], self.meta["created"])
        self.assertEqual((new["engine"], new["model"], new["effort"]), ("claude", "sonnet", "medium"))
        self.assertNotEqual(new["sid"], self.meta["sid"])
        self.assertEqual(journal.read_bytes(), before)
        self.assertEqual((self.root / "files/general/report.txt").read_text(), "important")
        past = core.history(self.uid, new["id"])
        self.assertEqual([e["engine"] for e in past], ["codex"] * 4 + ["claude"])
        self.assertEqual(past[-1]["kind"], "engine_changed")
        self.assertIsNone(core.last_request(past))
        snapshot = self.root / new["handoff"]["file"]
        self.assertIn("Прежний ответ", snapshot.read_text())
        self.assertEqual(snapshot.stat().st_mode & 0o777, 0o600)
        self.assertGreater(core.chat_size(self.uid, new)["files"], snapshot.stat().st_size)

    def test_round_trip_always_fresh_session(self):
        first = self.transfer()
        second = self.transfer(first, "codex")
        self.assertEqual(second["sid"], "")
        self.assertEqual(len(second["transfers"]), 2)
        core.append_history(self.uid, second["id"], {"kind": "user", "text": "Продолжи"})
        past = core.history(self.uid, second["id"])
        self.assertEqual([e["engine"] for e in past[-3:]], ["claude", "codex", "codex"])
        self.assertEqual(core.last_request(past), len(past) - 1)

    def test_mixed_export(self):
        new = self.transfer()
        core.append_history(self.uid, new["id"], {"kind": "user", "text": "Далее"})
        core.append_history(self.uid, new["id"], {"kind": "text", "text": "Новый ответ"})
        md = core.export_markdown(new, core.history(self.uid, new["id"]))
        self.assertLess(md.index("### Codex"), md.index("Прежний ответ"))
        self.assertIn("Смена сети: Codex → Claude", md)
        self.assertLess(md.index("### Claude"), md.index("Новый ответ"))

    def test_context_bounded_complete_file_and_no_source_summary(self):
        core.append_history(self.uid, self.meta["id"], {"kind": "text", "text": "M" * 100000 + "END"})
        new = self.transfer()
        prompt = core.handoff_prompt(self.uid, new, "Следующий вопрос")
        self.assertTrue(prompt.startswith("Следующий вопрос"))
        self.assertLess(len(prompt), core.HANDOFF_LIMIT + 1500)
        self.assertIn("Середина истории опущена", prompt)
        self.assertIn("END", prompt)
        self.assertIn(new["handoff"]["file"], prompt)
        self.assertGreater(len((self.root / new["handoff"]["file"]).read_text()), 100000)
        # Mutable workspace copy is not trusted for inline context.
        (self.root / new["handoff"]["file"]).write_text("TAMPERED")
        self.assertNotIn("TAMPERED", core.handoff_prompt(self.uid, new, "go"))
        new["handoff"]["pending"] = False
        self.assertEqual(core.handoff_prompt(self.uid, new, "go"), "go")

    def test_first_success_consumes_handoff_failure_does_not(self):
        new = self.transfer()
        build = core.build_input
        with patch.object(core, "build_input", wraps=build) as mocked:
            asyncio.run(test_core.collect(core.run(self.uid, new, "Далее", test_core.ALL)))
            self.assertIn("Прежний ответ", mocked.call_args.args[0])
        self.assertFalse(new["handoff"]["pending"])
        with patch.object(core, "build_input", wraps=build) as mocked:
            asyncio.run(test_core.collect(core.run(self.uid, new, "Ещё", test_core.ALL)))
            self.assertNotIn("Прежний ответ", mocked.call_args.args[0])
        new["handoff"]["pending"] = True
        with patch.object(core.asyncio, "create_subprocess_exec", side_effect=OSError("no CLI")):
            with self.assertRaises(OSError):
                asyncio.run(test_core.collect(core.run(self.uid, new, "x", test_core.ALL)))
        self.assertTrue(new["handoff"]["pending"])

    def test_atomic_save_failure(self):
        files = set(self.root.iterdir())
        with patch.object(core, "save_chat", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                self.transfer()
        self.assertEqual(core.load_chat(self.uid, self.meta["id"]), self.meta)
        self.assertEqual(set(self.root.iterdir()), files)
        self.assertEqual(len(core.history(self.uid, self.meta["id"])), 4)

    def test_reject_stale_revision(self):
        for source, revision in [("claude", self.meta["updated"]), ("codex", 0)]:
            with self.assertRaises(jobs.Refused) as caught:
                jobs.transfer(self.uid, self.meta, "claude", source, revision)
            self.assertEqual(caught.exception.status, 409)

    def test_busy_transcribing_and_updating(self):
        key = (self.uid, self.meta["id"])
        jobs.jobs[key] = jobs.Job()
        try:
            with self.assertRaises(jobs.Refused) as caught:
                self.transfer()
            self.assertEqual(caught.exception.status, 409)
        finally:
            del jobs.jobs[key]
        jobs.transcribing[key] += 1
        try:
            with self.assertRaises(jobs.Refused):
                self.transfer()
        finally:
            del jobs.transcribing[key]
        with patch.object(jobs, "draining", return_value=True):
            with self.assertRaises(jobs.Refused) as caught:
                self.transfer()
            self.assertEqual(caught.exception.status, 503)

    def test_no_alternatives_same_engine_and_denied_target(self):
        for engine in ["codex", "unknown"]:
            with self.assertRaises(jobs.Refused) as caught:
                self.transfer(engine=engine)
            self.assertEqual(caught.exception.status, 403)
        del self.avail["claude"]
        self.assertEqual(jobs.transfer_targets(self.uid, self.meta), [])
        with self.assertRaises(jobs.Refused):
            self.transfer()

    def test_telegram_and_disk_quota(self):
        self.meta["tg"] = {"moved": False}
        with self.assertRaises(jobs.Refused):
            self.transfer()
        self.meta.pop("tg")
        with patch.object(jobs, "room", side_effect=jobs.Refused(507, "disk full")):
            with self.assertRaises(jobs.Refused) as caught:
                self.transfer()
            self.assertEqual(caught.exception.status, 507)
        self.assertEqual(core.load_chat(self.uid, self.meta["id"])["engine"], "codex")

    def test_api_owner_and_confirmation_revision(self):
        user = {"id": self.uid}
        opts = app.transfer_options(self.meta["id"], user)
        self.assertEqual(opts["targets"][0]["engine"], "claude")
        body = app.ChatTransfer(engine="claude", from_engine=opts["engine"], revision=opts["updated"])
        with self.assertRaises(app.HTTPException):
            asyncio.run(app.transfer_chat(self.meta["id"], body, {"id": self.uid + 1}))
        new = asyncio.run(app.transfer_chat(self.meta["id"], body, user))
        self.assertEqual(new["engine"], "claude")
        with self.assertRaises(app.HTTPException) as caught:
            asyncio.run(app.transfer_chat(self.meta["id"], body, user))
        self.assertEqual(caught.exception.status_code, 409)

    def test_model_edit_invalidates_transfer_confirmation(self):
        before = self.meta["updated"]
        new = asyncio.run(app.change_chat(self.meta["id"], app.ChatChange(title="Другое"), {"id": self.uid}))
        with self.assertRaises(jobs.Refused):
            jobs.transfer(self.uid, new, "claude", "codex", before)

    def test_retry_does_not_cross_transfer_boundary(self):
        new = self.transfer()
        with self.assertRaises(jobs.Refused):
            asyncio.run(jobs.retry(self.uid, new))
        self.assertEqual(core.raw_history(self.uid, new["id"])[-1]["kind"], "done")

    def test_retry_first_target_turn_restores_handoff_only_when_needed(self):
        new = self.transfer()
        core.append_history(self.uid, new["id"], {"kind": "user", "text": "Первый новый вопрос"})
        core.append_history(self.uid, new["id"], {"kind": "text", "text": "Ответ"})
        new["handoff"]["pending"] = False
        with patch.object(jobs, "check", return_value=users.settings(self.uid)), \
                patch.object(core, "rewind"), patch.object(jobs, "start", new_callable=unittest.mock.AsyncMock):
            asyncio.run(jobs.retry(self.uid, new))
            self.assertTrue(new["handoff"]["pending"])
            # Retry record is followed by the replacement first turn and a later turn.
            for text in ["Первый новый вопрос", "Второй новый вопрос"]:
                core.append_history(self.uid, new["id"], {"kind": "user", "text": text})
                core.append_history(self.uid, new["id"], {"kind": "text", "text": "Ответ"})
            new["handoff"]["pending"] = False
            asyncio.run(jobs.retry(self.uid, new))
            self.assertFalse(new["handoff"]["pending"], "later retries must not duplicate the initial context")

    def test_delete_archived_native_sessions(self):
        new = self.transfer()
        project = Path(core.claude_home(self.uid)) / "projects/test"
        project.mkdir(parents=True, exist_ok=True)
        (project / (new["sid"] + ".jsonl")).write_text("local journal")
        (project / "unrelated.jsonl").write_text("keep")
        with patch.object(core, "CODEX_BIN", "codex-test"), \
                patch.object(core.subprocess, "run") as delete:
            core.delete_chat(self.uid, new)
            self.assertTrue(any(call.args[0][-1] == "original-thread" for call in delete.call_args_list))
        self.assertFalse((project / (new["sid"] + ".jsonl")).exists())
        self.assertTrue((project / "unrelated.jsonl").exists())
        self.assertFalse(self.root.exists())


if __name__ == "__main__":
    unittest.main()
