"""Backup/restore round trips; no real services, user data or network."""
from contextlib import closing
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import tarfile
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location("backup_script", ROOT / "tools/create_backup.py")
backup = importlib.util.module_from_spec(spec)
spec.loader.exec_module(backup)


class Backup(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.folder = Path(self.temp.name) / "проект с пробелом"
        (self.folder / "tools").mkdir(parents=True)
        for name in ("create_backup.sh", "setup_site.py", "tools/create_backup.py"):
            shutil.copy2(ROOT / name, self.folder / name)
        self.data = self.folder / "данные"
        self.data.mkdir()
        (self.folder / ".env").write_text('NEXUS_DATA="данные"\nIGNORED=$(touch bad)\n')
        self.bin = Path(self.temp.name) / "bin"
        self.bin.mkdir()
        stub = self.bin / "systemctl"
        stub.write_text("#!/bin/sh\nexit 3\n")
        stub.chmod(0o755)
        self.env = {**os.environ, "PATH": str(self.bin) + os.pathsep + os.environ["PATH"]}
        self.env.pop("NEXUS_DATA", None)
        with closing(sqlite3.connect(self.data / "nexus.db")) as db:
            db.executescript("CREATE TABLE users (id INTEGER PRIMARY KEY, name TEXT, pw TEXT);"
                             "CREATE TABLE logins(token TEXT, user_id INTEGER);"
                             "CREATE TABLE tg_groups(chat_id INTEGER, owner INTEGER);")
            db.executemany("INSERT INTO users VALUES (?,?,?)", [(1,"one","hash1"),(8,"eight","hash8"),(77,"new","hash77")])
            db.execute("INSERT INTO logins VALUES ('browser-session',77)")
            db.execute("INSERT INTO tg_groups VALUES (-100,77)")
            db.commit()
        for uid in (1,8,77):
            for name, content in {
                "chats/"+f"{uid:032x}"+".json": json.dumps({"id": f"{uid:032x}", "title":"Беседа"}),
                "chats/"+f"{uid:032x}"+".jsonl": '{"kind":"user","text":"привет"}\n',
                "data/MEMORY.md": "Память пользователя",
                "data/library/.env": "USER_FILE=keep\nOPENAI_API_KEY=user-owned-project-file\n",
                "tg.json": '{"chat":123}', "push.json": '[{"endpoint":"example"}]',
                "usage.json": '{"total":42}',
            }.items():
                p=self.data/"users"/str(uid)/name
                p.parent.mkdir(parents=True,exist_ok=True)
                p.write_text(content)
        library=self.data/"users/77/data/library/image.bin"
        library.write_bytes(bytes(range(256))*20)
        linked=self.data/"users/77/data/workspaces/chat/files/image.bin"
        linked.parent.mkdir(parents=True)
        os.link(library,linked)
        (library.parent/"relative-link").symlink_to("image.bin")
        auth=self.data/"users/77/codex-home/auth.json"
        auth.parent.mkdir()
        auth.symlink_to(Path(self.temp.name)/"DO_NOT_READ")
        (auth.parent/"logs_2.sqlite").write_text("runtime log")
        (self.data/"settings.json").write_text('{"tg_token":"telegram-token","timeout":123}')
        (self.data/"vapid.json").write_text('{"private":"push-private-key","public":"push-public-key"}')
        (self.data/"busy.json").write_text('{"pid":2147483647,"jobs":1}')
        (self.data/"engine").mkdir()
        (self.data/"engine/cache").write_text("runtime")

    def run_backup(self,*args,ok=True,env=None):
        result=subprocess.run([str(self.folder/"create_backup.sh"),*map(str,args)],cwd=self.temp.name,
                              env=env or self.env,capture_output=True,text=True)
        if ok:self.assertEqual(result.returncode,0,result.stderr)
        else:self.assertNotEqual(result.returncode,0)
        return result

    def test_default_round_trip_all_accounts_files_and_settings(self):
        self.run_backup()
        archive=self.folder/"backup.tar.gz"
        self.assertEqual(archive.stat().st_mode & 0o777,0o600)
        restored=Path(self.temp.name)/"restored"
        restored.mkdir()
        # Fixture has only an internal symlink. GNU tar preserves hardlinks as on restore.
        subprocess.run(["tar","--no-same-owner","-xzf",str(archive),"-C",str(restored)],check=True)
        manifest=json.loads((restored/"manifest.json").read_text())
        self.assertEqual((manifest["format"],manifest["version"]),("nexusai-data-backup",1))
        self.assertEqual(manifest["counts"],{"users":3,"chats":3})
        for entry in manifest["files"]:
            p=restored/entry["path"]
            if entry["type"]=="file":
                self.assertEqual(hashlib.sha256(p.read_bytes()).hexdigest(),entry["sha256"])
            elif entry["type"]=="symlink":self.assertEqual(os.readlink(p),entry["target"])
            else:self.assertTrue(p.is_dir())
        with closing(sqlite3.connect(restored/"data/nexus.db")) as db:
            self.assertEqual(db.execute("PRAGMA integrity_check").fetchone(),("ok",))
            self.assertEqual(db.execute("SELECT id,pw FROM users ORDER BY id").fetchall(),[(1,"hash1"),(8,"hash8"),(77,"hash77")])
            self.assertEqual(db.execute("SELECT * FROM logins").fetchall(),[("browser-session",77)])
            self.assertEqual(db.execute("SELECT * FROM tg_groups").fetchall(),[(-100,77)])
        for name in ("settings.json","vapid.json","users/77/data/MEMORY.md","users/77/data/library/.env","users/77/tg.json","users/77/push.json"):
            self.assertEqual((restored/"data"/name).read_bytes(),(self.data/name).read_bytes())
        a=restored/"data/users/77/data/library/image.bin"
        b=restored/"data/users/77/data/workspaces/chat/files/image.bin"
        self.assertEqual(a.stat().st_ino,b.stat().st_ino)
        for name in ("busy.json","engine",".backup.lock","users/77/codex-home/auth.json","users/77/codex-home/logs_2.sqlite"):
            self.assertFalse((restored/"data"/name).exists())
        self.assertFalse((Path(self.temp.name)/"bad").exists())
        self.assertFalse(list(self.folder.glob(".nexus-backup-*")))

    def test_custom_destination_name_and_latest_user_in_wal(self):
        connection=sqlite3.connect(self.data/"nexus.db")
        self.addCleanup(connection.close)
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("INSERT INTO users VALUES (123,'added now','new hash')")
        connection.commit()
        self.run_backup("./backups", "-o", "backup1.tar.gz")
        archive=Path(self.temp.name)/"backups/backup1.tar.gz"
        with tarfile.open(archive) as tar:
            manifest=json.load(tar.extractfile("manifest.json"))
            self.assertEqual(manifest["counts"]["users"],4)
            dbfile=Path(self.temp.name)/"wal-restored.db"
            dbfile.write_bytes(tar.extractfile("data/nexus.db").read())
            self.assertNotIn("data/nexus.db-wal",tar.getnames())
        with closing(sqlite3.connect(dbfile)) as db:
            self.assertEqual(db.execute("SELECT pw FROM users WHERE id=123").fetchone(),("new hash",))

    def test_existing_archive_and_destination_inside_data_are_rejected(self):
        existing=self.folder/"backup.tar.gz"
        existing.write_bytes(b"previous backup")
        self.run_backup(ok=False)
        self.assertEqual(existing.read_bytes(),b"previous backup")
        self.run_backup(self.data/"backups",ok=False)
        self.assertFalse((self.data/"backups").exists())

    def test_environment_override_and_failed_snapshot_cleanup(self):
        moved=self.folder/"external data"
        self.data.rename(moved)
        result=self.run_backup("-o","override.tgz",env={**self.env,"NEXUS_DATA":str(moved)})
        self.assertIn("override.tgz",result.stdout)
        os.mkfifo(moved/"pipe")
        self.run_backup("-o","failed.tar.gz",ok=False,env={**self.env,"NEXUS_DATA":str(moved)})
        self.assertFalse((self.folder/"failed.tar.gz").exists())
        self.assertFalse(list(self.folder.glob("*.partial")))

    def test_running_service_restarts_when_snapshot_fails(self):
        service=unittest.mock.Mock()
        service.name="nexus-web.service"
        service.run.side_effect=lambda *args,**kwargs: {"ActiveState":"active","WorkingDirectory":str(self.folder),"MainPID":"123"}.get(args[2] if len(args)>2 else "","")
        (self.data/"busy.json").write_text('{"pid":123,"jobs":0,"transcribing":0}')
        with patch.object(backup,"PROJECT",self.folder),patch.object(backup,"Service",return_value=service),patch.object(backup.Path,"read_text",return_value="test-scope"):
            with self.assertRaisesRegex(RuntimeError,"snapshot failed"):
                with backup.stopped_service(self.data,0):
                    self.assertTrue((self.data/"draining").exists())
                    raise RuntimeError("snapshot failed")
        self.assertFalse((self.data/"draining").exists())
        service.run.assert_any_call("stop",check=True)
        service.run.assert_any_call("start",check=True)

    def test_busy_timeout_does_not_stop_or_leave_drain(self):
        service=unittest.mock.Mock()
        service.name="nexus-web.service"
        service.run.side_effect=lambda *args,**kwargs: {"ActiveState":"active","WorkingDirectory":str(self.folder),"MainPID":"123"}.get(args[2] if len(args)>2 else "","")
        (self.data/"busy.json").write_text('{"pid":123,"jobs":1,"transcribing":0}')
        with patch.object(backup,"PROJECT",self.folder),patch.object(backup,"Service",return_value=service),patch.object(backup.Path,"read_text",return_value="test-scope"):
            with self.assertRaisesRegex(RuntimeError,"Не дождался"):
                with backup.stopped_service(self.data,0):pass
        self.assertFalse((self.data/"draining").exists())
        self.assertNotIn(unittest.mock.call("stop",check=True),service.run.call_args_list)

    def test_concurrent_backup_and_unmanaged_live_process_are_rejected(self):
        with backup.backup_lock(self.data):
            result=self.run_backup(ok=False)
            self.assertIn("уже создаётся",result.stderr)
        app=self.folder/"app.py"
        app.write_text("import time\ntime.sleep(30)\n")
        process=subprocess.Popen([os.sys.executable,str(app)],cwd=self.folder)
        def stop():
            process.terminate()
            process.wait(timeout=5)
        self.addCleanup(stop)
        (self.data/"busy.json").write_text(json.dumps({"pid":process.pid,"jobs":0}))
        result=self.run_backup(ok=False)
        self.assertIn("работающий процесс",result.stderr)
        self.assertFalse((self.folder/"backup.tar.gz").exists())

    def test_existing_drain_is_preserved(self):
        service=unittest.mock.Mock()
        service.name="nexus-web.service"
        service.run.side_effect=lambda *args,**kwargs: {"ActiveState":"active","WorkingDirectory":str(self.folder),"MainPID":"123"}.get(args[2] if len(args)>2 else "","")
        (self.data/"draining").write_text("another operation")
        with patch.object(backup,"PROJECT",self.folder),patch.object(backup,"Service",return_value=service),patch.object(backup.Path,"read_text",return_value="test-scope"):
            with self.assertRaises(FileExistsError):
                with backup.stopped_service(self.data,0):pass
        self.assertEqual((self.data/"draining").read_text(),"another operation")
        self.assertNotIn(unittest.mock.call("stop",check=True),service.run.call_args_list)


if __name__=="__main__":
    unittest.main()
