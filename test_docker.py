"""Docker bootstrap tests without Docker daemon, host auth or privileged operations."""
import contextlib
import importlib.util
import io
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import MagicMock, Mock, patch
import urllib.error

HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location("container_entrypoint", HERE / "docker/entrypoint.py")
entry = importlib.util.module_from_spec(spec)
spec.loader.exec_module(entry)


class ContainerTests(unittest.TestCase):
    def test_health_authorized_or_unauthorized(self):
        opener = MagicMock()
        opener.open.return_value.__enter__.return_value.status = 200
        with patch.object(entry.urllib.request, "build_opener", return_value=opener):
            self.assertTrue(entry.health())
            opener.open.assert_called_with("http://127.0.0.1:8080/api/me", timeout=3)
            for code, expected in ((401, True), (403, False), (500, False)):
                opener.open.side_effect = urllib.error.HTTPError("url", code, "test", {}, None)
                self.assertEqual(entry.health(), expected)
            opener.open.side_effect = OSError("offline")
            self.assertFalse(entry.health())

    def test_preflight_namespaces_and_directories(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            with patch.dict(os.environ, {"NEXUS_DATA": str(root / "data")}), \
                    patch.object(entry.Path, "home", return_value=root / "home"), \
                    patch.object(entry.os, "getuid", return_value=1000), \
                    patch.object(entry.subprocess, "run", return_value=Mock(returncode=0)) as run:
                entry.preflight()
                self.assertTrue((root / "home/.claude").is_dir())
                self.assertTrue((root / "home/.codex").is_dir())
                command = run.call_args.args[0]
                for flag in ("--unshare-user", "--unshare-net", "--unshare-pid", "--ro-bind"):
                    self.assertIn(flag, command)
                run.return_value = Mock(returncode=1, stderr="namespace denied")
                with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                    entry.preflight()

    def test_preflight_refuses_root(self):
        with patch.object(entry.os, "getuid", return_value=0), \
                patch.object(entry.subprocess, "run") as run, self.assertRaises(SystemExit):
            entry.preflight()
        run.assert_not_called()

    def test_bootstrap_admin_required_and_exec(self):
        with patch.object(entry.os, "chdir"), patch.object(entry, "preflight") as check, \
                patch.object(entry, "has_admin", return_value=False) as admin, \
                patch.object(entry.os, "execv") as execute, patch.object(entry.sys, "argv", ["entry", "serve"]):
            with self.assertRaisesRegex(SystemExit, "docker.sh setup"):
                entry.main()
            execute.assert_not_called()
            admin.return_value = True
            entry.main()
            execute.assert_called_once_with(entry.sys.executable, [entry.sys.executable, str(entry.APP / "app.py")])
            self.assertEqual(check.call_count, 2)

    def test_health_and_maintenance_do_not_bootstrap(self):
        with patch.object(entry.os, "chdir"), patch.object(entry, "preflight") as check, \
                patch.object(entry, "health", return_value=True), \
                patch.object(entry.sys, "argv", ["entry", "health"]):
            with self.assertRaises(SystemExit) as error:
                entry.main()
            self.assertEqual(error.exception.code, 0)
            check.assert_not_called()
        with patch.object(entry.os, "chdir"), patch.object(entry.os, "execvp") as execute, \
                patch.object(entry.sys, "argv", ["entry", "bash", "-l"]):
            entry.main()
            execute.assert_called_once_with("bash", ["bash", "-l"])

    def test_image_and_compose_safety_contract(self):
        image = (HERE / "Dockerfile").read_text()
        compose = (HERE / "compose.yaml").read_text()
        ignore = (HERE / ".dockerignore").read_text()
        self.assertIn("USER nexus:nexus", image)
        self.assertNotIn("COPY . ", image)
        self.assertNotIn("npm install", image)
        self.assertNotIn("claude.ai/install", image)
        self.assertTrue(ignore.splitlines()[1].startswith("**"))
        self.assertNotIn("!.env", ignore)
        self.assertNotIn("!data/", ignore)
        for forbidden in ("privileged: true", "SYS_ADMIN]", "/var/run/docker.sock", "network_mode: host"):
            self.assertNotIn(forbidden, compose)
        self.assertIn("cap_drop: [ALL]", compose)
        self.assertIn("systempaths=unconfined", compose)
        self.assertIn("home:/home/nexus", compose)
        self.assertIn("data:/data", compose)
        self.assertEqual((HERE / "compose.auth.yaml").read_text().count("create_host_path: false"), 2)

    @unittest.skipUnless(shutil.which("bash"), "Bash is required")
    def test_wrapper_isolated_config_and_commands(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            for name in ("docker.sh", "docker.env.example"):
                shutil.copyfile(HERE / name, root / name)
            # An exported function intercepts every Docker call; no daemon used.
            script = '''docker() { printf '%s\\n' "$*" >> "$CHECK_LOG"; }; export -f docker
bash "$CHECK_SCRIPT" "$@"'''
            env = dict(os.environ, CHECK_LOG=str(root / "calls"), CHECK_SCRIPT=str(root / "docker.sh"))
            def invoke(*args):
                return subprocess.run(["bash", "-c", script, "check", *args], env=env,
                                      text=True, capture_output=True)
            result = invoke("start")
            self.assertEqual(result.returncode, 0, result.stderr)
            calls = (root / "calls").read_text()
            self.assertIn("--env-file .env.docker -f compose.yaml build", calls)
            self.assertIn("run --rm --no-deps nexus setup", calls)
            self.assertIn("up -d --wait", calls)
            self.assertEqual((root / ".env.docker").stat().st_mode & 0o777, 0o600)
            original = (root / ".env.docker").read_text()
            result = invoke("--auth", "stop")
            self.assertEqual(result.returncode, 0, result.stderr)
            calls = (root / "calls").read_text()
            self.assertIn("-f compose.yaml -f compose.auth.yaml down", calls)
            self.assertNotIn("down -v", calls)
            self.assertEqual((root / ".env.docker").read_text(), original)
            self.assertEqual(invoke("invalid").returncode, 2)
            self.assertEqual(invoke("start", "extra").returncode, 2)
            env["NEXUS_DOCKER_ENV"] = str(root / "missing")
            self.assertNotEqual(invoke("status").returncode, 0)
            self.assertFalse((root / "missing").exists())


if __name__ == "__main__":
    unittest.main()
