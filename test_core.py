"""python -m unittest test_core — без сети и без настоящих CLI."""
import asyncio
import contextlib
import json
import os
import pathlib
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
import unittest.mock

TMP = tempfile.mkdtemp()
FAKE = os.path.join(TMP, "fake-claude")
with open(FAKE, "w") as f:
    # Печатает stream-json как claude -p; FAKE_MODE=hang зависает после первой строки.
    f.write(f"""#!{sys.executable}
import json, os, sys, time
data = sys.stdin.read()
print(json.dumps({{"type": "system", "subtype": "init"}}), flush=True)
if '"text": "/compact"' in data:
    print(json.dumps({{"type": "system", "subtype": "compact_boundary",
                      "compact_metadata": {{"trigger": "manual", "pre_tokens": 13500}}}}))
    print(json.dumps({{"type": "result", "is_error": False, "result": ""}}))
    sys.exit()
print(json.dumps({{"type": "assistant", "message": {{"content": [
    {{"type": "tool_use", "name": "Bash", "input": {{"command": "ls files"}}}}]}}}}), flush=True)
if os.environ.get("FAKE_MODE") == "hang":
    time.sleep(60)
print(json.dumps({{"type": "assistant", "message": {{"content": [{{"type": "text", "text": "Привет"}}]}}}}))
print(json.dumps({{"type": "rate_limit_event", "rate_limit_info": {{"unifiedWindows": {{
    "five_hour": {{"utilization": 0.12, "resetsAt": 4102444800}},
    "seven_day": {{"utilization": 0.5, "resetsAt": 4103049600}}}}}}}}))
print(json.dumps({{"type": "result", "is_error": False, "result": "Привет",
                  "usage": {{"input_tokens": 3, "cache_read_input_tokens": 7, "output_tokens": 5}}}}))
print(" ".join(sys.argv[1:]), file=sys.stderr)
""")
os.chmod(FAKE, 0o755)
os.environ.update(NEXUS_DATA=os.path.join(TMP, "data"), CLAUDE_BIN=FAKE)

import app  # noqa: E402
import core  # noqa: E402
import jobs  # noqa: E402
import usage  # noqa: E402
import settings  # noqa: E402
import users  # noqa: E402

ACCESS = users.clean({})["access"]
ALL = ACCESS["claude"]
from fastapi import HTTPException  # noqa: E402


async def collect(gen):
    return [e async for e in gen]


class Users(unittest.TestCase):
    def test_password(self):
        uid = users.add("work", "secret")
        self.assertEqual(users.check("work", "secret")["id"], uid)
        self.assertIsNone(users.check("work", "wrong"))
        token = users.login(uid)
        self.assertEqual(users.by_token(token)["name"], "work")
        users.set_password(uid, "new")
        self.assertIsNone(users.by_token(token))  # смена пароля выкидывает входы


class Lockout(unittest.TestCase):
    def request(self):
        return unittest.mock.Mock(url=unittest.mock.Mock(scheme="http"), headers={})

    def test_failures_survive_restart(self):
        users.add("locky", "right")
        credentials = app.Credentials(name="locky", password="wrong")
        with unittest.mock.patch("time.sleep"):
            for _ in range(users.LOCK_AFTER):
                with self.assertRaises(HTTPException) as err:
                    app.login(credentials, self.request(), app.Response())
                self.assertEqual(err.exception.status_code, 401)
            # Счётчик в базе, а не в памяти процесса: верный пароль тоже не пускает.
            with self.assertRaises(HTTPException) as err:
                app.login(app.Credentials(name="locky", password="right"), self.request(), app.Response())
            self.assertEqual(err.exception.status_code, 429)
            with users.db() as conn:
                self.assertEqual(conn.execute("SELECT count FROM login_failures WHERE name = 'locky'").fetchone()[0],
                                 users.LOCK_AFTER)
                # Прошло 15 минут: вход открыт, прежние неудачи не считаются.
                conn.execute("UPDATE login_failures SET last = last - ?", (users.LOCK_FOR + 1,))
            self.assertFalse(users.locked("locky"))
            with self.assertRaises(HTTPException):
                app.login(credentials, self.request(), app.Response())
            self.assertFalse(users.locked("locky"))
            self.assertEqual(app.login(app.Credentials(name="locky", password="right"),
                                       self.request(), app.Response())["name"], "locky")
            self.assertFalse(users.locked("locky"))
        with users.db() as conn:
            self.assertIsNone(conn.execute("SELECT 1 FROM login_failures WHERE name = 'locky'").fetchone())

    def test_tg_codes(self):
        uid = users.add("coder", "pw")
        code = users.new_tg_code(uid, 600)
        with users.db() as conn:
            self.assertIsNone(conn.execute("SELECT 1 FROM tg_codes WHERE code = ?", (code,)).fetchone())  # только хэш
        self.assertEqual(users.take_tg_code(code), uid)
        self.assertIsNone(users.take_tg_code(code))  # одноразовый
        self.assertIsNone(users.take_tg_code(users.new_tg_code(uid, -1)))  # просрочен
        other = users.new_tg_code(uid, 600)
        users.delete(uid)
        self.assertIsNone(users.take_tg_code(other))  # пользователя удалили — код пропал


class Events(unittest.TestCase):
    def test_claude(self):
        rec = {"type": "assistant", "message": {"content": [
            {"type": "text", "text": "a"},
            {"type": "tool_use", "name": "Read", "input": {"file_path": "/x/y"}}]}}
        self.assertEqual(list(core.claude_events(rec)), [
            {"kind": "text", "text": "a"}, {"kind": "tool", "name": "Read", "input": "/x/y"}])

    def test_codex(self):
        items = [{"type": "agent_message", "text": "ok"},
                 {"type": "command_execution", "command": "ls"},
                 {"type": "file_change", "changes": [{"path": "a.txt", "kind": "add"}]},
                 {"type": "reasoning", "text": "hidden"}]
        events = [e for i in items for e in core.codex_events({"type": "item.completed", "item": i})]
        self.assertEqual([e["kind"] for e in events], ["text", "tool", "tool"])
        self.assertEqual(events[2]["input"], "a.txt")


class Run(unittest.TestCase):
    def setUp(self):
        self.meta = core.create_chat(1, "claude", "sonnet")

    def test_stream(self):
        events = asyncio.run(collect(core.run(1, self.meta, "hi", ALL)))
        self.assertEqual([e for e in events if e["kind"] in ("tool", "text")],
                         [{"kind": "tool", "name": "Bash", "input": "ls files"},
                          {"kind": "text", "text": "Привет"}])
        self.assertIn({"kind": "limits", "window": "five_hour", "percent": 12.0,
                       "resets_at": 4102444800}, events)
        self.assertIn({"kind": "tokens", "in": 10, "out": 5}, events)

    def test_stop(self):
        os.environ["FAKE_MODE"] = "hang"
        try:
            async def go():
                procs = []
                gen = core.run(1, self.meta, "hi", ALL, on_proc=procs.append)
                first = await anext(gen)
                core.stop(procs[0])
                with self.assertRaises(RuntimeError):
                    await anext(gen)
                return first

            started = time.monotonic()
            self.assertEqual(asyncio.run(go())["kind"], "tool")
            self.assertLess(time.monotonic() - started, 10)
        finally:
            del os.environ["FAKE_MODE"]

    def test_files_move_to_general(self):
        cwd = core.workspace(1, self.meta["id"])
        with open(os.path.join(cwd, "files", "for_user", "report.txt"), "w") as f:
            f.write("x")
        self.assertEqual(core.finish(1, self.meta["id"], True), ["files/general/report.txt"])


