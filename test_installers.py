"""No real installer, tokens, network or installation into the test runner's home."""
import os
from pathlib import Path
import signal
import subprocess
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

import test_core  # shared isolated app fixture, not the running site's data
import app
import core
import installers


class Installers(unittest.TestCase):
    def setUp(self):
        self.states = dict(core._engine_states)
        installers._states.clear()
        installers._stopping.clear()

    def tearDown(self):
        installers._states.clear()
        installers._stopping.clear()
        installers._processes.clear()
        core._engine_states.clear()
        core._engine_states.update(self.states)

    def test_fixed_sources_and_clean_environment(self):
        with patch.dict(os.environ, {"OPENAI_API_KEY": "SECRET", "NEXUS_TG_TOKEN": "SECRET",
                                    "CLAUDE_CONFIG_DIR": "/other", "CODEX_HOME": "/other"}), \
                patch.object(installers.Path, "home", return_value=Path("/home/test user")):
            env = installers.environment()
        for key in ("OPENAI_API_KEY", "NEXUS_TG_TOKEN", "CLAUDE_CONFIG_DIR"):
            self.assertNotIn(key, env)
        self.assertEqual(env["CODEX_HOME"], "/home/test user/.local/share/nexus/codex")
        self.assertEqual(env["CODEX_NON_INTERACTIVE"], "1")
        self.assertEqual(installers.SOURCES["codex"], ("https://chatgpt.com/codex/install.sh", "/bin/sh"))
        self.assertEqual(installers.SOURCES["claude"], ("https://claude.ai/install.sh", "/bin/bash"))
        with self.assertRaises(ValueError):
            installers.start("https://attacker.invalid/install.sh")

    def test_duplicate_request_already_installed_and_root(self):
        with patch.object(installers.os, "getuid", return_value=1000), \
                patch.object(core, "installed", return_value=False), \
                patch.object(installers.threading, "Thread") as thread:
            first = installers.start("codex")
            second = installers.start("codex")
            self.assertEqual(first, second)
            self.assertTrue(first["running"])
            self.assertEqual(thread.return_value.start.call_count, 1)
        installers._states.clear()
        with patch.object(installers.os, "getuid", return_value=1000), \
                patch.object(core, "installed", return_value=True), \
                patch.object(installers.threading, "Thread") as thread:
            self.assertTrue(installers.start("codex")["complete"])
            thread.assert_not_called()
        with patch.object(installers.os, "getuid", return_value=0), self.assertRaises(ValueError):
            installers.start("claude")

    def test_worker_order_and_new_binary_discovery(self):
        installers._states["codex"] = {"running": True}
        steps = []
        def execute(engine, args, env, timeout):
            steps.append(args)
            if args[0] == "/usr/bin/curl":
                Path(args[args.index("--output") + 1]).write_text("#!/bin/sh\nexit 0\n")
            else:
                self.assertEqual(Path(args[1]).read_text(), "#!/bin/sh\nexit 0\n")
        with patch.object(installers, "_execute", side_effect=execute), \
                patch.object(core, "rescan_and_check", return_value={"installed": True}) as check:
            installers._worker("codex")
        self.assertEqual([args[0] for args in steps], ["/usr/bin/curl", "/bin/sh"])
        self.assertIn("=https", steps[0])
        check.assert_called_once_with("codex")
        self.assertTrue(installers.status("codex")["complete"])
        self.assertFalse(Path(steps[1][1]).exists())

    def test_failed_download_never_executes_partial_script_and_can_retry(self):
        installers._states["claude"] = {"running": True}
        with patch.object(installers, "_execute", side_effect=RuntimeError("SECRET")) as execute, \
                patch.object(core, "rescan_and_check"):
            installers._worker("claude")
        self.assertEqual(execute.call_count, 1)
        state = installers.status("claude")
        self.assertFalse(state["running"])
        self.assertFalse(state["complete"])
        self.assertTrue(state["error"])
        self.assertNotIn("SECRET", state["error"])
        with patch.object(installers.os, "getuid", return_value=1000), \
                patch.object(core, "installed", return_value=False), \
                patch.object(installers.threading, "Thread"):
            self.assertTrue(installers.start("claude")["running"])

    def test_missing_binary_after_success_is_failure(self):
        installers._states["codex"] = {"running": True}
        with patch.object(installers, "_execute"), \
                patch.object(core, "rescan_and_check", return_value={"installed": False}):
            installers._worker("codex")
        self.assertFalse(installers.status("codex")["complete"])

    def test_timeout_and_shutdown_kill_process_group(self):
        process = Mock(pid=123456, poll=Mock(return_value=None))
        process.wait.side_effect = [subprocess.TimeoutExpired("sh", 1), 0]
        with patch.object(installers.subprocess, "Popen", return_value=process) as popen, \
                patch.object(installers.os, "killpg") as kill, self.assertRaises(RuntimeError):
            installers._execute("claude", ["/bin/sh", "script"], {}, 1)
        kill.assert_called_once_with(123456, signal.SIGKILL)
        self.assertTrue(popen.call_args.kwargs["start_new_session"])
        self.assertEqual(popen.call_args.kwargs["stdin"], subprocess.DEVNULL)
        self.assertEqual(installers._processes, {})
        installers._processes["codex"] = process
        with patch.object(installers.os, "killpg") as kill:
            installers.stop()
            kill.assert_called_once()

    def test_status_install_error_and_login_in_progress(self):
        core._engine_states["codex"] = {"engine": "codex", "installed": False,
            "state": "missing", "available": False, "login": False}
        installers._states["codex"] = {"running": True, "message": "Downloading"}
        state = next(s for s in core.engine_statuses() if s["engine"] == "codex")
        self.assertEqual(state["state"], "installing")
        self.assertFalse(state["available"])
        installers._states["codex"] = {"running": False, "error": "Failed"}
        state = next(s for s in core.engine_statuses() if s["engine"] == "codex")
        self.assertEqual(state["state"], "missing")
        self.assertIn("Failed", state["message"])
        installers._states.clear()
        core._engine_states["codex"]["installed"] = True
        with patch.dict(core._login_sessions, {"codex": {"running": True}}):
            state = next(s for s in core.engine_statuses() if s["engine"] == "codex")
        self.assertEqual(state["state"], "authorizing")

    def test_rescan_invalidates_model_cache(self):
        with patch.object(core, "CODEX_BIN", None), patch.object(core, "_codex_version_cache", "old"), \
                patch.object(core, "newest_executable", return_value="/new/codex"), \
                patch.object(core, "check_engine", return_value={"state": "login"}):
            self.assertEqual(core.rescan_and_check("codex")["state"], "login")
            self.assertEqual(core.CODEX_BIN, "/new/codex")
            self.assertIsNone(core._codex_version_cache)

    def test_install_api_admin_only_and_validation(self):
        routes = [r for r in app.app.routes if r.path == "/api/engines/{engine}/install"]
        self.assertEqual(len(routes), 2)
        for route in routes:
            self.assertIn(app.admin_user, [d.call for d in route.dependant.dependencies])
        with self.assertRaises(app.HTTPException) as error:
            app.engine_install_state("unknown", user={})
        self.assertEqual(error.exception.status_code, 404)

    def test_login_completes_only_after_probe(self):
        session = {"proc": Mock(stdout=[], wait=Mock(return_value=0)), "running": True,
                   "condition": threading.Condition()}
        def check(engine):
            self.assertTrue(session["running"])
            self.assertNotIn("complete", session)
        with patch.object(core, "check_engine", side_effect=check):
            core._watch_login("codex", session)
        self.assertTrue(session["complete"])
        self.assertFalse(session["running"])

    def test_system_check_finds_removed_binary_instead_of_cached_available(self):
        with patch.object(core, "CODEX_BIN", "/old/codex"), \
                patch.object(core, "newest_executable", return_value=None), \
                patch.object(core, "_run_check") as probe:
            core._engine_states["codex"] = {"engine": "codex", "state": "available", "available": True}
            state = core.rescan_and_check("codex")
            self.assertEqual(state["state"], "missing")
            self.assertFalse(state["available"])
            probe.assert_not_called()

    def test_unreachable_cli_is_not_mistaken_for_logged_out(self):
        with patch.object(core, "installed", return_value=True), \
                patch.object(core, "_run_check", return_value=(-1, "timeout")):
            state = core.check_engine("codex")
            self.assertEqual(state["state"], "error")
            self.assertFalse(state["login"])

    def test_new_probe_replaces_old_auth_and_availability(self):
        for replies, expected in (([(1, "not logged in")], "login"),
                                  ([(0, "logged in"), (0, "OK")], "available"),
                                  ([(0, "logged in"), (1, "usage limit exceeded")], "limited")):
            core._engine_states["codex"] = {"engine": "codex", "state": "available", "available": True}
            with patch.object(core, "CODEX_BIN", "/fake/codex"), \
                    patch.object(core, "newest_executable", return_value="/fake/codex"), \
                    patch.object(core, "installed", return_value=True), \
                    patch.object(core, "_run_check", side_effect=replies):
                self.assertEqual(core.rescan_and_check("codex")["state"], expected)


if __name__ == "__main__":
    unittest.main()
