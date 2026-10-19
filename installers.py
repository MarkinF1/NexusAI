"""Admin-triggered official CLI installers. Never read credentials or accept URLs from clients."""
import os
from pathlib import Path
import signal
import subprocess
import tempfile
import threading

SOURCES = {
    "claude": ("https://claude.ai/install.sh", "/bin/bash"),
    "codex": ("https://chatgpt.com/codex/install.sh", "/bin/sh"),
}
_lock = threading.Lock()
_states = {}
_processes = {}
_stopping = threading.Event()


def status(engine):
    if engine not in SOURCES:
        raise ValueError("Неизвестная нейросеть")
    with _lock:
        return dict(_states.get(engine, {"running": False, "complete": False, "error": ""}))


def environment():
    home = str(Path.home())
    # In particular: no bot/API tokens, session-specific CODEX_HOME or CLAUDE_CONFIG_DIR.
    allowed = ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY", "http_proxy", "https_proxy",
               "all_proxy", "no_proxy", "SSL_CERT_FILE", "SSL_CERT_DIR", "CURL_CA_BUNDLE")
    env = {key: os.environ[key] for key in allowed if key in os.environ}
    env.update(HOME=home, PATH=home + "/.local/bin:/usr/local/bin:/usr/bin:/bin",
               LANG="C.UTF-8", TERM="dumb", NO_COLOR="1", CI="1", CODEX_NON_INTERACTIVE="1",
               CODEX_INSTALL_DIR=home + "/.local/bin",
               # Keep packages out of optional host-mounted ~/.codex auth/config directories.
               CODEX_HOME=home + "/.local/share/nexus/codex")
    return env


def _execute(engine, args, env, timeout):
    with _lock:
        if _stopping.is_set():
            raise RuntimeError("Установка прервана остановкой сервиса")
        proc = subprocess.Popen(args, cwd=str(Path.home()), env=env, stdin=subprocess.DEVNULL,
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                start_new_session=True)
        _processes[engine] = proc
    try:
        try:
            code = proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            _kill(proc)
            proc.wait()
            raise RuntimeError("Истекло время ожидания установщика. Попробуйте ещё раз.") from None
        if code:
            raise RuntimeError(f"Установщик завершился с кодом {code}. Проверьте сеть, "
                               "свободное место и права пользователя сервиса, затем повторите установку.")
    finally:
        with _lock:
            _processes.pop(engine, None)


def _kill(proc):
    if proc.poll() is None:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass


def _worker(engine):
    import core
    error = ""
    try:
        url, shell = SOURCES[engine]
        env = environment()
        with tempfile.TemporaryDirectory(prefix="nexus-install-") as folder:
            script = str(Path(folder) / "install.sh")
            # Download completely before execution; never execute a partial response.
            _execute(engine, ["/usr/bin/curl", "--fail", "--silent", "--show-error", "--location",
                              "--proto", "=https", "--proto-redir", "=https", "--connect-timeout", "20",
                              "--max-time", "60", "--max-filesize", "2097152", "--output", script, url], env, 75)
            with _lock:
                _states[engine]["message"] = "Выполняется официальный установщик…"
            _execute(engine, [shell, script], env, 600)
        with _lock:
            _states[engine]["message"] = "Установлена. Проверяем подключение…"
        result = core.rescan_and_check(engine)
        if not result["installed"]:
            raise RuntimeError("Установщик завершился, но рабочий CLI не найден. Повторите установку.")
    except (OSError, RuntimeError, subprocess.SubprocessError):
        # Do not publish installer output or exception paths: they may include proxy credentials.
        error = "Не удалось установить CLI. Проверьте доступ к официальному сайту, свободное " \
                "место и права пользователя сервиса. Можно повторить установку."
        try:
            if not _stopping.is_set():
                core.rescan_and_check(engine)
        except Exception:
            pass
    except Exception:
        error = "Установка не завершена. Повторите проверку или установку."
    finally:
        with _lock:
            _states[engine] = {"running": False, "complete": not bool(error), "error": error,
                               "message": error or "CLI установлен"}


def start(engine):
    import core
    if engine not in SOURCES:
        raise ValueError("Неизвестная нейросеть")
    if os.getuid() == 0:
        raise ValueError("Запустите сервис от обычного пользователя: установка CLI от root запрещена")
    with _lock:
        if _stopping.is_set():
            raise ValueError("Сервис останавливается")
        if _states.get(engine, {}).get("running"):
            return dict(_states[engine])
        if core.installed(engine):
            return {"running": False, "complete": True, "error": "", "message": "CLI уже установлен"}
        state = {"running": True, "complete": False, "error": "",
                 "message": "Скачивается официальный установщик…"}
        _states[engine] = state
        thread = threading.Thread(target=_worker, args=(engine,), daemon=True)
        try:
            thread.start()
        except RuntimeError:
            _states.pop(engine, None)
            raise
        return dict(state)


def stop():
    with _lock:
        _stopping.set()
        processes = list(_processes.values())
    for proc in processes:
        _kill(proc)