class Library(unittest.TestCase):
    def put(self, cwd, folder, name, body=b"x"):
        with open(os.path.join(cwd, "files", folder, name), "wb") as f:
            f.write(body)

    def test_shelve_and_delete(self):
        a, b = core.create_chat(30, "claude", "sonnet"), core.create_chat(30, "claude", "sonnet")
        cwd_a, cwd_b = core.workspace(30, a["id"]), core.workspace(30, b["id"])
        lib = core.library(30)
        self.assertIn("library/", open(core.memory_file(30), encoding="utf-8").read())
        self.put(cwd_a, "from_user", "doc.txt", b"a" * 200_000)
        self.put(cwd_a, "for_user", "report.txt")
        self.assertEqual(core.finish(30, a["id"], True), ["files/general/report.txt"])
        self.put(cwd_b, "from_user", "doc.txt", b"b")
        core.finish(30, b["id"], True)
        names = sorted(os.listdir(lib))
        self.assertEqual(len(names), 3)  # doc.txt, report.txt и doc_<суффикс>.txt
        self.assertIn("doc.txt", names)
        general = os.path.join(cwd_a, "files", "general", "doc.txt")
        self.assertTrue(os.path.samefile(general, os.path.join(lib, "doc.txt")))
        # Одно место на диске, хотя имён два.
        self.assertLess(usage.disk(30)["files"], 300_000)
        core.finish(30, a["id"], True)  # повторный проход ничего не дублирует
        self.assertEqual(len(os.listdir(lib)), 3)
        core.delete_chat(30, a)
        self.assertEqual(len(os.listdir(lib)), 1)  # остался только файл беседы b
        self.assertTrue(os.listdir(lib)[0].startswith("doc"))

    def test_overwrite_replaces_library_version(self):
        chat = core.create_chat(32, "claude", "sonnet")["id"]
        cwd, lib = core.workspace(32, chat), core.library(32)
        self.put(cwd, "for_user", "report.txt", b"v1")
        core.finish(32, chat, True)
        # Модель перезаписала файл целиком: новый файл под старым именем, связь разорвана.
        general = os.path.join(cwd, "files", "general", "report.txt")
        with open(general + ".new", "wb") as f:
            f.write(b"v2")
        os.replace(general + ".new", general)
        core.finish(32, chat, True)
        self.assertEqual(os.listdir(lib), ["report.txt"])
        self.assertTrue(os.path.samefile(general, os.path.join(lib, "report.txt")))
        with open(os.path.join(lib, "report.txt"), "rb") as f:
            self.assertEqual(f.read(), b"v2")
        # Удалённый из беседы файл забывается: новый с тем же именем — уже другой файл.
        os.remove(general)
        core.finish(32, chat, True)
        self.put(cwd, "for_user", "report.txt", b"v3")
        core.finish(32, chat, True)
        self.assertEqual(len(os.listdir(lib)), 2)

    def test_delete_keeps_files(self):
        owner = {"id": 33, "is_admin": False}
        meta = core.create_chat(33, "claude", "sonnet")
        cwd, lib = core.workspace(33, meta["id"]), core.library(33)
        self.put(cwd, "for_user", "done.txt")
        core.finish(33, meta["id"], True)
        self.put(cwd, "from_user", "pending.txt")  # ответа на него ещё не было
        asyncio.run(app.delete_chat(meta["id"], False, owner))
        self.assertEqual(sorted(os.listdir(lib)), ["done.txt", "pending.txt"])
        self.assertFalse(os.path.exists(cwd))
        self.assertFalse(os.path.exists(core.chat_file(33, meta["id"], ".links.json")))
        other = core.create_chat(33, "claude", "sonnet")
        self.put(core.workspace(33, other["id"]), "for_user", "gone.txt")
        core.finish(33, other["id"], True)
        asyncio.run(app.delete_chat(other["id"], True, owner))
        self.assertEqual(sorted(os.listdir(lib)), ["done.txt", "pending.txt"])

    def test_library_api(self):
        owner = {"id": 34, "is_admin": False}
        meta = core.create_chat(34, "claude", "sonnet")
        core.save_chat(34, {**meta, "title": "Отчёты"})
        self.put(core.workspace(34, meta["id"]), "for_user", "report.html", b"<script>alert(1)</script>")
        self.put(core.workspace(34, meta["id"]), "for_user", ".env", b"TOKEN=")
        core.finish(34, meta["id"], True)
        os.symlink("/etc/passwd", os.path.join(core.library(34), "passwd"))  # подсунутая моделью ссылка
        items = app.library(owner)
        self.assertEqual(sorted((i["path"], i["size"]) for i in items), [(".env", 6), ("report.html", 25)])
        self.assertEqual(items[0]["chats"], [{"id": meta["id"], "title": "Отчёты"}])
        view = app.library_file("report.html", False, owner)

        async def call_next(_):
            return view
        # Общий CSP сайта не перекрывает sandbox ответа.
        self.assertEqual(asyncio.run(app.security_headers(None, call_next)).headers["content-security-policy"], "sandbox")
        self.assertTrue(view.headers["content-disposition"].startswith("inline"))
        self.assertTrue(app.library_file("report.html", True, owner).headers["content-disposition"].startswith("attachment"))
        for bad in ("passwd", "../MEMORY.md", "../../chats", "/etc/passwd"):
            with self.assertRaises(HTTPException, msg=bad):
                app.library_file(bad, False, owner)
        self.assertEqual(app.library({"id": 35, "is_admin": False}), [])  # чужая библиотека пуста

    def test_hardlinks_on_site(self):
        owner = {"id": 31, "is_admin": False}
        chat = core.create_chat(31, "claude", "sonnet")["id"]
        cwd = core.workspace(31, chat)
        self.put(cwd, "for_user", "ok.txt")
        core.finish(31, chat, True)
        self.assertTrue(app.get_file(chat, "files/general/ok.txt", owner))
        ok = os.path.join(cwd, "files", "general", "ok.txt")
        self.assertEqual(app.list_files(chat, owner),
                         [{"path": "files/general/ok.txt", "size": os.path.getsize(ok), "mtime": os.path.getmtime(ok)}])
        # Чужая жёсткая ссылка (не из библиотеки) не отдаётся и не показывается.
        outside = os.path.join(TMP, "secret")
        with open(outside, "w") as f:
            f.write("s")
        os.link(outside, os.path.join(cwd, "files", "general", "secret"))
        with self.assertRaises(HTTPException):
            app.get_file(chat, "files/general/secret", owner)
        ok = os.path.join(cwd, "files", "general", "ok.txt")
        self.assertEqual(app.list_files(chat, owner),
                         [{"path": "files/general/ok.txt", "size": os.path.getsize(ok), "mtime": os.path.getmtime(ok)}])


    def test_files_newest_first(self):
        owner = {"id": 38, "is_admin": False}
        chat = core.create_chat(38, "claude", "sonnet")["id"]
        cwd = core.workspace(38, chat)
        for i, (folder, name) in enumerate([("from_user", "b-old.txt"), ("general", "z-mid.txt"), ("for_user", "a-new.txt")]):
            self.put(cwd, folder, name)
            os.utime(os.path.join(cwd, "files", folder, name), (1000 + i, 1000 + i))
        self.assertEqual([f["path"] for f in app.list_files(chat, owner)],
                         ["files/for_user/a-new.txt", "files/general/z-mid.txt", "files/from_user/b-old.txt"])

    def test_chat_size(self):
        owner = {"id": 39, "is_admin": False}
        meta = core.create_chat(39, "claude", "sonnet")
        self.assertEqual(app.chat_size(meta["id"], owner)["session"], 0)  # сессии ещё нет
        meta = core.save_chat(39, {**meta, "sid": "11111111-2222-3333-4444-555555555555"})
        project = os.path.join(core.claude_home(39), "projects", "-tmp-x")
        os.makedirs(os.path.join(project, meta["sid"], "subagents"))
        with open(os.path.join(project, meta["sid"] + ".jsonl"), "wb") as f:
            f.write(b"x" * 1000)
        with open(os.path.join(project, meta["sid"], "subagents", "a.jsonl"), "wb") as f:
            f.write(b"x" * 200)
        self.put(core.workspace(39, meta["id"]), "general", "f.txt", b"y" * 50)
        size = app.chat_size(meta["id"], owner)
        self.assertEqual((size["session"], size["files"], size["voice"]), (1200, 50, 0))
        self.assertGreater(size["history"], 0)
        with self.assertRaises(HTTPException):
            app.chat_size(meta["id"], {"id": 40, "is_admin": False})

    def test_compact(self):
        owner = {"id": 43, "is_admin": False}
        meta = core.save_chat(43, {**core.create_chat(43, "claude", "sonnet"), "sid": "22222222-2222-3333-4444-555555555555"})
        with self.assertRaises(HTTPException):  # сессии ещё нет — сжимать нечего
            asyncio.run(app.compact(meta["id"], owner))
        project = os.path.join(core.claude_home(43), "projects", "-tmp-y")
        os.makedirs(project)
        lines = [b'{"type":"user","n":1}\n' * 50, b'{"type":"system","subtype":"compact_boundary"}\n',
                 b'{"type":"user","after":true}\n']
        with open(os.path.join(project, meta["sid"] + ".jsonl"), "wb") as f:
            f.write(b"".join(lines))
        size = app.chat_size(meta["id"], owner)
        self.assertEqual(size["context"], len(lines[2]))  # перечитывается только часть после сжатия
        self.assertEqual(size["session"], sum(map(len, lines)))
        self.assertTrue(size["compactable"])
        self.assertEqual(size["limit"], settings.get(settings.COMPACT_MB.name) * 2 ** 20)  # порог по умолчанию — общая настройка
        with unittest.mock.patch("jobs.check", return_value={}), \
                unittest.mock.patch("jobs.start", unittest.mock.AsyncMock()) as start:
            asyncio.run(app.compact(meta["id"], owner))
        self.assertEqual(start.await_args.args[2], "/compact")
        self.assertEqual(start.await_args.kwargs["shown"]["kind"], "compact")
        codex = core.create_chat(43, "codex", "gpt")
        with self.assertRaises(HTTPException):  # у codex сессии ещё нет — сжимать нечего
            asyncio.run(app.compact(codex["id"], owner))
        self.assertFalse(app.chat_size(codex["id"], owner)["compactable"])
        events = list(core.claude_events({"type": "system", "subtype": "compact_boundary",
                                          "compact_metadata": {"trigger": "manual", "pre_tokens": 1842}}))
        self.assertEqual(events, [{"kind": "compacted", "tokens": 1842, "auto": False}])
        self.assertEqual(users.clean({"compact_mb": 12})["compact_mb"], 12)
        self.assertEqual(users.clean({"compact_mb": 0})["compact_mb"], 0)  # явный 0 — не предлагать
        self.assertEqual(users.clean({})["compact_mb"], settings.get(settings.COMPACT_MB.name))
        with self.assertRaises(ValueError):
            users.clean({"compact_mb": -1})

    def test_delete_files(self):
        owner = {"id": 36, "is_admin": False}
        a = core.create_chat(36, "claude", "sonnet")["id"]
        b = core.create_chat(36, "claude", "sonnet")["id"]
        wa, wb, lib = core.workspace(36, a), core.workspace(36, b), core.library(36)
        self.put(wa, "for_user", "shared.txt")
        core.finish(36, a, True)
        os.link(os.path.join(lib, "shared.txt"), os.path.join(wb, "files", "general", "shared.txt"))
        core.shelve(36, b)
        self.put(wa, "from_user", "pending.txt")
        # Из беседы: файл уходит, в библиотеке и в другой беседе остаётся.
        app.delete_file(a, "files/general/shared.txt", owner)
        self.assertFalse(os.path.exists(os.path.join(wa, "files", "general", "shared.txt")))
        self.assertNotIn("shared.txt", core.links(36, a))
        self.assertTrue(os.path.exists(os.path.join(lib, "shared.txt")))
        app.delete_file(a, "files/from_user/pending.txt", owner)
        self.assertEqual(app.list_files(a, owner), [])
        # Наружу из files/ не выйти; ссылки и чужие жёсткие ссылки не трогаются.
        outside = os.path.join(TMP, "keep-36")
        with open(outside, "w") as f:
            f.write("k")
        os.link(outside, os.path.join(wa, "files", "general", "foreign"))
        os.symlink(outside, os.path.join(wa, "files", "general", "link"))
        for bad in ("files/general/foreign", "files/../MEMORY.md", "files", "files/general/..", "/etc/passwd",
                    "files/general/link/x", "../../chats/" + a + ".json"):
            with self.assertRaises(HTTPException, msg=bad):
                app.delete_file(a, bad, owner)
        self.assertTrue(os.path.exists(outside))
        with self.assertRaises(HTTPException):  # симлинк в списке не показывается и не удаляется
            app.delete_file(a, "files/general/link", owner)
        self.assertTrue(os.path.exists(outside))
        # Пока модель отвечает в беседе с файлом, из библиотеки его не удалить.
        jobs.jobs[(36, b)] = object()
        try:
            with self.assertRaises(HTTPException):
                app.delete_library_file("shared.txt", owner)
            with self.assertRaises(HTTPException):
                app.delete_file(b, "files/general/shared.txt", owner)
        finally:
            del jobs.jobs[(36, b)]
        # Из библиотеки: файл пропадает и из всех бесед.
        app.delete_library_file("shared.txt", owner)
        self.assertFalse(os.path.exists(os.path.join(lib, "shared.txt")))
        self.assertFalse(os.path.exists(os.path.join(wb, "files", "general", "shared.txt")))
        self.assertEqual(core.links(36, b), {})
        for bad in ("shared.txt", "../MEMORY.md", "", "..", "/etc/passwd"):
            with self.assertRaises(HTTPException, msg=bad):
                app.delete_library_file(bad, owner)
        with self.assertRaises(HTTPException):
            app.delete_file(a, "files/general/x", {"id": 37, "is_admin": False})  # чужая беседа


