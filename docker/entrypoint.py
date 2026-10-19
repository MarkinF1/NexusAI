"""Container bootstrap, namespace preflight and HTTP healthcheck; no token reads."""
import os
from pathlib import Path
import subprocess
import sys
import urllib.error
import urllib.request

APP = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(APP))


def health():
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open("http://127.0.0.1:8080/api/me", timeout=3) as response:
            return response.status == 200
    except urllib.error.HTTPError as exc:
        exc.close()
        return exc.code == 401
    except OSError:
        return False


def preflight():
    if os.getuid() == 0:
        raise SystemExit("NexusAI должен запускаться от непривилегированного пользователя.")
    for path in (Path(os.environ.get("NEXUS_DATA", "/data")), Path.home(),
                 Path.home() / ".claude", Path.home() / ".codex"):
        path.mkdir(parents=True, exist_ok=True)
        if not os.access(path, os.R_OK | os.W_OK | os.X_OK):
            raise SystemExit(f"Нет доступа к {path}. Проверьте UID/GID и права подключённой папки.")
    # Test the namespaces actually needed by both CLI sandboxes, not just bwrap --version.
    result = subprocess.run([
        "bwrap", "--unshare-user", "--unshare-pid", "--unshare-net", "--new-session",
        "--die-with-parent", "--ro-bind", "/", "/", "--proc", "/proc", "--dev", "/dev",
        "/bin/true"], capture_output=True, text=True, timeout=15)
    if result.returncode:
        print(result.stderr.strip(), file=sys.stderr)
        raise SystemExit("Песочница не работает. Проверьте user namespaces, seccomp и AppArmor; "
                         "изоляция автоматически не отключается. См. раздел Docker в README.")


def has_admin():
    import users
    return any(user["is_admin"] for user in users.listing())


def main():
    os.chdir(APP)
    action = sys.argv[1] if len(sys.argv) > 1 else "serve"
    if action == "health":
        raise SystemExit(0 if health() else 1)
    if action == "setup":
        from setup_site import ensure_admin
        ensure_admin()
        return
    if action == "serve":
        preflight()
        if not has_admin():
            raise SystemExit("Сначала создайте администратора: ./docker.sh setup")
        os.execv(sys.executable, [sys.executable, str(APP / "app.py")])
        return
    # Maintenance commands run as the same non-root user, with the same volumes.
    os.execvp(sys.argv[1], sys.argv[1:])


if __name__ == "__main__":
    main()
