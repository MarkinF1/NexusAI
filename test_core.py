"""python -m unittest test_core — без сети и без настоящих CLI."""
import asyncio
import json
import os
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
sys.stdin.read()
print(json.dumps({{"type": "system", "subtype": "init"}}), flush=True)
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
import usage  # noqa: E402
import users  # noqa: E402

ALL = users.clean({})["access"]
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
    def test_env_list(self):
        for raw, expected in (("[claude]", ["claude"]), ("claude, codex", ["claude", "codex"]),
                              ("", ["claude", "codex"]), ("[codex]", ["codex"])):
            os.environ["NEXUS_ENGINES"] = raw
            with unittest.mock.patch.object(core, "CODEX_BIN", "/bin/true"):
                self.assertEqual(core.engines(), expected, raw)
        os.environ["NEXUS_ENGINES"] = "[claude, codex]"
        with unittest.mock.patch.object(core, "CODEX_BIN", None):
            self.assertEqual(core.engines(), ["claude"])  # codex нет в системе
        del os.environ["NEXUS_ENGINES"]


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
        self.assertEqual(settings["permissions"], {"deny": []})
        self.assertNotIn("denyWrite", settings["sandbox"]["filesystem"])

    def test_claude_restricted(self):
        other = core.workspace(3, core.create_chat(3, "claude", "sonnet")["id"])
        args, settings = self.claude(bash=False, write=False, network=False, web=False, all_chats=False)
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

    def test_codex(self):
        meta = {**self.meta, "engine": "codex", "sid": ""}
        with unittest.mock.patch.object(core, "CODEX_BIN", "codex"):
            on = " ".join(core.codex_args(3, meta, self.cwd, [], ALL))
            off = " ".join(core.codex_args(3, meta, self.cwd, [], {**ALL, "network": False,
                                                                   "web": False, "write": False}))
        self.assertIn("network.enabled=true", on)
        self.assertIn('web_search="live"', on)
        self.assertIn('"%s" = "write"' % core.data_dir(3), on)
        self.assertIn("network.enabled=false", off)
        self.assertIn('web_search="disabled"', off)
        self.assertIn('"%s" = "read"' % core.data_dir(3), off)


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
        self.assertIsNone(app.allowed_model(avail, "claude", "sonnet", "low"))
        self.assertIsNotNone(app.allowed_model(avail, "claude", "sonnet", ""))  # «авто» мог бы выбрать high
        self.assertIsNotNone(app.allowed_model(avail, "claude", "opus", "low"))
        self.assertIsNotNone(app.allowed_model(avail, "codex", "x", ""))
        with self.assertRaises(ValueError):
            users.clean({"engines": {"claude": {"limit_5h": 150}}})


class Accounting(unittest.TestCase):
    R5, RW = 4102444800, 4103049600

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

    def test_stale_base(self):
        usage.charge(21, "claude", [self.point(50)], time.time())
        snapshot = usage._load(usage.global_path())
        snapshot["claude"]["five_hour"]["seen"] -= 3600  # чужое использование за час не наше
        usage._save(usage.global_path(), snapshot)
        usage.charge(22, "claude", [self.point(70), self.point(71)], time.time())
        self.assertEqual(usage._load(usage.user_path(22))["claude"]["five_hour"]["used"], 1)

    def test_disk(self):
        cwd = core.workspace(23, core.create_chat(23, "claude", "sonnet")["id"])
        with open(os.path.join(cwd, "files", "general", "big"), "wb") as f:
            f.write(b"x" * 5000)
        d = usage.disk(23)
        self.assertGreaterEqual(d["files"], 5000)
        self.assertEqual(d["total"], d["files"] + d["sessions"])


if __name__ == "__main__":
    unittest.main()