class LibraryBatch(unittest.TestCase):
    def test_delete_many(self):
        owner = {"id": 38, "is_admin": False}
        a, b = core.create_chat(38, "claude", "sonnet"), core.create_chat(38, "claude", "sonnet")
        for meta, names in ((a, ("a1.txt", "a2.txt", "keep.txt")), (b, ("b1.txt",))):
            for name in names:
                with open(os.path.join(core.workspace(38, meta["id"]), "files", "for_user", name), "w") as f:
                    f.write(name)
            core.finish(38, meta["id"], True)
        lib = core.library(38)
        jobs.jobs[(38, b["id"])] = object()  # беседа b отвечает — ничего не удаляется
        try:
            with self.assertRaises(HTTPException) as err:
                app.delete_library_files(app.FilePaths(paths=["a1.txt", "b1.txt"]), owner)
            self.assertEqual(err.exception.status_code, 409)
            self.assertEqual(sorted(os.listdir(lib)), ["a1.txt", "a2.txt", "b1.txt", "keep.txt"])
        finally:
            del jobs.jobs[(38, b["id"])]
        result = app.delete_library_files(app.FilePaths(paths=["a1.txt", "b1.txt", "a1.txt", "gone.txt", "../MEMORY.md"]), owner)
        self.assertEqual(sorted(result["deleted"]), ["a1.txt", "b1.txt"])
        self.assertEqual(sorted(os.listdir(lib)), ["a2.txt", "keep.txt"])
        self.assertEqual(sorted(os.listdir(os.path.join(core.workspace(38, a["id"]), "files", "general"))), ["a2.txt", "keep.txt"])
        self.assertEqual(os.listdir(os.path.join(core.workspace(38, b["id"]), "files", "general")), [])
        self.assertEqual(sorted(core.links(38, a["id"]).values()), ["a2.txt", "keep.txt"])
        self.assertTrue(os.path.exists(core.memory_file(38)))
        with self.assertRaises(HTTPException):
            app.delete_library_files(app.FilePaths(paths=[]), owner)


class ChatFilesBatch(unittest.TestCase):
    def test_delete_many(self):
        owner = {"id": 39, "is_admin": False}
        meta = core.create_chat(39, "claude", "sonnet")
        cwd = core.workspace(39, meta["id"])
        for name in ("old.txt", "keep.txt"):
            with open(os.path.join(cwd, "files", "for_user", name), "w") as f:
                f.write(name)
        core.finish(39, meta["id"], True)  # в general и в библиотеке
        with open(os.path.join(cwd, "files", "from_user", "new.jpg"), "w") as f:
            f.write("x")
        os.link(core.memory_file(39), os.path.join(cwd, "files", "from_user", "alien"))  # чужая жёсткая ссылка
        result = app.delete_files(meta["id"], app.FilePaths(paths=[
            "files/general/old.txt", "files/from_user/new.jpg", "files/from_user/alien", "../MEMORY.md", "files/none"]), owner)
        self.assertEqual(result["deleted"], ["files/general/old.txt", "files/from_user/new.jpg"])
        self.assertEqual(os.listdir(os.path.join(cwd, "files", "general")), ["keep.txt"])
        self.assertEqual(sorted(os.listdir(core.library(39))), ["keep.txt", "old.txt"])  # библиотека не тронута
        self.assertEqual(list(core.links(39, meta["id"]).values()), ["keep.txt"])
        jobs.jobs[(39, meta["id"])] = object()
        try:
            with self.assertRaises(HTTPException) as err:
                app.delete_files(meta["id"], app.FilePaths(paths=["files/general/keep.txt"]), owner)
            self.assertEqual(err.exception.status_code, 409)
        finally:
            del jobs.jobs[(39, meta["id"])]
        with self.assertRaises(HTTPException):  # чужая беседа
            app.delete_files(meta["id"], app.FilePaths(paths=["files/general/keep.txt"]), {"id": 40, "is_admin": False})


class Thumbnails(unittest.TestCase):
    def photo(self, path, size=(3000, 2000), orientation=None):
        from PIL import Image
        image = Image.new("RGB", size, (200, 30, 30))
        exif = Image.Exif()
        if orientation:
            exif[0x0112] = orientation  # повёрнутое фото с телефона
        image.save(path, "JPEG", quality=95, exif=exif)

    def test_chat_and_library(self):
        from PIL import Image
        import thumbs
        owner = {"id": 36, "is_admin": False}
        meta = core.create_chat(36, "claude", "sonnet")
        cwd = core.workspace(36, meta["id"])
        self.photo(os.path.join(cwd, "files", "for_user", "photo.jpg"), orientation=6)
        with open(os.path.join(cwd, "files", "for_user", "notes.txt"), "w") as f:
            f.write("текст")
        response = app.chat_thumb(meta["id"], "files/for_user/photo.jpg", owner)
        self.assertEqual(response.media_type, "image/webp")
        self.assertIn("immutable", response.headers["cache-control"])
        with Image.open(response.path) as image:
            self.assertEqual(image.size, (240, 360))  # уменьшено и повёрнуто по EXIF
        self.assertLess(os.path.getsize(response.path), 20_000)
        with self.assertRaises(HTTPException) as err:
            app.chat_thumb(meta["id"], "files/for_user/notes.txt", owner)
        self.assertEqual(err.exception.status_code, 415)
        with self.assertRaises(HTTPException):
            app.chat_thumb(meta["id"], "../../chats", owner)
        with self.assertRaises(HTTPException):  # чужая беседа
            app.chat_thumb(meta["id"], "files/for_user/photo.jpg", {"id": 37, "is_admin": False})
        # После ответа файл в библиотеке — та же миниатюра (одна на жёсткие ссылки).
        core.finish(36, meta["id"], True)
        same = app.library_thumb("photo.jpg", owner)
        self.assertEqual(same.path, app.chat_thumb(meta["id"], "files/general/photo.jpg", owner).path)
        self.assertEqual(same.path, response.path)
        # Файл заменили: новая миниатюра, старая удалена.
        time.sleep(0.01)
        self.photo(os.path.join(core.library(36), "photo.jpg"), size=(500, 1000))
        fresh = app.library_thumb("photo.jpg", owner)
        self.assertNotEqual(fresh.path, response.path)
        self.assertFalse(os.path.exists(response.path))
        with Image.open(fresh.path) as image:
            self.assertEqual(image.size, (180, 360))
        broken = os.path.join(core.library(36), "broken.png")
        with open(broken, "wb") as f:
            f.write(b"not a png")
        with self.assertRaises(HTTPException) as err:
            app.library_thumb("broken.png", owner)
        self.assertEqual(err.exception.status_code, 415)
        folder = pathlib.Path(core.user_dir(36)) / "thumbs"
        self.assertEqual([p.suffix for p in folder.iterdir()], [".webp"])  # без недописанных .tmp
        # Уборка: давно не открытые миниатюры уходят.
        old = time.time() - 40 * 86400
        os.utime(fresh.path, (old, old))
        thumbs.swept.clear()
        thumbs.sweep(folder)
        self.assertEqual(list(folder.iterdir()), [])

    def test_large_and_office(self):
        from PIL import Image
        import thumbs
        owner = {"id": 38, "is_admin": False}
        library = core.library(38)
        self.photo(os.path.join(library, "big.jpg"))
        small = app.library_thumb("big.jpg", owner)
        large = app.library_thumb("big.jpg", owner, size=thumbs.LARGE)
        with Image.open(large.path) as image:
            self.assertEqual(image.size, (1600, 1067))  # окно просмотра: крупнее плитки
        self.assertTrue(os.path.exists(small.path))  # оба размера живут рядом
        with self.assertRaises(HTTPException) as err:
            app.library_thumb("big.jpg", owner, size=5000)  # произвольный размер не делаем
        self.assertEqual(err.exception.status_code, 415)
        with open(os.path.join(library, "notes.txt"), "w") as f:
            f.write("текст")
        with self.assertRaises(HTTPException) as err:
            app.library_preview("notes.txt", owner)
        self.assertEqual(err.exception.status_code, 415)
        if not thumbs.SOFFICE:
            self.skipTest("нет LibreOffice")
        with open(os.path.join(library, "doc.rtf"), "w") as f:
            f.write(r"{\rtf1\ansi Hello from NexusAI\par}")
        pdf = app.library_preview("doc.rtf", owner)
        self.assertEqual(pdf.media_type, "application/pdf")
        with open(pdf.path, "rb") as f:
            self.assertEqual(f.read(5), b"%PDF-")
        self.assertEqual(app.library_preview("doc.rtf", owner).path, pdf.path)  # второй раз из кэша


class Voice(unittest.TestCase):
    def upload(self, body=b"OggS-voice", mime="audio/webm;codecs=opus"):
        import io
        from starlette.datastructures import Headers, UploadFile
        return UploadFile(io.BytesIO(body), size=len(body), filename="voice.webm", headers=Headers({"content-type": mime}))

    def test_voice_flow(self):
        owner = {"id": 40, "is_admin": False}
        meta = core.create_chat(40, "claude", "sonnet")
        chat = meta["id"]
        with unittest.mock.patch("stt.transcribe", unittest.mock.AsyncMock(return_value="Привет, проверь логи")):
            out = asyncio.run(app.voice(chat, self.upload(), 3.456, "[0.1, 2, -1, \"x\"]", owner))
        voice, transcript = out["events"]
        self.assertEqual((voice["kind"], voice["ext"], voice["duration"]), ("voice", "webm", 3.46))
        self.assertEqual(voice["peaks"], [])  # мусор в уровнях — дорожка без них, а не ошибка
        self.assertEqual(transcript, {"kind": "transcript", "voice": voice["id"], "text": "Привет, проверь логи"})
        self.assertEqual([e["kind"] for e in core.history(40, chat)], ["voice", "transcript"])
        self.assertEqual(app.voice_file(chat, voice["id"], owner).media_type, "audio/webm")
        for bad in ("../x", "0" * 31, "g" * 32, "0" * 32):
            with self.assertRaises(HTTPException, msg=bad):
                app.voice_file(chat, bad, owner)
        with self.assertRaises(HTTPException):
            app.voice_file(chat, voice["id"], {"id": 41, "is_admin": False})  # чужая беседа
        # «Нет» — кнопки убираются; «да» — ответ и реплика встают в журнал подряд.
        app.voice_decline(chat, voice["id"], owner)
        with unittest.mock.patch("jobs.check", return_value={}), \
                unittest.mock.patch("jobs.start", unittest.mock.AsyncMock()) as start:
            asyncio.run(app.send(chat, "Привет, проверь логи", [], owner, voice=voice["id"]))
        start.assert_awaited_once()
        self.assertEqual(core.history(40, chat)[-2:], [
            {"kind": "voice_answer", "voice": voice["id"], "ok": False},
            {"kind": "voice_answer", "voice": voice["id"], "ok": True}])
        folder = core.voice_dir(40, chat)
        self.assertTrue(os.listdir(folder))
        core.delete_chat(40, core.load_chat(40, chat))
        self.assertFalse(os.path.exists(folder))

    def test_voice_errors(self):
        owner = {"id": 42, "is_admin": False}
        chat = core.create_chat(42, "claude", "sonnet")["id"]
        with unittest.mock.patch("stt.transcribe", unittest.mock.AsyncMock(return_value="")):
            out = asyncio.run(app.voice(chat, self.upload(mime="audio/mp4"), 1.0, "[0.5]", owner))
        self.assertEqual(out["events"][0]["ext"], "m4a")
        self.assertEqual(out["events"][1]["kind"], "warning")
        with unittest.mock.patch("stt.transcribe", unittest.mock.AsyncMock(side_effect=RuntimeError("нет модели"))):
            out = asyncio.run(app.voice(chat, self.upload(), 1.0, "", owner))
        self.assertIn("нет модели", out["events"][1]["text"])
        self.assertNotIn((42, chat), jobs.transcribing)  # счётчик сброшен и после ошибки
        for duration in (0, -1, app.MAX_VOICE_SECONDS + 1):
            with self.assertRaises(HTTPException):
                asyncio.run(app.voice(chat, self.upload(), duration, "", owner))
        with self.assertRaises(HTTPException):
            asyncio.run(app.voice(chat, self.upload(b""), 1.0, "", owner))
        with unittest.mock.patch("app.MAX_VOICE", 4), self.assertRaises(HTTPException):
            asyncio.run(app.voice(chat, self.upload(b"12345"), 1.0, "", owner))
        # Пока идёт расшифровка, беседа занята: ни реплики, ни второго голосового.
        jobs.transcribing[(42, chat)] += 1
        try:
            with self.assertRaises(HTTPException):
                app.voice_decline(chat, "0" * 32, owner)
            with self.assertRaises(HTTPException):
                asyncio.run(app.voice(chat, self.upload(), 1.0, "", owner))
        finally:
            del jobs.transcribing[(42, chat)]
        self.assertFalse(app.get_chat(chat, owner)["transcribing"])

