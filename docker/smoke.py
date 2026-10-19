"""Local Linux Docker smoke test. Uses only disposable data and dummy credentials.

Build first, then run: python3 docker/smoke.py
Never mounts existing chats/auth and never stops the native service.
"""
import http.cookiejar
import argparse
import json
import os
from pathlib import Path
import secrets
import socket
import subprocess
import tempfile
import time
import urllib.error
import urllib.request

HERE = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--install", action="store_true", help="Test real official CLI installers in disposable volumes")
    args = parser.parse_args()
    project = "nexusai-smoke-" + secrets.token_hex(4)
    env = {key: value for key, value in os.environ.items()
           if not key.startswith(("NEXUS_", "COMPOSE_"))}
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    env.update(NEXUS_DOCKER_PORT=str(port), NEXUS_DOCKER_BIND="127.0.0.1")
    command = ["docker", "compose", "--env-file", "docker.env.example", "-p", project,
               "-f", "compose.yaml"]

    def compose(*args, input=None, auth=False):
        base = command + (["-f", "compose.auth.yaml"] if auth else [])
        result = subprocess.run([*base, *args], cwd=HERE, env=env, input=input,
                                capture_output=True, text=True, timeout=300)
        if result.returncode:
            raise RuntimeError(result.stdout + result.stderr)
        return result.stdout

    # Only resources belonging to this random test project are removed.
    try:
        with tempfile.TemporaryDirectory(prefix="nexusai-dummy-auth-") as folder:
            root = Path(folder)
            for name in ("claude", "codex"):
                (root / name).mkdir()
            (root / "claude/.credentials.json").write_text("dummy-not-a-token")
            (root / "codex/auth.json").write_text("dummy-not-a-token")
            env.update(NEXUS_CLAUDE_DIR=str(root / "claude"), NEXUS_CODEX_DIR=str(root / "codex"))
            # Uses the host UID/GID for dummy bind mounts; image default is 1000.
            compose("run", "--rm", "-T", "--user", f"{os.getuid()}:{os.getgid()}", "nexus", "python", "-c",
                    "from pathlib import Path; "
                    "p=Path.home()/'.claude/.credentials.json'; "
                    "assert p.read_text()=='dummy-not-a-token'; "
                    "q=p.with_suffix('.new'); q.write_text('dummy-refreshed'); q.replace(p); "
                    "assert (Path.home()/'.codex/auth.json').read_text()=='dummy-not-a-token'",
                    auth=True)
            assert (root / "claude/.credentials.json").read_text() == "dummy-refreshed"
        compose("run", "--rm", "-T", "nexus", "python", "-c",
                "import sys; sys.path.insert(0, '/app/docker'); import entrypoint; entrypoint.preflight()")
        compose("run", "--rm", "-T", "nexus", "python", "-c",
                "import os; from pathlib import Path; "
                "assert os.getuid()!=0; "
                "assert not Path('/app/.env').exists(); assert not Path('/app/data').exists(); "
                "assert not Path('/app/.git').exists(); "
                "status=Path('/proc/self/status').read_text(); "
                "assert 'CapEff:\\t0000000000000000' in status; "
                "assert 'NoNewPrivs:\\t1' in status")
        compose("run", "--rm", "-T", "nexus", "python", "-c",
                "import subprocess; result=subprocess.run(['bwrap', '--unshare-user', "
                "'--ro-bind', '/', '/', '/bin/touch', '/tmp/nexus-smoke-forbidden'], "
                "capture_output=True); assert result.returncode != 0; "
                "assert b'Read-only file system' in result.stderr")
        password = secrets.token_urlsafe(24)
        output = compose("run", "--rm", "-T", "nexus", "setup",
                         input=f"smoke\n{password}\n{password}\n")
        assert password not in output
        assert "Администратор уже существует" in compose("run", "--rm", "-T", "nexus", "setup", input="")
        compose("run", "--rm", "-T", "nexus", "python", "-c",
                "from pathlib import Path; Path('/data/smoke-marker').write_text('persistent'); "
                "Path.home().joinpath('.local/bin/smoke-marker').write_text('persistent')")
        compose("up", "-d", "--no-build", "--wait", "--wait-timeout", "180")
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}),
                     urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
        url = f"http://127.0.0.1:{port}"
        with opener.open(url) as response:
            assert response.status == 200
        try:
            opener.open(urllib.request.Request(url + "/api/engines/codex/install", data=b""))
        except urllib.error.HTTPError as exc:
            assert exc.code == 401
            exc.close()
        else:
            raise AssertionError("Unauthenticated installation was allowed")
        request = urllib.request.Request(url + "/api/login",
                    data=json.dumps({"name": "smoke", "password": password}).encode(),
                    headers={"Content-Type": "application/json"})
        with opener.open(request) as response:
            assert json.load(response)["is_admin"]
        with opener.open(url + "/api/engines") as response:
            engines = json.load(response)
            assert "claude" in str(engines) and "codex" in str(engines)
        if args.install:
            for engine in ("claude", "codex"):
                with opener.open(urllib.request.Request(url + f"/api/engines/{engine}/install", data=b"")) as response:
                    assert json.load(response)["running"]
            remaining = {"claude", "codex"}
            deadline = time.monotonic() + 900
            phases = {}
            while remaining and time.monotonic() < deadline:
                for engine in list(remaining):
                    with opener.open(url + f"/api/engines/{engine}/install") as response:
                        state = json.load(response)
                    if phases.get(engine) != state.get("message"):
                        print(engine + ": " + state.get("message", ""), flush=True)
                        phases[engine] = state.get("message")
                    if not state["running"]:
                        assert state["complete"], state
                        remaining.remove(engine)
                time.sleep(1)
            assert not remaining, "Installation timeout"
            with opener.open(url + "/api/engines") as response:
                for state in json.load(response):
                    assert state["installed"] and state["state"] == "login", state
            print("PASS: official installers through admin API; both CLIs installed and need login", flush=True)
        compose("up", "-d", "--no-build", "--force-recreate", "--wait", "--wait-timeout", "180")
        compose("exec", "-T", "nexus", "python", "-c",
                "from pathlib import Path; "
                "assert Path('/data/smoke-marker').read_text()=='persistent'; "
                "assert Path.home().joinpath('.local/bin/smoke-marker').read_text()=='persistent'; "
                "import users; assert any(u['name']=='smoke' and u['is_admin'] for u in users.listing())")
        with opener.open(request) as response:
            assert json.load(response)["is_admin"]
        print("PASS: dummy auth mounts/atomic refresh, namespaces, admin, HTTP login, "
              "engine list, recreate and data/home persistence")
        if args.install:
            with opener.open(url + "/api/engines") as response:
                assert all(state["installed"] for state in json.load(response))
            compose("exec", "-T", "nexus", "sh", "-c", "claude --version && codex --version")
    finally:
        compose("down", "--volumes", "--remove-orphans")
        print("Removed disposable test containers, network and volumes: " + project)


if __name__ == "__main__":
    main()
