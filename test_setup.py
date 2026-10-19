"""Проверки установщика без APT, сети, настоящих CLI и изменения службы пользователя."""
import contextlib
import io
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch
import urllib.error

import setup_site


class Setup(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.folder = Path(self.temp.name) / 'проект с пробелом 100% $HOME @APP@'
        self.folder.mkdir()
        shutil.copyfile(Path(setup_site.__file__).with_name("nexus-web.service"), self.folder / "nexus-web.service")
        (self.folder / ".env.example").write_text("NEXUS_HOST=127.0.0.1\nNEXUS_PORT=8080\n", encoding="utf-8")

    def test_unit_paths_and_migration(self):
        template = (self.folder / "nexus-web.service").read_text()
        config = Path(self.temp.name) / "config"
        unit = config / "systemd/user/nexus-web.service"
        unit.parent.mkdir(parents=True)
        unit.symlink_to(self.folder / "nexus-web.service")
        self.assertTrue(setup_site.write_service(self.folder, config))
        self.assertFalse(unit.is_symlink())
        self.assertEqual((self.folder / "nexus-web.service").read_text(), template)
        text = unit.read_text()
        self.assertIn('100%% $HOME @APP@', text)
        self.assertIn('100%% $$HOME @APP@', text)  # ExecStart: не раскрывать $HOME
        self.assertNotIn('/opt/NexusAI', text)
        self.assertNotIn('WorkingDirectory=@PROJECT@', text)
        self.assertFalse(setup_site.write_service(self.folder, config))
        unit.unlink()
        unit.symlink_to(self.folder / "missing")
        self.assertTrue(setup_site.write_service(self.folder, config))

    def test_unit_with_systemd_parser(self):
        analyzer = shutil.which("systemd-analyze")
        if not analyzer or not Path(f"/run/user/{os.getuid()}").exists():
            self.skipTest("systemd user runtime недоступен")
        binary = self.folder / ".venv/bin/python"
        binary.parent.mkdir(parents=True)
        binary.symlink_to(sys.executable)
        (self.folder / "app.py").touch()
        (self.folder / ".env").touch()
        config = Path(self.temp.name) / "verify"
        setup_site.write_service(self.folder, config)
        result = subprocess.run([analyzer, "--user", "verify", str(config / "systemd/user/nexus-web.service")],
                                text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("ignoring", result.stderr)

    def test_unit_rejects_multiline_path(self):
        with self.assertRaises(ValueError):
            setup_site.quote_unit('project\nExecStart=/bin/false')

    def test_data_directory_and_env_are_data_not_shell(self):
        (self.folder / ".env").write_text('NEXUS_DATA="my files"\nIGNORED=$(touch bad)\n', encoding="utf-8")
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(setup_site.data_dir(self.folder), self.folder / "my files")
            self.assertEqual(setup_site.env_values(self.folder)["IGNORED"], "$(touch bad)")
        with patch.dict(os.environ, NEXUS_DATA="/other/data"):
            self.assertEqual(setup_site.data_dir(self.folder), Path("/other/data"))

    def test_configure_preserves_existing_and_validates_input(self):
        answers = ['not-an-address', '0.0.0.0', 'oops', '80', '9000']
        with patch.object(setup_site, "HERE", self.folder), patch("builtins.input", side_effect=answers), \
                patch.object(setup_site.socket, "socket"), contextlib.redirect_stdout(io.StringIO()):
            setup_site.configure()
        path = self.folder / ".env"
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        content = path.read_text()
        self.assertIn("NEXUS_HOST=0.0.0.0", content)
        self.assertIn("NEXUS_PORT=9000", content)
        with patch.object(setup_site, "HERE", self.folder), patch("builtins.input", side_effect=AssertionError("asked again")), \
                contextlib.redirect_stdout(io.StringIO()):
            setup_site.configure()
        self.assertEqual(path.read_text(), content)

    def test_admin_retry_and_existing_admin(self):
        users = Mock()
        users.listing.return_value = []
        output = io.StringIO()
        with patch.dict(sys.modules, users=users), patch.object(setup_site, "load_environment"), \
                patch("builtins.input", side_effect=["owner", "owner"]), \
                patch.object(setup_site.getpass, "getpass", side_effect=["wrong", "different", "secret", "secret"]), \
                contextlib.redirect_stdout(output):
            setup_site.ensure_admin()
        users.add.assert_called_once_with("owner", "secret", admin=True)
        self.assertNotIn("secret", output.getvalue())
        users.listing.return_value = [{"is_admin": True}]
        with patch.dict(sys.modules, users=users), patch.object(setup_site, "load_environment"), \
                patch("builtins.input", side_effect=AssertionError("asked again")), contextlib.redirect_stdout(io.StringIO()):
            setup_site.ensure_admin()

    def test_wait_local_401_and_failure(self):
        opener = Mock()
        opener.open.side_effect = urllib.error.HTTPError("url", 401, "login", {}, None)
        with patch.object(setup_site, "load_environment"), patch.dict(os.environ, NEXUS_HOST="::", NEXUS_PORT="8080"), \
                patch.object(setup_site.urllib.request, "build_opener", return_value=opener), \
                contextlib.redirect_stdout(io.StringIO()):
            setup_site.wait_ready()
        self.assertEqual(opener.open.call_args.args[0], "http://[::1]:8080/api/me")
        with patch.object(setup_site, "load_environment"):
            with self.assertRaises(SystemExit):
                setup_site.wait_ready(timeout=0)

    def test_shell_syntax_and_no_cli_installer(self):
        root = Path(setup_site.__file__).parent
        for name in ("start.sh", "update.sh"):
            subprocess.run(["bash", "-n", str(root / name)], check=True)
        start = (root / "start.sh").read_text()
        self.assertNotIn("npm install", start)
        self.assertNotIn("claude.ai/install", start)
        self.assertNotIn("/opt/NexusAI", start)

    def test_start_flow_with_mocked_system_commands(self):
        if os.geteuid() == 0 or not Path("/run/systemd/system").exists():
            self.skipTest("Проверка пользовательского запуска на Debian/systemd")
        release = Path("/etc/os-release").read_text()
        if 'ID=debian' not in release or 'VERSION_ID="13"' not in release:
            self.skipTest("Проверка установщика Debian 13")
        source = Path(setup_site.__file__).with_name("start.sh")
        shutil.copyfile(source, self.folder / "start.sh")
        binary = self.folder / ".venv/bin/python"
        binary.parent.mkdir(parents=True)
        binary.write_text('''#!/bin/bash
if [[ $1 == -c ]]; then
  printf '%s/.venv\\n' "$PWD"
else
  printf 'python %s\\n' "$*" >> "$NEXUS_TEST_LOG"
fi
''')
        binary.chmod(0o755)
        log = self.folder / "commands.log"
        env = dict(os.environ, NEXUS_TEST_LOG=str(log))
        functions = {
            "dpkg-query": 'return 1',
            "sudo": 'printf "sudo %s\\n" "$*" >> "$NEXUS_TEST_LOG"',
            "systemctl": 'printf "systemctl %s\\n" "$*" >> "$NEXUS_TEST_LOG"',
            "loginctl": 'printf "yes\\n"',
            "bwrap": 'printf "bwrap checked\\n" >> "$NEXUS_TEST_LOG"',
        }
        for name, body in functions.items():
            env[f"BASH_FUNC_{name}%%"] = "() { " + body + "; }"
        result = subprocess.run(["bash", str(self.folder / "start.sh")], env=env,
                                text=True, capture_output=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        commands = log.read_text()
        self.assertIn("sudo apt-get install", commands)
        self.assertIn("python setup_site.py admin", commands)
        self.assertIn("python setup_site.py service", commands)
        self.assertIn("systemctl --user restart nexus-web.service", commands)
        self.assertIn("python setup_site.py wait", commands)
        self.assertNotIn("npm install", commands)
        env["BASH_FUNC_sudo%%"] = "() { return 17; }"
        result = subprocess.run(["bash", str(self.folder / "start.sh")], env=env,
                                text=True, capture_output=True, timeout=10)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Установка остановлена", result.stderr)


if __name__ == "__main__":
    unittest.main()