class GeneralSettings(unittest.TestCase):
    """settings.py: settings.json поверх .env, get/set, проверка значений и форма админа."""

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.file = os.path.join(self.dir, "settings.json")
        patch = unittest.mock.patch.object(settings, "path", lambda: self.file)
        patch.start()
        self.addCleanup(patch.stop)
        self.addCleanup(settings.load)  # остальным тестам — прежние настройки

    def env(self, file=None, **values):
        """Окружение только с values; file — содержимое .env (по умолчанию файла нет)."""
        clean = {k: v for k, v in os.environ.items() if not k.startswith("NEXUS_") or k == "NEXUS_DATA"}
        stack = contextlib.ExitStack()
        stack.enter_context(unittest.mock.patch.dict(os.environ, {**clean, **values}, clear=True))
        parsed = {}
        for line in (file or "").splitlines():
            key, _, value = line.partition("=")
            parsed[key] = value
        stack.enter_context(unittest.mock.patch.object(settings, "dotenv", lambda: parsed))
        return stack

    def stored(self):
        with open(self.file) as f:
            return json.load(f)

    def test_first_start_from_env(self):
        with self.env(NEXUS_TIMEOUT="600", NEXUS_TG_STT_MODEL="base", NEXUS_COMPACT_MB="abc"):
            settings.load()
        self.assertEqual(settings.get(settings.TIMEOUT.name), 600)  # из .env
        self.assertEqual(settings.get(settings.STT_MODEL.name), "base")  # старое имя переменной тоже понимаем
        self.assertEqual(settings.get(settings.COMPACT_MB.name), 10)  # кривое значение в .env — по умолчанию
        self.assertEqual(settings.get(settings.CLAUDE_MODEL.name), "sonnet")  # нет в .env — по умолчанию
        self.assertEqual(set(self.stored()), {p.name for p in settings.PARAMETERS})  # файл создан со всеми параметрами
        self.assertEqual(oct(os.stat(self.file).st_mode & 0o777), "0o600")  # внутри токен бота

    def test_dotenv_file(self):
        """Первый запуск из консоли (users.py): окружения нет — берём сам файл .env."""
        with self.env(file="NEXUS_TG_TOKEN=123:abc\nNEXUS_TIMEOUT=77", NEXUS_TIMEOUT="66"):
            settings.load()
        self.assertEqual(settings.get(settings.TG_TOKEN.name), "123:abc")
        self.assertEqual(settings.get(settings.TIMEOUT.name), 66)  # окружение сильнее файла
        path = os.path.join(self.dir, ".env")
        with open(path, "w") as f:
            f.write('# комментарий\nNEXUS_MODEL="opus"\nNEXUS_TG_TOKEN=\n')
        with unittest.mock.patch.object(settings, "HERE", self.dir):
            parsed = settings.dotenv()
        self.assertEqual(parsed, {"NEXUS_MODEL": "opus", "NEXUS_TG_TOKEN": ""})

    def test_file_wins_and_gets_completed(self):
        with open(self.file, "w") as f:
            json.dump({"timeout": 99, "model": "haiku", "future": True, "compact_mb": -5, "engines": "[claude]"}, f)
        with self.env(NEXUS_TIMEOUT="600", NEXUS_USAGE_REFRESH="30"):
            settings.load()
        self.assertEqual(settings.get(settings.TIMEOUT.name), 99)  # файл сильнее .env
        self.assertEqual(settings.get(settings.CLAUDE_MODEL.name), "haiku")  # прежнее имя model перенесено
        self.assertEqual(settings.get(settings.USAGE_REFRESH.name), 30)  # недостающее — из .env
        self.assertEqual(settings.get(settings.COMPACT_MB.name), 10)  # отрицательное — по умолчанию
        stored = self.stored()
        self.assertEqual((stored["usage_refresh"], stored["future"]), (30, True))  # дописан, чужое не теряем
        self.assertNotIn("model", stored)
        self.assertNotIn("engines", stored)  # прежний ручной список удалён

    def test_set_saves_and_notifies(self):
        with self.env():
            settings.load()
        seen = []
        settings.watch(settings.STT_LANG.name, seen.append)
        self.addCleanup(settings._watchers.pop, "stt_lang")
        self.assertEqual(settings.set(settings.STT_LANG.name, " ru "), "ru")
        self.assertEqual(settings.set(settings.TIMEOUT.name, "120"), 120)
        settings.set(settings.STT_LANG.name, "ru")  # то же значение — без уведомления
        self.assertEqual(seen, ["ru"])
        self.assertEqual((self.stored()["stt_lang"], self.stored()["timeout"]), ("ru", 120))
        for name, bad in (("timeout", "abc"), ("timeout", "-1")):
            with self.assertRaises(ValueError):
                settings.set(name, bad)
        with self.assertRaises(KeyError):
            settings.set("nope", 1)
        with self.assertRaises(ValueError):  # только из списка
            settings.set(settings.CLAUDE_MODEL.name, "gpt")
        self.assertEqual(settings.set(settings.CLAUDE_MODEL.name, "opus"), "opus")
        settings.load()  # перечитали с диска — то же
        self.assertEqual(settings.get(settings.TIMEOUT.name), 120)

    def test_admin_form(self):
        with self.env():
            settings.load()
        admin = {"id": 1, "is_admin": True}
        listing = app.general_settings(admin)
        self.assertEqual([p["name"] for p in listing], [p.name for p in settings.PARAMETERS])
        token = next(p for p in listing if p["name"] == "tg_token")
        self.assertTrue(token["secret"])
        self.assertEqual(token["title"], "Токен Telegram-бота")  # на экране — русское название
        model = next(p for p in listing if p["name"] == settings.CLAUDE_MODEL.name)
        self.assertEqual([c["value"] for c in model["choices"]], ["opus", "sonnet", "haiku"])
        self.assertIsNone(model["shown_if"])
        self.assertNotIn("Возможные значения", model["about"])
        self.assertNotIn("engines", [p["name"] for p in listing])
        with self.assertRaises(HTTPException):
            asyncio.run(app.save_settings({"engines": ["codex"]}, admin))
        self.assertEqual(next(p for p in listing if p["name"] == "timeout")["type"], "int")
        with self.assertRaises(HTTPException):  # одно плохое значение — не сохраняется ничего
            asyncio.run(app.save_settings({"stt_lang": "en", "timeout": "x"}, admin))
        self.assertEqual(settings.get(settings.STT_LANG.name), "")
        with self.assertRaises(HTTPException):
            asyncio.run(app.save_settings({"nope": 1}, admin))
        result = asyncio.run(app.save_settings({"stt_lang": "en", "timeout": "300"}, admin))
        self.assertEqual(next(p for p in result if p["name"] == "timeout")["value"], 300)

    def test_codex_model(self):
        """Модель Codex — из каталога codex; новая беседа берёт модель по умолчанию своей сети."""
        with self.env():
            settings.load()
        catalog = [{"slug": "gpt-a", "name": "GPT A", "about": "", "efforts": ["low"]},
                   {"slug": "gpt-b", "name": "GPT B", "about": "", "efforts": ["low"]}]
        with unittest.mock.patch.object(core, "codex_models", return_value=catalog), \
                unittest.mock.patch.object(core, "CODEX_BIN", "/bin/true"):
            choices = next(p for p in settings.listing() if p["name"] == "codex_model")["choices"]
            self.assertEqual(choices, [{"value": "", "label": "Первая в каталоге Codex"},
                                       {"value": "gpt-a", "label": "GPT A"}, {"value": "gpt-b", "label": "GPT B"}])
            with self.assertRaises(ValueError):
                settings.set(settings.CODEX_MODEL.name, "gpt-z")
            self.assertEqual(jobs.new_chat(49, "codex")["model"], "gpt-a")  # пусто — первая
            settings.set(settings.CODEX_MODEL.name, "gpt-b")
            self.assertEqual(jobs.new_chat(49, "codex")["model"], "gpt-b")
            settings.set(settings.CLAUDE_MODEL.name, "haiku")
            self.assertEqual(jobs.new_chat(49, "claude")["model"], "haiku")

    def test_live_use(self):
        """Параметры читаются в момент использования: новое значение действует сразу."""
        import sys
        import stt
        with self.env():
            settings.load()
        settings.set(settings.COMPACT_MB.name, 3)
        self.assertEqual(users.clean({})["compact_mb"], 3)
        loaded = []

        class Whisper:
            def __init__(self, name, **kw):
                loaded.append(name)

            def transcribe(self, audio, language=None, **kw):
                return [unittest.mock.Mock(text=f"{language}")], None

        fake = unittest.mock.Mock(WhisperModel=Whisper)
        with unittest.mock.patch.dict(sys.modules, {"faster_whisper": fake}), \
                unittest.mock.patch.object(stt, "model", None), unittest.mock.patch.object(stt, "loaded", ""):
            settings.set(settings.STT_LANG.name, "ru")
            self.assertEqual(stt.run(b"x"), "ru")
            stt.run(b"x")
            settings.set(settings.STT_MODEL.name, "large-v3-turbo")
            stt.run(b"x")
        self.assertEqual(loaded, ["small", "large-v3-turbo"])  # сменили модель — загрузилась новая


class Update(unittest.TestCase):
    def test_drain_and_busy(self):
        owner = {"id": 44, "is_admin": False}
        meta = core.create_chat(44, "claude", "sonnet")
        jobs.save_busy()
        with open(jobs.BUSY) as f:
            self.assertEqual(json.load(f), {"pid": os.getpid(), "jobs": 0, "transcribing": 0})
        jobs.transcribing[(44, meta["id"])] += 2
        jobs.save_busy()
        with open(jobs.BUSY) as f:
            self.assertEqual(json.load(f)["transcribing"], 2)
        del jobs.transcribing[(44, meta["id"])]
        open(jobs.DRAIN, "w").close()
        try:
            for call in (lambda: asyncio.run(app.send(meta["id"], "привет", [], owner)),
                         lambda: asyncio.run(app.compact(meta["id"], owner)),
                         lambda: asyncio.run(app.voice(meta["id"], Voice.upload(None), 1.0, "", owner))):
                with self.assertRaises(HTTPException) as caught:
                    call()
                self.assertEqual(caught.exception.status_code, 503)
            with self.assertRaises(jobs.Refused):  # бот зовёт start напрямую
                asyncio.run(jobs.start(44, meta, "привет", {}))
        finally:
            os.remove(jobs.DRAIN)
        self.assertNotIn((44, meta["id"]), jobs.jobs)

    def test_update_script(self):
        self.assertEqual(subprocess.run(["bash", "-n", os.path.join(core.HERE, "update.sh")]).returncode, 0)


