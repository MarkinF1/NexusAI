"""Автоназвание: порядок запуска, изоляция CLI, сбой и ручное переименование."""
import asyncio
import json
import unittest
from unittest.mock import AsyncMock, Mock, patch

import test_core  # изолированные данные и CLI
import app
import core
import jobs
import titles
import users


class Titles(unittest.TestCase):
    uid = 934

    def run_job(self, meta, generate, text="Как настроить сервер?", fail=False, stopped=False):
        async def answer(uid, current, prompt, *args, **kwargs):
            self.assertEqual(current["sid"], meta["sid"])
            if fail:
                raise RuntimeError("main failed")
            yield {"kind": "text", "text": "Ответ"}

        async def go():
            with patch.object(core, "run", answer), patch.object(titles, "generate", generate), \
                    patch.object(jobs.web_push, "notify", AsyncMock()), \
                    patch.object(core, "codex_limits", return_value=[]):
                job = await jobs.start(self.uid, meta, text, users.settings(self.uid))
                job.stopped = stopped
                await job.task
            return core.load_chat(self.uid, meta["id"])
        return asyncio.run(go())

    def test_name_is_saved_before_main_request_for_both_engines(self):
        for engine in core.ENGINES:
            meta = core.create_chat(self.uid, engine, "main-model")
            sid = meta["sid"]

            async def generate(uid, actual_engine, message, attach):
                self.assertEqual(actual_engine, engine)
                self.assertEqual(message, "Как настроить сервер?")
                return "Настройка сервера"

            original_run = core.run

            async def answer(uid, current, *args, **kwargs):
                self.assertEqual(current["title"], "Настройка сервера")
                self.assertEqual(core.load_chat(uid, current["id"])["title"], "Настройка сервера")
                self.assertEqual(current["model"], "main-model")
                self.assertEqual(current["sid"], sid)
                yield {"kind": "text", "text": "Ответ"}

            async def go():
                with patch.object(core, "run", answer), patch.object(titles, "generate", generate), \
                        patch.object(jobs.web_push, "notify", AsyncMock()), \
                        patch.object(core, "codex_limits", return_value=[]):
                    job = await jobs.start(self.uid, meta, "Как настроить сервер?", users.settings(self.uid))
                    await job.task
            asyncio.run(go())
            history = core.history(self.uid, meta["id"])
            self.assertEqual([e["kind"] for e in history], ["user", "title", "text", "done"])
            generate_again = AsyncMock(return_value="Другое название")
            result = self.run_job(meta, generate_again, text="Второй запрос")
            generate_again.assert_not_awaited()
            self.assertEqual(result["title"], "Настройка сервера")
            self.assertIs(core.run, original_run)

    def test_failure_keeps_fallback_and_still_answers(self):
        meta = core.create_chat(self.uid, "claude", "sonnet")
        with self.assertLogs("nexus", level="ERROR"):
            result = self.run_job(meta, AsyncMock(side_effect=TimeoutError()))
        self.assertEqual(result["title"], "Как настроить сервер?")
        history = core.history(self.uid, meta["id"])
        self.assertEqual([e["kind"] for e in history], ["user", "text", "done"])

    def test_manual_name_and_legacy_chats_are_preserved(self):
        for legacy in (False, True):
            meta = core.create_chat(self.uid, "claude", "sonnet")
            if legacy:
                meta.pop("auto_title")
            else:
                meta["title"] = "Мой сервер"
            core.save_chat(self.uid, meta)
            generate = AsyncMock()
            result = self.run_job(meta, generate)
            generate.assert_not_awaited()
            self.assertEqual(result["title"], "Как настроить сервер?" if legacy else "Мой сервер")

    def test_manual_rename_disables_autonaming_even_if_cleared(self):
        meta = core.create_chat(self.uid, "claude", "sonnet")
        with patch.object(users, "available", return_value={}):
            result = asyncio.run(app.change_chat(meta["id"], app.ChatChange(title=""), {"id": self.uid}))
        generate = AsyncMock()
        self.run_job(result, generate)
        generate.assert_not_awaited()

    def test_stop_before_title_does_not_start_either_model(self):
        meta = core.create_chat(self.uid, "claude", "sonnet")
        generate = AsyncMock()
        self.run_job(meta, generate, stopped=True)
        generate.assert_not_awaited()
        self.assertEqual([e["kind"] for e in core.history(self.uid, meta["id"])],
                         ["user", "stopped", "done"])

    def test_clean_title_enforces_limit(self):
        self.assertEqual(titles.clean_title('  «Настройка\n сервера». '), 'Настройка сервера')
        self.assertEqual(titles.clean_title('"Настройка сервера"'), "Настройка сервера")
        self.assertLessEqual(len(titles.clean_title("длинное название " * 50)), 60)

    def test_cli_uses_light_models_and_no_existing_session(self):
        for engine in core.ENGINES:
            record = ({"type": "item.completed", "item": {"type": "agent_message", "text": "Настройка"}}
                      if engine == "codex" else {"type": "result", "result": "Настройка", "usage": {"output_tokens": 4}})
            proc = Mock(returncode=0, wait=AsyncMock())
            proc.communicate = AsyncMock(return_value=(json.dumps(record).encode(), b""))
            spawn, attached = AsyncMock(return_value=proc), Mock()
            with patch.object(core.asyncio, "create_subprocess_exec", spawn), \
                    patch.object(titles.usage, "charge") as charge:
                title = asyncio.run(titles.generate(self.uid, engine, "Запрос", attached))
            self.assertEqual(title, "Настройка")
            args = spawn.call_args.args
            self.assertIn(titles.MODELS[engine], args)
            self.assertNotIn("--resume", args)
            self.assertIn("--ephemeral" if engine == "codex" else "--no-session-persistence", args)
            self.assertIn("features.shell_tool=false" if engine == "codex" else "--safe-mode", args)
            attached.assert_called_once_with(proc)
            payload = proc.communicate.call_args.args[0].decode()
            self.assertIn("60", payload)
            self.assertIn("Запрос", payload)
            self.assertNotIn("MEMORY.md", payload)
            charge.assert_called_once()

    def test_cli_timeout_kills_process(self):
        proc = Mock(returncode=None, wait=AsyncMock())
        proc.communicate = AsyncMock(side_effect=TimeoutError())
        with patch.object(core.asyncio, "create_subprocess_exec", AsyncMock(return_value=proc)), \
                patch.object(core, "stop") as stop, patch.object(titles.usage, "charge"):
            with self.assertRaises(TimeoutError):
                asyncio.run(titles.generate(self.uid, "claude", "Запрос", Mock()))
        stop.assert_called_once_with(proc)
        proc.wait.assert_awaited_once()