FAKE_CODEX = os.path.join(TMP, "fake-codex")
with open(FAKE_CODEX, "w") as f:
    # codex app-server: отвечает на resume и compact/start; FAKE_COMPACT=fail — сжатие не удалось.
    f.write(f"""#!{sys.executable}
import json, os, sys
assert sys.argv[1] == "app-server"
def out(m):
    print(json.dumps(m), flush=True)
for line in sys.stdin:
    m = json.loads(line)
    if m.get("method") == "thread/resume":
        out({{"id": m["id"], "result": {{"thread": {{"id": m["params"]["threadId"]}}}}}})
    elif m.get("method") == "thread/compact/start":
        out({{"id": m["id"], "result": {{}}}})
        fail = os.environ.get("FAKE_COMPACT") == "fail"
        if not fail:
            out({{"method": "item/completed", "params": {{"item": {{"type": "contextCompaction"}}}}}})
        out({{"method": "turn/completed", "params": {{"turn": {{"status": "failed" if fail else "completed",
             "error": {{"message": "лимит"}} if fail else None}}}}}})
""")
os.chmod(FAKE_CODEX, 0o755)


class CodexCompact(unittest.TestCase):
    def test_compact(self):
        meta = core.save_chat(50, {**core.create_chat(50, "codex", "gpt"), "sid": "thread-1"})
        with unittest.mock.patch.object(core, "CODEX_BIN", FAKE_CODEX):
            events = asyncio.run(collect(core.run(50, meta, "/compact", ALL, raw=True)))
            self.assertEqual(events, [{"kind": "compacted", "tokens": 0, "auto": False}])
            with unittest.mock.patch.dict(os.environ, {"FAKE_COMPACT": "fail"}), \
                    self.assertRaisesRegex(RuntimeError, "лимит"):
                asyncio.run(collect(core.run(50, meta, "/compact", ALL, raw=True)))
            with self.assertRaises(RuntimeError):  # сессии нет — сжимать нечего
                asyncio.run(collect(core.run(50, {**meta, "sid": ""}, "/compact", ALL, raw=True)))


class WebPush(unittest.TestCase):
    SUB = {"endpoint": "https://web.push.apple.com/abc", "keys": {"p256dh": "BPub", "auth": "secret"}}

    def test_keys_and_subscriptions(self):
        import push
        key = push.public_key()
        self.assertEqual(len(key), 87)  # точка P-256 без сжатия в base64url
        self.assertEqual(push.public_key(), key)  # создаётся один раз
        self.assertEqual(oct(os.stat(push.keys_path()).st_mode & 0o777), "0o600")
        for bad in ({}, {"endpoint": "http://x", "keys": {"p256dh": "a", "auth": "b"}}, {"endpoint": "https://x"}):
            with self.assertRaises(ValueError):
                push.add(51, bad)
        push.add(51, self.SUB, "iPhone")
        push.add(51, self.SUB, "iPhone")  # то же устройство — одна запись
        self.assertEqual([s["label"] for s in push.subscriptions(51)], ["iPhone"])
        push.remove(51, self.SUB["endpoint"])
        self.assertEqual(push.subscriptions(51), [])

    def test_notify_drops_dead(self):
        import push
        alive = {**self.SUB, "endpoint": "https://fcm.googleapis.com/x"}
        push.add(52, self.SUB)
        push.add(52, alive)
        sent = []

        def fake(sub, payload):
            sent.append((sub["endpoint"], payload["title"], payload["url"]))
            return sub["endpoint"] == alive["endpoint"]  # Apple ответил 410 — подписки больше нет
        with unittest.mock.patch.object(push, "_send", fake):
            self.assertEqual(asyncio.run(push.notify(52, "Ответ готов", "Беседа", "/#c1")), 1)
        self.assertEqual(len(sent), 2)
        self.assertEqual([s["endpoint"] for s in push.subscriptions(52)], [alive["endpoint"]])

    def test_answer_ready(self):
        """Ответ готов, а беседу никто не смотрит — пуш на устройства; смотрят — нет."""
        meta = core.create_chat(53, "claude", "sonnet")
        meta["title"] = "Про бананы"
        calls = []

        async def fake(uid, title, body, url="/"):
            calls.append((uid, title, body, url))

        async def go():
            job = await jobs.start(53, meta, "привет", users.settings(53))
            await job.task
        with unittest.mock.patch("push.notify", fake):
            asyncio.run(go())
            jobs.watchers[(53, meta["id"])] += 1
            try:
                asyncio.run(go())
            finally:
                del jobs.watchers[(53, meta["id"])]
        self.assertEqual(calls, [(53, "Ответ готов", "Про бананы", "/#" + meta["id"])])


class Retry(unittest.TestCase):
    def test_visible(self):
        events = [{"kind": "user", "text": "a"}, {"kind": "text", "text": "1"}, {"kind": "done"},
                  {"kind": "user", "text": "b"}, {"kind": "text", "text": "2"}, {"kind": "done"},
                  {"kind": "retry"}, {"kind": "user", "text": "c"}, {"kind": "done"}]
        shown = core.visible(events)
        self.assertEqual([e.get("text") for e in shown if e["kind"] == "user"], ["a", "c"])
        self.assertEqual(core.last_request(shown), 3)
        self.assertIsNone(core.last_request(shown + [{"kind": "compact"}, {"kind": "compacted"}]))
        self.assertIsNone(core.last_request([]))

    def transcript(self, uid, sid, records):
        project = os.path.join(core.claude_home(uid), "projects", "-tmp-r")
        os.makedirs(project, exist_ok=True)
        with open(os.path.join(project, sid + ".jsonl"), "w") as f:
            f.write("".join(json.dumps(r) + "\n" for r in records))

    def test_claude_rewind(self):
        meta = core.create_chat(46, "claude", "sonnet")
        prompt = lambda u, parent, text: {"type": "user", "uuid": u, "parentUuid": parent,  # noqa: E731
                                          "message": {"content": [{"type": "text", "text": text}]}}
        self.transcript(46, meta["sid"], [
            {"type": "attachment", "uuid": "a0", "parentUuid": None},
            prompt("u1", "a0", "первый\n\n[Рабочая папка]"),
            {"type": "assistant", "uuid": "x1", "parentUuid": "u1"},
            {"type": "user", "uuid": "t1", "parentUuid": "x1", "message": {"content": [{"type": "tool_result"}]}},
            prompt("u2", "t1", "второй\n\n[Рабочая папка]"),
            {"type": "assistant", "uuid": "x2", "parentUuid": "u2"}])
        self.assertEqual(core.rewind(46, meta, "второй"), {"at": "t1", "drop": "u2"})
        self.assertIsNone(core.rewind(46, meta, "другой"))  # ход не дошёл до сессии — откатывать нечего
        args = core.claude_args(46, meta, core.workspace(46, meta["id"]), ALL, rewind={"at": "t1", "drop": "u2"})
        self.assertEqual(args[-4:], ["--resume-session-at", "t1", "--resume-drops-turn", "u2"])
        # Повтор первого хода — сессия с начала.
        sid = meta["sid"]
        self.transcript(46, sid, [prompt("u1", None, "первый")])
        self.assertIsNone(core.rewind(46, meta, "первый"))
        self.assertNotEqual(meta["sid"], sid)
        self.assertEqual(core.load_chat(46, meta["id"])["sid"], meta["sid"])

    def test_codex_rewind(self):
        import sqlite3
        meta = core.save_chat(47, {**core.create_chat(47, "codex", "gpt"), "sid": "0000-sid"})
        folder = os.path.join(core.codex_home(47), "sessions", "2026", "01", "01")
        os.makedirs(folder)
        path = os.path.join(folder, "rollout-2026-01-01-0000-sid.jsonl")
        lines = [{"type": "session_meta", "payload": {}},
                 {"type": "event_msg", "payload": {"type": "task_started"}},
                 {"type": "response_item", "payload": {"type": "message", "role": "user",
                                                       "content": [{"type": "input_text", "text": "раз\n\n[папка]"}]}},
                 {"type": "event_msg", "payload": {"type": "task_complete"}},
                 {"type": "event_msg", "payload": {"type": "task_started"}},
                 {"type": "event_msg", "payload": {"type": "user_message", "message": "два\n\n[папка]"}},
                 {"type": "event_msg", "payload": {"type": "task_complete"}}]
        with open(path, "w") as f:
            f.write("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in lines))
        db = os.path.join(core.codex_home(47), "thread_history_1.sqlite")
        with contextlib.closing(sqlite3.connect(db)) as conn, conn:
            conn.execute("CREATE TABLE thread_turns (thread_id TEXT, turn_id TEXT)")
            conn.executemany("INSERT INTO thread_turns VALUES (?, ?)", [("0000-sid", "t1"), ("other", "t9")])
        self.assertIsNone(core.rewind(47, meta, "три"))  # последний ход — не этот запрос
        self.assertEqual(len(open(path).readlines()), 7)
        self.assertIsNone(core.rewind(47, meta, "два"))
        self.assertEqual(len(open(path).readlines()), 4)  # остался первый ход
        with contextlib.closing(sqlite3.connect(db)) as conn:  # проекцию истории codex соберёт заново из журнала
            self.assertEqual(list(conn.execute("SELECT thread_id FROM thread_turns")), [("other",)])
        self.assertIsNone(core.rewind(47, meta, "раз"))
        self.assertEqual(meta["sid"], "")  # первый ход — новая сессия

    def test_retry_flow(self):
        owner = {"id": 48, "is_admin": False}
        meta = core.create_chat(48, "claude", "sonnet")
        cwd = core.workspace(48, meta["id"])
        with open(os.path.join(cwd, "files", "from_user", "pic.png"), "wb") as f:
            f.write(b"\x89PNG")

        async def go():
            job = await jobs.start(48, meta, "первый вопрос", users.settings(48), ["pic.png"])
            await job.task
            with self.assertRaises(HTTPException):  # пока идёт ответ — нельзя
                jobs.jobs[(48, meta["id"])] = job
                await app.retry(meta["id"], app.Retry(), owner)
            del jobs.jobs[(48, meta["id"])]
            with unittest.mock.patch("jobs.start", wraps=jobs.start) as start:
                await app.retry(meta["id"], app.Retry(text="изменённый вопрос"), owner)
                await jobs.jobs[(48, meta["id"])].task
            return start.await_args

        call = asyncio.run(go())
        self.assertEqual(call.kwargs["paths"], ["files/general/pic.png"])  # вложение уже в general
        self.assertEqual(len(call.args[5]), 1)  # и снова уходит картинкой
        raw = core.history(48, meta["id"])
        self.assertIn({"kind": "retry"}, [{"kind": e["kind"]} for e in raw])
        shown = core.visible(raw)
        self.assertEqual([e["text"] for e in shown if e["kind"] == "user"], ["изменённый вопрос"])
        self.assertEqual(shown[-1]["kind"], "done")
        md = app.export(meta["id"], owner).body.decode()
        self.assertTrue(md.startswith("# изменённый вопрос\n"))  # название — из нового запроса
        self.assertIn("### Вы", md)
        self.assertIn("изменённый вопрос", md)
        self.assertNotIn("первый вопрос", md)
        self.assertIn("### Claude", md)
        self.assertIn("Привет", md)
        self.assertIn("filename*=UTF-8''", app.export(meta["id"], owner).headers["content-disposition"])
        found = app.search("ИЗМЕНЁН", owner)
        self.assertEqual([(c["id"], c["hits"]) for c in found], [(meta["id"], 1)])
        self.assertIn("изменённый", found[0]["snippet"])
        self.assertEqual(app.search("первый вопрос", owner), [])  # заменённый ход не ищется
        self.assertEqual(app.search("и", owner), [])  # слишком коротко
        # После сжатия повторять нечего.
        core.append_history(48, meta["id"], {"kind": "compact"})
        with self.assertRaises(HTTPException):
            asyncio.run(app.retry(meta["id"], app.Retry(), owner))


class Access(unittest.TestCase):
    def test_paths(self):
        owner, stranger = {"id": 7, "is_admin": False}, {"id": 8, "is_admin": False}
        chat = core.create_chat(7, "claude", "sonnet")["id"]
        root = core.workspace(7, chat)
        with open(os.path.join(root, "files", "general", "ok.txt"), "w") as f:
            f.write("x")
        os.symlink("/etc/passwd", os.path.join(root, "files", "general", "link"))
        self.assertTrue(app.get_file(chat, "files/general/ok.txt", owner))
        for bad in ("../../../../nexus.db", "files/general/link", "MEMORY.md", "/etc/passwd"):
            with self.assertRaises(HTTPException, msg=bad):
                app.get_file(chat, bad, owner)
        with self.assertRaises(HTTPException):  # чужая беседа
            app.get_file(chat, "files/general/ok.txt", stranger)
        with self.assertRaises(HTTPException):  # id беседы идёт в путь
            app.get_chat("../7", owner)



class Engines(unittest.TestCase):
    def tearDown(self):
        core._engine_states.clear()

    def test_newest_codex_binary_wins_over_configured_old_version(self):
        old = os.path.join(TMP, "codex-old")
        new = os.path.join(TMP, "codex-new")
        for path, version in ((old, "0.154.0"), (new, "0.163.0")):
            with open(path, "w") as f:
                f.write(f"#!{sys.executable}\nprint('codex-cli {version}')\n")
            os.chmod(path, 0o755)
        self.assertEqual(core.newest_executable("codex", old, (new,)), new)

    def test_checks_install_login_answer_and_limits(self):
        replies = {("claude", False): (0, "logged in"), ("claude", True): (0, "OK"),
                   ("codex", False): (0, "Logged in"), ("codex", True): (1, "usage limit exceeded")}
        with unittest.mock.patch.object(core, "installed", return_value=True), \
                unittest.mock.patch.object(core, "_run_check", side_effect=lambda engine, probe=False: replies[engine, probe]):
            core.refresh_engines()
        self.assertEqual(core.engines(), ["claude", "codex"])
        states = {item["engine"]: item for item in core.engine_statuses()}
        self.assertEqual(states["claude"]["state"], "available")
        self.assertEqual(states["codex"]["state"], "limited")

        with unittest.mock.patch.object(core, "installed", side_effect=lambda engine: engine == "claude"), \
                unittest.mock.patch.object(core, "_run_check", return_value=(1, "not logged in")):
            core.refresh_engines()
        states = {item["engine"]: item for item in core.engine_statuses()}
        self.assertEqual((states["claude"]["state"], states["claude"]["login"]), ("login", True))
        self.assertEqual((states["codex"]["state"], states["codex"]["installed"]), ("missing", False))
        self.assertEqual(core.engines(), [])

    def test_codex_catalog_matches_cli_version(self):
        folder = os.path.join(core.DATA, "engine", "codex")
        os.makedirs(folder, exist_ok=True)
        old = os.path.join(folder, "models_cache.json")
        new = os.path.join(TMP, "new-models.json")
        def cache(path, version, slug):
            with open(path, "w") as f:
                json.dump({"client_version": version, "models": [{"slug": slug, "visibility": "list",
                           "supported_reasoning_levels": []}]}, f)
        cache(old, "0.154.0", "gpt-supported")
        cache(new, "0.163.0", "gpt-too-new")
        with unittest.mock.patch.object(core, "MODELS_CACHE", new), \
                unittest.mock.patch.object(core, "codex_version", return_value="0.154.0"), \
                unittest.mock.patch.object(core, "installed", return_value=True), \
                unittest.mock.patch.object(core.glob, "glob", return_value=[]):
            self.assertEqual([model["slug"] for model in core.codex_models()], ["gpt-supported"])


class Permissions(unittest.TestCase):
    def setUp(self):
        self.meta = core.create_chat(3, "claude", "sonnet")
        self.cwd = core.workspace(3, self.meta["id"])

    def claude(self, **off):
        args = core.claude_args(3, self.meta, self.cwd, {**ALL, **off})
        return args, json.loads(args[args.index("--settings") + 1])

    def test_claude_all(self):
        args, settings = self.claude()
        self.assertIn("Bash", args[args.index("--tools") + 1])
        self.assertEqual(settings["sandbox"]["network"], {"allowedDomains": ["*"]})
        # WebFetch(domain:*) открыл бы сеть Bash в обход галочки «Интернет».
        self.assertEqual(settings["permissions"], {"allow": args[args.index("--tools") + 1].split(","), "deny": []})
        self.assertTrue(settings["sandbox"]["autoAllowBashIfSandboxed"])
        self.assertNotIn("denyWrite", settings["sandbox"]["filesystem"])

    def test_claude_restricted(self):
        other = core.workspace(3, core.create_chat(3, "claude", "sonnet")["id"])
        args, settings = self.claude(bash=False, write=False, memory=False, network=False, web=False, all_chats=False)
        tools = args[args.index("--tools") + 1].split(",")
        self.assertEqual(tools, ["Read", "Glob", "Grep"])
        sandbox = settings["sandbox"]
        self.assertEqual(sandbox["network"], {"strictAllowlist": True})
        fs = sandbox["filesystem"]
        self.assertIn(other, fs["denyRead"])
        self.assertNotIn(self.cwd, fs["denyRead"])
        # Корень рабочей папки не запрещаем: bwrap пишет туда защитные файлы.
        self.assertNotIn(self.cwd, fs["denyWrite"])
        self.assertIn(os.path.join(self.cwd, "files"), fs["denyWrite"])
        self.assertIn(core.memory_file(3), fs["denyWrite"])
        self.assertEqual(fs["allowWrite"], [])
        self.assertIn(f"Read(/{other}/**)", settings["permissions"]["deny"])
        # В библиотеке файлы всех бесед: без доступа к ним закрыта и она.
        self.assertIn(core.library(3), fs["denyRead"])
        self.assertIn(f"Read(/{core.library(3)}/**)", settings["permissions"]["deny"])

    def test_claude_split(self):
        # Память без файлов: Write/Edit есть, но files/ закрыта и для них, и для Bash.
        args, settings = self.claude(write=False)
        self.assertIn("Edit", args[args.index("--tools") + 1])
        files = os.path.join(self.cwd, "files")
        self.assertEqual(settings["sandbox"]["filesystem"]["denyWrite"],
                         [files, os.path.join(self.cwd, "CLAUDE.md"), os.path.join(self.cwd, "AGENTS.md"),
                          core.library(3)])
        self.assertIn(f"Edit(/{files}/**)", settings["permissions"]["deny"])
        _, settings = self.claude(memory=False)
        self.assertIn(f"Edit(/{core.memory_file(3)})", settings["permissions"]["deny"])

    def test_claude_lists(self):
        lists = dict(commands=True, allowed_commands=["git status", "ls *"], denied_commands=["rm"],
                     site_allow=True, allowed_sites=[], site_deny=True, denied_sites=["vk.com"])
        args, settings = self.claude(**lists)
        perms = settings["permissions"]
        self.assertTrue({"Bash(git status)", "Bash(git status *)", "Bash(ls *)"} <= set(perms["allow"]))
        self.assertNotIn("Bash", perms["allow"])
        self.assertNotIn("Bash(ls * *)", perms["allow"])
        # Пустой белый список сайтов: WebFetch есть, но не разрешён ни на один домен.
        self.assertIn("WebFetch", args[args.index("--tools") + 1])
        self.assertFalse([a for a in perms["allow"] if a.startswith("WebFetch")])
        self.assertTrue({"Bash(rm)", "Bash(rm *)", "WebFetch(domain:vk.com)"} <= set(perms["deny"]))
        self.assertFalse(settings["sandbox"]["autoAllowBashIfSandboxed"])
        # Белый список сайтов CLI переносит в сеть песочницы — без «Интернета» закрываем его для команд.
        _, settings = self.claude(network=False, site_allow=True, allowed_sites=["a.ru"])
        self.assertEqual(settings["sandbox"]["network"], {"strictAllowlist": True, "deniedDomains": ["a.ru"]})
        self.assertIn("WebFetch(domain:a.ru)", settings["permissions"]["allow"])
        # Вне песочницы: исключение из sandbox и разрешение — только с галочкой и Bash.
        _, settings = self.claude(unsandboxed=True, unsandboxed_commands=["docker ps"])
        self.assertEqual(settings["sandbox"]["excludedCommands"], ["docker ps", "docker ps *"])
        self.assertIn("Bash(docker ps *)", settings["permissions"]["allow"])
        _, settings = self.claude(unsandboxed=True, unsandboxed_commands=["docker ps"], bash=False)
        self.assertEqual(settings["sandbox"]["excludedCommands"], [])
        self.assertFalse(ALL["unsandboxed"])
        # Без Bash и поиска списки не действуют.
        _, settings = self.claude(**lists, bash=False, web=False)
        self.assertEqual(settings["permissions"]["deny"], [])

    def test_claude_skills(self):
        # Иначе следующие тесты увидят установленные скиллы: галочка включена по умолчанию.
        self.addCleanup(shutil.rmtree, core.PLUGIN, True)
        # Скиллов не установлено — галочка ничего не меняет, остаётся --safe-mode.
        args, _ = self.claude(skills=True)
        self.assertIn("--safe-mode", args)
        for name in ("docx", "pdf"):
            os.makedirs(os.path.join(core.PLUGIN, "skills", name), exist_ok=True)
            with open(os.path.join(core.PLUGIN, "skills", name, "SKILL.md"), "w") as f:
                f.write(f"---\nname: {name}\ndescription: '{name} files'\n---\n")
        self.assertEqual(core.skills(), {"docx": "docx files", "pdf": "pdf files"})
        args, settings = self.claude(skills=True, skills_off=["pdf"])
        self.assertNotIn("--safe-mode", args)
        self.assertIn("--strict-mcp-config", args)
        self.assertEqual(args[args.index("--plugin-dir") + 1], core.PLUGIN)
        self.assertIn("Skill", args[args.index("--tools") + 1])
        self.assertIn("Skill(nexus:pdf)", settings["permissions"]["deny"])
        self.assertTrue(settings["disableAllHooks"] and settings["disableSkillShellExecution"])
        # Скрипты скиллов читать можно, менять нельзя.
        self.assertIn(core.PLUGIN, settings["sandbox"]["filesystem"]["allowRead"])
        self.assertIn(core.PLUGIN, settings["sandbox"]["filesystem"]["denyWrite"])
        self.assertIn(f"Edit(/{core.PLUGIN}/**)", settings["permissions"]["deny"])
        args, _ = self.claude(skills=False)
        self.assertIn("--safe-mode", args)  # галочка снята
        # Скилл аккаунта выкладывается ссылкой; песочнице открыт её настоящий путь.
        synced = os.path.join(TMP, "synced", "bucket", "xlsx")
        os.makedirs(synced)
        with open(os.path.join(synced, "SKILL.md"), "w") as f:
            f.write("---\nname: xlsx\ndescription: tables\n---\n")
        with unittest.mock.patch.object(core, "SYNCED", os.path.dirname(os.path.dirname(synced))):
            self.assertEqual(core.account_skills(), {"xlsx": "tables"})
            self.assertEqual(set(core.publish_skills({"xlsx"})), {"docx", "pdf", "xlsx"})
            _, settings = self.claude(skills=True)
            self.assertIn(synced, settings["sandbox"]["filesystem"]["allowRead"])
            self.assertIn(synced, settings["sandbox"]["filesystem"]["denyWrite"])
            _, settings = self.claude(skills=True, skills_off=["xlsx"])
            self.assertNotIn(synced, settings["sandbox"]["filesystem"]["allowRead"])
            # Снятие убирает только ссылку, свои папки плагина остаются.
            self.assertEqual(set(core.publish_skills(set())), {"docx", "pdf"})
        with self.assertRaises(ValueError):
            users.clean({"access": {"claude": {"skills_off": ["../x"]}}})

    def test_codex(self):
        meta = {**self.meta, "engine": "codex", "sid": ""}
        with unittest.mock.patch.object(core, "CODEX_BIN", "codex"):
            on = " ".join(core.codex_args(3, meta, self.cwd, [], ACCESS["codex"]))
            off = " ".join(core.codex_args(3, meta, self.cwd, [], {**ACCESS["codex"], "network": False,
                                                                   "web": False, "write": False}))
        self.assertIn("network.enabled=true", on)
        self.assertIn('web_search="live"', on)
        self.assertIn('"%s" = "write"' % core.data_dir(3), on)
        self.assertIn("network.enabled=false", off)
        self.assertIn('web_search="disabled"', off)
        self.assertIn('"%s" = "read"' % core.data_dir(3), off)


    def codex_settings(self, **changes):
        import tomllib
        meta = {**self.meta, "engine": "codex", "sid": ""}
        args = core.codex_args(3, meta, self.cwd, [], {**ACCESS["codex"], **changes})
        return tomllib.loads("\n".join(args[i + 1] for i, arg in enumerate(args[:-1]) if arg == "-c"))

    def test_codex_memory_and_isolation(self):
        other = core.workspace(3, core.create_chat(3, "codex", "test")["id"])
        fs = self.codex_settings(memory=False)["permissions"]["chat"]["filesystem"]
        self.assertEqual(fs[core.data_dir(3)], "write")
        for path in (core.memory_file(3), os.path.join(self.cwd, "MEMORY.md"), os.path.join(other, "MEMORY.md")):
            self.assertEqual(fs[path], "read")
        fs = self.codex_settings(write=False, memory=True, all_chats=False)["permissions"]["chat"]["filesystem"]
        self.assertEqual(fs[core.data_dir(3)], "read")
        self.assertNotIn(core.memory_file(3), fs)  # наследует общий read; file-write ломает CLI
        self.assertNotIn(os.path.join(self.cwd, "MEMORY.md"), fs)
        self.assertEqual(fs[other], "deny")
        self.assertEqual(fs[core.library(3)], "deny")
        self.assertNotIn(os.path.join(other, "MEMORY.md"), fs)

    def test_codex_site_skills(self):
        self.addCleanup(shutil.rmtree, core.PLUGIN, True)
        for name in ("docx", "pdf"):
            folder = os.path.join(core.PLUGIN, "skills", name)
            os.makedirs(folder, exist_ok=True)
            with open(os.path.join(folder, "SKILL.md"), "w") as f:
                f.write(f"---\nname: {name}\ndescription: {name}\n---\n")
        fs = self.codex_settings(skills=True, skills_off=["pdf"])["permissions"]["chat"]["filesystem"]
        links = os.path.join(core.codex_home(3), "skills")
        self.assertTrue(os.path.islink(os.path.join(links, "docx")))
        self.assertFalse(os.path.lexists(os.path.join(links, "pdf")))
        self.assertEqual(fs[os.path.realpath(os.path.join(core.PLUGIN, "skills", "docx"))], "read")
        # Системные скиллы и посторонние элементы в CODEX_HOME не удаляются.
        os.makedirs(os.path.join(links, ".system"), exist_ok=True)
        with open(os.path.join(links, "mine"), "w") as f:
            f.write("keep")
        self.codex_settings(skills=False, skills_off=[])
        self.assertFalse(os.path.lexists(os.path.join(links, "docx")))
        self.assertTrue(os.path.isdir(os.path.join(links, ".system")))
        self.assertTrue(os.path.isfile(os.path.join(links, "mine")))

    def test_codex_network_and_tools(self):
        conf = self.codex_settings(bash=False, multi_agent=False, web_cached=True,
                                   site_allow=True, allowed_sites=["docs.python.org"],
                                   site_deny=True, denied_sites=["docs.python.org"])
        self.assertFalse(conf["features"]["shell_tool"])
        self.assertFalse(conf["features"]["multi_agent"])
        self.assertEqual(conf["web_search"], "cached")
        self.assertTrue(conf["features"]["network_proxy"]["enabled"])
        self.assertEqual(conf["permissions"]["chat"]["network"]["domains"], {"docs.python.org": "deny"})
        conf = self.codex_settings(site_allow=True)
        self.assertEqual(conf["permissions"]["chat"]["network"]["domains"], {})
        self.assertTrue(conf["features"]["network_proxy"]["enabled"])
        conf = self.codex_settings(site_deny=True, denied_sites=["example.com"])
        self.assertEqual(conf["permissions"]["chat"]["network"]["domains"], {"*": "allow", "example.com": "deny"})
        conf = self.codex_settings(network=False, site_allow=True, web=False, web_cached=True)
        self.assertFalse(conf["features"]["network_proxy"]["enabled"])
        self.assertFalse(conf["permissions"]["chat"]["network"]["enabled"])
        self.assertEqual(conf["web_search"], "disabled")

    def test_codex_unsandboxed(self):
        rules = os.path.join(core.codex_home(3), "rules", "nexus.rules")
        conf = self.codex_settings()
        # Папка беседы — недоверенная: правила из её .codex модель подложила бы сама.
        self.assertEqual(conf["projects"], {self.cwd: {"trust_level": "untrusted"}})
        self.assertEqual(conf["project_root_markers"], [])
        self.assertFalse(os.path.exists(rules))
        # Без галочки или без «Запуска команд» список не действует.
        self.codex_settings(unsandboxed_commands=["ls -la"])
        self.assertFalse(os.path.exists(rules))
        self.codex_settings(unsandboxed=True, unsandboxed_commands=["ls -la"], bash=False)
        self.assertFalse(os.path.exists(rules))
        with open(os.path.join(os.path.dirname(rules), "default.rules"), "w") as f:
            f.write('prefix_rule(pattern=["sh"])\n')
        self.codex_settings(unsandboxed=True, unsandboxed_commands=["ls -la", "git log 'a b'"])
        with open(rules) as f:
            text = f.read()
        self.assertIn('prefix_rule(pattern=["ls", "-la"], decision="allow")', text)
        self.assertIn('prefix_rule(pattern=["git", "log", "a b"], decision="allow")', text)
        self.assertIn('host_executable(name="ls", paths=', text)  # свой «ls» по абсолютному пути не пройдёт
        self.assertEqual(os.listdir(os.path.dirname(rules)), ["nexus.rules"])
        # Модель знает список: иначе склеит команду с другими и останется в песочнице.
        self.assertIn("`ls -la`", self.codex_settings(unsandboxed=True, unsandboxed_commands=["ls -la"])["developer_instructions"])
        conf = self.codex_settings(unsandboxed=False, unsandboxed_commands=["ls -la"])
        self.assertFalse(os.path.exists(rules))
        self.assertNotIn("Вне песочницы", conf["developer_instructions"])
        self.assertEqual(users.clean({"access": {"codex": {"unsandboxed_commands": [" docker  ps "]}}})
                         ["access"]["codex"]["unsandboxed_commands"], ["docker ps"])
        for bad in ("docker *", "echo 'a"):
            with self.assertRaises(ValueError):
                users.clean({"access": {"codex": {"unsandboxed_commands": [bad]}}})


class Settings(unittest.TestCase):
    def test_available(self):
        data = users.clean({"engines": {"claude": {"models": {
            "opus": {"on": False}, "sonnet": {"on": True, "efforts": ["low", "medium"]},
            "haiku": {"on": True, "efforts": []}}}}})
        with unittest.mock.patch.object(core, "CODEX_BIN", None):
            avail = users.available(data)
        self.assertEqual([m["slug"] for m in avail["claude"]], ["sonnet"])  # opus выкл, у haiku нет уровней
        sonnet = avail["claude"][0]
        self.assertEqual((sonnet["efforts"], sonnet["auto"]), (["low", "medium"], False))
        self.assertIsNone(jobs.allowed_model(avail, "claude", "sonnet", "low"))
        self.assertIsNotNone(jobs.allowed_model(avail, "claude", "sonnet", ""))  # «авто» мог бы выбрать high
        self.assertIsNotNone(jobs.allowed_model(avail, "claude", "opus", "low"))
        self.assertIsNotNone(jobs.allowed_model(avail, "codex", "x", ""))
        with self.assertRaises(ValueError):
            users.clean({"engines": {"claude": {"limit_5h": 150}}})

    def test_access(self):
        # У каждой сети свои галочки; старый общий формат применяется ко всем.
        data = users.clean({"access": {"claude": {"web": False}}})["access"]
        self.assertEqual((data["claude"]["web"], data["codex"]["web"]), (False, True))
        self.assertTrue(data["codex"]["bash"])
        old = users.clean({"access": {"network": False}})["access"]
        self.assertEqual((old["claude"]["network"], old["codex"]["network"]), (False, False))
        # Память раньше шла под «Изменением файлов»: без записи старый пользователь её не получает.
        self.assertFalse(users.clean({"access": {"claude": {"write": False}}})["access"]["claude"]["memory"])
        self.assertFalse(users.clean({"access": {"codex": {"write": False}}})["access"]["codex"]["memory"])
        self.assertFalse(users.clean({"access": {"codex": {"write": False, "memory": True}}})["access"]["codex"]["memory"])
        self.assertTrue(users.clean({})["access"]["codex"]["skills"])
        self.assertEqual(users.clean({"access": {"codex": {"skills_off": ["pdf"]}}})
                         ["access"]["codex"]["skills_off"], ["pdf"])
        codex = users.clean({"access": {"codex": {"allowed_sites": ["https://Docs.Python.org/3/"]}}})["access"]["codex"]
        self.assertEqual(codex["allowed_sites"], ["docs.python.org"])
        self.assertFalse(ALL["commands"] or ALL["site_allow"] or ALL["site_deny"])
        conf = users.clean({"access": {"claude": {"allowed_sites": ["https://Docs.Python.org/3/", "*.пример.рф", " "],
                                                  "denied_commands": ["  git   push "]}}})["access"]["claude"]
        self.assertEqual(conf["allowed_sites"], ["docs.python.org", "*.xn--e1afmkfd.xn--p1ai"])
        self.assertEqual(conf["denied_commands"], ["git push"])
        for bad in ({"allowed_sites": ["a b"]}, {"denied_sites": ["x.com:8080"]}, {"allowed_commands": ["ls)"]}):
            with self.assertRaises(ValueError):
                users.clean({"access": {"claude": bad}})


class Accounting(unittest.TestCase):
    R5, RW = 4102444800, 4103049600

    def keep_snapshot(self):
        before = usage.global_snapshot()
        self.addCleanup(usage._save, usage.global_path(), before)

    def point(self, percent, window="five_hour", resets=None):
        return {"kind": "limits", "window": window, "percent": percent, "resets_at": resets or self.R5}

    def test_charge(self):
        data = users.clean({"engines": {"claude": {"limit_5h": 10}}})
        now = time.time()
        # Первый запуск: базы нет — база = первый снимок запуска.
        usage.charge(20, "claude", [self.point(30), self.point(34)], now, (100, 10))
        self.assertEqual(usage.summary(20, data)["claude"]["five_hour"]["used"], 4)
        # Следующий запуск сразу после: база — свежий общий снимок 34.
        usage.charge(20, "claude", [self.point(36), self.point(39)], time.time())
        self.assertEqual(usage.summary(20, data)["claude"]["five_hour"]["used"], 9)
        self.assertIsNone(usage.check(20, "claude", data))
        # С учётом идущего запуска лимит 10% уже превышен — его остановят.
        self.assertIn("исчерпан", usage.check(20, "claude", data, [self.point(41)], time.time()))
        # Новое окно: расход начинается заново.
        usage.charge(20, "claude", [self.point(2, resets=self.R5 + 18000)], time.time())
        self.assertEqual(usage.summary(20, data)["claude"]["five_hour"]["used"], 0)
        self.assertEqual(usage.summary(20, data)["claude"]["tokens"], {"in": 100, "out": 10})

    def test_share(self):
        """Лимит — часть подписки; чужой расход съедает её, если подписки осталось меньше."""
        data = users.clean({"engines": {"codex": {"limit_5h": 60}}})
        free = users.clean({})
        usage.charge(24, "codex", [self.point(0), self.point(60)], time.time())
        first, second = usage.summary(24, data)["codex"], usage.summary(25, data)["codex"]
        self.assertEqual((first["five_hour"]["share"], first["five_hour"]["used"]), (100, 60))
        self.assertEqual((second["five_hour"]["share"], second["five_hour"]["used"]), (33.33, 0))
        self.assertEqual(second["five_hour"]["subscription"], 60)
        self.assertEqual(second["five_hour"]["resets_at"], self.R5)  # сброс — от подписки, своих трат нет
        self.assertEqual(usage.summary(25, data)["codex"]["seven_day"]["share"], 0)
        self.assertIn("60% из 60%", usage.check(24, "codex", data))
        self.assertIsNone(usage.check(25, "codex", data))
        self.assertEqual(usage.summary(26, free)["codex"]["five_hour"]["share"], 60)  # без лимита — вся подписка
        # Подписка кончилась целиком: второму не осталось ничего, хотя сам он не тратил.
        usage.charge(25, "codex", [self.point(60), self.point(100)], time.time())
        usage._save(usage.user_path(25), {})
        self.assertIn("вашей части не осталось", usage.check(25, "codex", data))
        self.assertIn("вашей части не осталось", usage.check(26, "codex", free))
        # Окно сбросилось — части снова целые.
        snapshot = usage._load(usage.global_path())
        snapshot["codex"]["five_hour"]["resets_at"] = time.time() - 1
        usage._save(usage.global_path(), snapshot)
        self.assertEqual(usage.summary(25, data)["codex"]["five_hour"]["share"], 0)

    def test_stale_base(self):
        usage.charge(21, "claude", [self.point(50)], time.time())
        snapshot = usage._load(usage.global_path())
        snapshot["claude"]["five_hour"]["seen"] -= 3600  # чужое использование за час не наше
        usage._save(usage.global_path(), snapshot)
        usage.charge(22, "claude", [self.point(70), self.point(71)], time.time())
        self.assertEqual(usage._load(usage.user_path(22))["claude"]["five_hour"]["used"], 1)

    def test_refresh(self):
        """Опрос через CLI: оба окна, без чтения credentials; ошибки сохраняют снимок."""
        self.keep_snapshot()
        reply = {"rateLimits": {"limitId": "codex",
                 "primary": {"usedPercent": 7, "windowDurationMins": 300, "resetsAt": self.R5},
                 "secondary": {"usedPercent": 100, "windowDurationMins": 10080, "resetsAt": self.RW}}}
        load = usage._load
        def guarded_load(path):
            self.assertNotIn(path, (core.HOST_CREDS, core.CODEX_HOST_AUTH))
            return load(path)
        usage.store("claude", {"five_hour": (38, 4102444800)})
        with unittest.mock.patch.object(usage, "_codex_rate_limits", new=unittest.mock.AsyncMock(return_value=reply)), \
                unittest.mock.patch.object(usage, "_load", guarded_load):
            self.assertFalse(usage.refresh("claude"))  # только события обычных ответов
            self.assertTrue(usage.refresh("codex"))
        snap = usage.global_snapshot()
        self.assertEqual(usage.subscription(snap, "claude", "five_hour"), (38.0, 4102444800))
        self.assertEqual(usage.subscription(snap, "claude", "seven_day"), (0, 0))  # окно не начато
        self.assertEqual(usage.subscription(snap, "codex", "five_hour"), (7.0, self.R5))
        self.assertEqual(usage.subscription(snap, "codex", "seven_day"), (100.0, self.RW))
        data = users.clean({})
        self.assertIn("вашей части не осталось", usage.over(usage.summary(27, data)["codex"]))

        with unittest.mock.patch.object(usage, "_codex_rate_limits", new=unittest.mock.AsyncMock(side_effect=OSError("offline"))):
            self.assertFalse(usage.refresh("codex"))
        self.assertEqual(usage.global_snapshot(), snap)

        reply = {"rateLimits": {"limitId": "other"}, "rateLimitsByLimitId": {"codex": {
            "primary": {"usedPercent": 0, "windowDurationMins": 300, "resetsAt": self.R5}}}}
        with unittest.mock.patch.object(usage, "_codex_rate_limits", new=unittest.mock.AsyncMock(return_value=reply)):
            self.assertEqual(usage.fetch_codex(), {"five_hour": (0, self.R5)})
        with unittest.mock.patch.object(usage, "_codex_rate_limits", new=unittest.mock.AsyncMock(return_value={})):
            self.assertFalse(usage.refresh("codex"))

    def test_codex_limits_protocol_and_cleanup(self):
        fake = os.path.join(TMP, "fake-limits-cli")
        with open(fake, "w") as f:
            f.write(f'''#!{sys.executable}
import json, os, sys, time
assert sys.argv[1:] == ["app-server"]
assert json.loads(input())["method"] == "initialize"
print(json.dumps({{"id": 0, "result": {{}}}}), flush=True)
assert json.loads(input())["method"] == "initialized"
assert json.loads(input())["method"] == "account/rateLimits/read"
mode = os.environ.get("FAKE_LIMITS_MODE", "ok")
if mode == "timeout":
    time.sleep(60)
if mode == "eof":
    sys.exit(1)
print(json.dumps({{"method": "account/updated", "params": {{}}}}), flush=True)
print(json.dumps({{"id": 1, "error": {{"message": "private output"}}}} if mode == "error" else
                 {{"id": 1, "result": {{"rateLimits": {{"limitId": "codex"}}}}}}), flush=True)
time.sleep(60)
''')
        os.chmod(fake, 0o755)
        with unittest.mock.patch.object(core, "CODEX_BIN", fake), \
                unittest.mock.patch.object(core, "codex_service_home", return_value=TMP), \
                unittest.mock.patch.object(core, "stop", wraps=core.stop) as stop:
            self.assertEqual(asyncio.run(usage._codex_rate_limits()), {"rateLimits": {"limitId": "codex"}})
            for mode, error in (("error", RuntimeError), ("eof", RuntimeError), ("timeout", TimeoutError)):
                with unittest.mock.patch.dict(os.environ, FAKE_LIMITS_MODE=mode):
                    with self.assertRaises(error) as raised:
                        asyncio.run(usage._codex_rate_limits(timeout=.5))
                    self.assertNotIn("private output", str(raised.exception))
            for call in stop.call_args_list:
                self.assertIsNotNone(call.args[0].returncode)

    def test_credential_freshness_uses_only_metadata(self):
        with unittest.mock.patch("builtins.open", side_effect=AssertionError("credentials read")), \
                unittest.mock.patch.object(core.os.path, "getmtime", return_value=123):
            self.assertEqual(core.creds_freshness("credentials"), 123)

    def test_base_at_start(self):
        """Опрос посреди запуска не уменьшает его расход: база — снимок на старте."""
        self.keep_snapshot()
        usage.charge(28, "claude", [self.point(10)], time.time())
        base, started = usage.global_snapshot(), time.time()
        usage.store("claude", {"five_hour": (15, self.R5)})  # опрос: часть запуска уже потрачена
        usage.charge(28, "claude", [self.point(20)], started, base=base)
        self.assertEqual(usage._load(usage.user_path(28))["claude"]["five_hour"]["used"], 10)

    def test_disk(self):
        cwd = core.workspace(23, core.create_chat(23, "claude", "sonnet")["id"])
        with open(os.path.join(cwd, "files", "general", "big"), "wb") as f:
            f.write(b"x" * 5000)
        d = usage.disk(23)
        self.assertGreaterEqual(d["files"], 5000)
        self.assertEqual(d["total"], d["files"] + d["sessions"])


if __name__ == "__main__":
    unittest.main()
