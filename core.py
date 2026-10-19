"""Ядро: папки пользователей, беседы и запуск claude/codex со стримингом событий."""
import asyncio
import base64
import contextlib
import glob
import json
import os
import re
import shlex
import shutil
import signal
import sqlite3
import stat
import subprocess
import threading
import time
import uuid
from datetime import datetime
from pathlib import Path

import session_files
import settings

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.abspath(os.environ.get("NEXUS_DATA", os.path.join(HERE, "data")))
HOST_CREDS = os.path.expanduser("~/.claude/.credentials.json")
CODEX_HOST_AUTH = os.path.expanduser("~/.codex/auth.json")
# Каталог моделей codex качает сам — оттуда берём список моделей и уровни размышления.
MODELS_CACHE = os.path.expanduser("~/.codex/models_cache.json")


def newest_executable(name, configured=None, discovered=None):
    """Самая новая установленная CLI; старый путь из .env не должен скрывать новые модели."""
    if discovered is None:
        discovered = (shutil.which(name), os.path.expanduser(f"~/.local/bin/{name}"))
    paths = []
    for value in (configured, *discovered):
        if not value:
            continue
        path = value if os.path.sep in value else shutil.which(value)
        if path and os.access(path, os.X_OK) and path not in paths:
            paths.append(path)
    versions = []
    for order, path in enumerate(paths):
        try:
            output = subprocess.check_output([path, "--version"], text=True, timeout=10,
                                             stderr=subprocess.DEVNULL)
            found = re.search(r"\d+(?:\.\d+)+", output)
            if found:
                versions.append((tuple(map(int, found.group().split("."))), -order, path))
        except (OSError, subprocess.SubprocessError):
            continue
    return max(versions, default=((), 0, paths[0] if paths else None))[2]

# У systemd-сервиса PATH урезанный и ~/.local/bin в него не входит — ищем сами.
CLAUDE_BIN = (os.environ.get("CLAUDE_BIN") or shutil.which("claude")
              or os.path.expanduser("~/.local/bin/claude"))
CODEX_BIN = newest_executable("codex", os.environ.get("CODEX_BIN"))
MODELS = {
    "opus": "для самых сложных задач, глубокого анализа и архитектуры",
    "sonnet": "универсальная модель для повседневной разработки",
    "haiku": "для быстрых и простых задач",
}
CLAUDE_EFFORTS = ["low", "medium", "high", "xhigh", "max"]
ENGINES = ("claude", "codex")
ENGINE_NAMES = {"claude": "Claude", "codex": "Codex"}
WINDOWS = ("five_hour", "seven_day")
# Защитный предел: обычно ответ останавливают кнопкой «Стоп».
# Строки stream-json с картинками и выводом инструментов длиннее лимита asyncio (64 КБ).
LINE_LIMIT = 16 * 1024 * 1024
MAX_IMAGE = 5 * 1024 * 1024  # больше API не принимает
MAX_IMAGES = 15 * 1024 * 1024  # суммарно на запрос: base64 раздувает payload
# Claude Code сам подкладывает в контекст email аккаунта подписки — это владелец
# сервера, а не собеседник; без запрета модель записывала его в общую память.
# Канал — откуда пришёл вопрос: сайт или Telegram-бот.
SYSTEM_PROMPT = {"web": "Ты отвечаешь пользователю в веб-интерфейсе. Можно использовать markdown: "
                        "заголовки, списки, таблицы, блоки кода.\n",
                 "tg": "Ты отвечаешь пользователю в Telegram. Пиши кратко, без таблиц и без "
                       "сложной markdown-разметки.\n"}
OWNER_EMAIL = ("Email из служебного контекста принадлежит владельцу сервера, а не собеседнику: "
               "никогда не называй его, не используй и не записывай в память и файлы.")
# WebFetch и WebSearch --restricted вырезает, если не перечислить их здесь.
FILE_TOOLS = "Bash,Read,Write,Edit,Glob,Grep,NotebookEdit,WebFetch,WebSearch"
CHAT_ID = re.compile(r"[0-9a-f]{32}")


# --- Папки пользователя ---

def creds_freshness(path):
    """Время записи файла, без чтения содержимого учётных данных."""
    try:
        return os.path.getmtime(path)
    except OSError:
        return 0


def link_creds(link, host):
    """Токен подписки один на всех, у каждого пользователя — симлинк на него."""
    if os.path.islink(link):
        return
    if os.path.exists(link):
        # claude обновляет токен атомарной заменой файла, и симлинк становится
        # копией. Refresh-токен одноразовый: старый у хоста уже не работает,
        # поэтому возвращаем хосту копию — но только если она свежее. Иначе
        # протухшая копия затрёт свежий вход хоста и токен других программ.
        if not os.path.exists(host) or creds_freshness(link) >= creds_freshness(host):
            shutil.copy2(link, host)
        os.remove(link)
    os.symlink(host, link)


def user_dir(uid):
    return os.path.join(DATA, "users", str(int(uid)))


def claude_home(uid):
    """CLAUDE_CONFIG_DIR пользователя: своя история и .claude.json, токен общий."""
    d = os.path.join(user_dir(uid), "claude")
    os.makedirs(d, exist_ok=True)
    link_creds(os.path.join(d, ".credentials.json"), HOST_CREDS)
    return d


def codex_home(uid):
    d = os.path.join(user_dir(uid), "codex-home")
    os.makedirs(d, exist_ok=True)
    link_creds(os.path.join(d, "auth.json"), CODEX_HOST_AUTH)
    config = os.path.join(d, "config.toml")
    if not os.path.exists(config):
        # Запасной вариант на случай, если -c переопределения не применятся:
        # без них codex остаётся только на чтение и ничего не спрашивает.
        with open(config, "w") as f:
            f.write('sandbox_mode = "read-only"\napproval_policy = "never"\n')
    return d


def data_dir(uid):
    """Единственная папка, которую видит модель: память и рабочие папки бесед."""
    return str(session_files.directory(os.path.join(user_dir(uid), "data")))


def memory_file(uid):
    return session_files.shared_memory(data_dir(uid))


def library(uid):
    """Все файлы пользователя; в files/general бесед — жёсткие ссылки на них."""
    return str(session_files.directory(os.path.join(data_dir(uid), session_files.LIBRARY)))


def chat_file(uid, chat, ext):
    if not CHAT_ID.fullmatch(chat or ""):
        raise KeyError(chat)
    return os.path.join(user_dir(uid), "chats", chat + ext)


def workspace(uid, chat):
    chat_file(uid, chat, "")  # проверка id: он идёт в путь
    return session_files.prepare(os.path.join(data_dir(uid), "workspaces", chat), memory_file(uid))


# --- Беседы ---

def save_chat(uid, meta):
    path = chat_file(uid, meta["id"], ".json")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(meta, f, ensure_ascii=False)
    os.replace(tmp, path)
    return meta


def create_chat(uid, engine, model, effort=""):
    """Модель и уровень выбирает вызывающий: он знает права пользователя."""
    if engine not in ENGINES:
        raise ValueError("Неизвестный движок")
    now = time.time()
    # У claude id сессии придумываем мы, у codex его выдаст первый ответ.
    sid = str(uuid.uuid4()) if engine == "claude" else ""
    return save_chat(uid, {"id": uuid.uuid4().hex, "title": "", "engine": engine, "model": model,
                           "effort": effort, "sid": sid, "created": now, "updated": now})


def load_chat(uid, chat):
    try:
        with open(chat_file(uid, chat, ".json"), encoding="utf-8") as f:
            return json.load(f)
    except (KeyError, OSError, ValueError):
        return None


def list_chats(uid):
    items = []
    for path in glob.glob(os.path.join(user_dir(uid), "chats", "*.json")):
        meta = load_chat(uid, os.path.basename(path)[:-len(".json")])
        if meta:
            items.append(meta)
    return sorted(items, key=lambda m: m["updated"], reverse=True)


def append_history(uid, chat, event):
    with open(chat_file(uid, chat, ".jsonl"), "a", encoding="utf-8") as f:
        f.write(json.dumps(event, ensure_ascii=False) + "\n")


def history(uid, chat):
    events = []
    try:
        with open(chat_file(uid, chat, ".jsonl"), encoding="utf-8") as f:
            for line in f:
                try:
                    events.append(json.loads(line))
                except ValueError:
                    continue  # оборванная строка после падения сервера
    except OSError:
        pass
    return events


def visible(events):
    """Журнал без заменённых ходов: отметка retry убирает всё с последней реплики
    пользователя. Журнал только дописывается — номера событий у вкладок не съезжают."""
    shown = []
    for event in events:
        if event.get("kind") == "retry":
            asked = max((i for i, e in enumerate(shown) if e.get("kind") == "user"), default=None)
            if asked is not None:
                del shown[asked:]
        else:
            shown.append(event)
    return shown


def last_request(events):
    """Номер последней реплики пользователя в visible(), если после неё не было сжатия."""
    for i in range(len(events) - 1, -1, -1):
        kind = events[i].get("kind")
        if kind == "user":
            return i
        if kind in ("compact", "compacted"):
            return None
    return None


def voice_dir(uid, chat):
    """Голосовые беседы с сайта. Вне папки данных: модели и библиотеке они не нужны."""
    chat_file(uid, chat, "")  # проверка id: он идёт в путь
    return os.path.join(user_dir(uid), "voice", chat)


def tree_size(path):
    """Байты файла или папки целиком; ссылки не раскрываются."""
    try:
        info = os.lstat(path)
    except OSError:
        return 0
    if not os.path.isdir(path) or os.path.islink(path):
        return info.st_size
    total = 0
    for current, _, names in os.walk(path):
        for name in names:
            try:
                total += os.lstat(os.path.join(current, name)).st_size
            except OSError:
                pass
    return total


# Строка журнала сессии, после которой CLI начинает контекст заново.
COMPACT_MARK = {"claude": b'"subtype":"compact_boundary"', "codex": b'"type":"compacted"'}


def after_mark(path, mark):
    """Байты файла после последней строки с mark; без неё — весь файл."""
    since = 0
    try:
        with open(path, "rb") as f:
            for line in f:
                since = 0 if mark in line else since + len(line)
    except OSError:
        return 0
    return since


def chat_size(uid, meta):
    """Байты беседы. context — сессия CLI после последнего сжатия: её модель перечитывает
    на каждом ответе. Сжатие дописывает в тот же файл, поэтому session (файл целиком) не
    уменьшается. Плюс журнал сайта, файлы и голосовые."""
    sid, chat = meta.get("sid"), meta["id"]
    session = context = 0
    if sid:
        if meta["engine"] == "claude":
            # <sid>.jsonl и папка <sid>/ рядом — вложенные агенты той же сессии
            pattern = os.path.join(claude_home(uid), "projects", "*", glob.escape(sid))
            main = glob.glob(pattern + ".jsonl")
            session = sum(tree_size(p) for p in main + glob.glob(pattern))
        else:
            main = glob.glob(os.path.join(codex_home(uid), "sessions", "*", "*", "*",
                                          f"rollout-*-{glob.escape(sid)}.jsonl"))
            session = sum(tree_size(p) for p in main)
        context = sum(after_mark(p, COMPACT_MARK[meta["engine"]]) for p in main)
    return {"session": session, "context": context,
            "history": sum(tree_size(chat_file(uid, chat, ext)) for ext in (".json", ".jsonl", ".links.json")),
            "files": tree_size(os.path.join(data_dir(uid), "workspaces", chat, "files")),
            "voice": tree_size(voice_dir(uid, chat))}


def delete_chat(uid, meta, files=True):
    """files — удалить и файлы беседы из библиотеки. Иначе они остаются там, включая
    ещё не обработанные вложения и неотданные файлы модели."""
    chat = meta["id"]
    root = os.path.join(data_dir(uid), "workspaces", chat)
    if os.path.isdir(root):
        if files:
            session_files.unshelve(root, library(uid))
        else:
            for folder in ("from_user", "for_user"):
                session_files.archive(root, folder)
            shelve(uid, chat)
    shutil.rmtree(root, ignore_errors=True)
    shutil.rmtree(voice_dir(uid, chat), ignore_errors=True)
    if meta["engine"] == "claude":
        for path in glob.glob(os.path.join(claude_home(uid), "projects", "*", meta["sid"] + ".jsonl")):
            os.remove(path)
    elif meta["sid"] and CODEX_BIN:
        # codex delete работает локально по своей базе; не вышло — останется мусор, не беда.
        try:
            subprocess.run([CODEX_BIN, "delete", meta["sid"]], timeout=30,
                           env={**os.environ, "CODEX_HOME": codex_home(uid)},
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except (OSError, subprocess.SubprocessError):
            pass
    for ext in (".json", ".jsonl", ".links.json"):
        try:
            os.remove(chat_file(uid, chat, ext))
        except FileNotFoundError:
            pass


# --- Модели ---

_engine_states = {}
_engine_lock = threading.Lock()
_login_sessions = {}
_login_lock = threading.Lock()
_codex_version_cache = None
ANSI = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
URL = re.compile(r"https://[^\s]+")
DEVICE_CODE = re.compile(r"\b[A-Z0-9]{4}-[A-Z0-9]{4}\b")
LIMIT_ERRORS = ("usage limit", "rate limit", "limit exceeded", "usage_limit", "quota exceeded")
AUTH_ERRORS = ("not logged in", "log in", "login required", "unauthorized", "authentication",
               "invalid token", "token expired", "401")


def installed(engine):
    binary = CLAUDE_BIN if engine == "claude" else CODEX_BIN
    return bool(binary) and os.access(binary, os.X_OK)


def codex_service_home():
    """Отдельный CODEX_HOME проверки: токен общий, каталог соответствует CLI сайта."""
    folder = os.path.join(DATA, "engine", "codex")
    os.makedirs(folder, exist_ok=True)
    link_creds(os.path.join(folder, "auth.json"), CODEX_HOST_AUTH)
    config = os.path.join(folder, "config.toml")
    if not os.path.exists(config):
        with open(config, "w") as f:
            f.write('sandbox_mode = "read-only"\napproval_policy = "never"\n')
    return folder


def _command(engine, probe=False):
    if engine == "claude":
        if probe:
            return [CLAUDE_BIN, "-p", "Ответь только: OK", "--output-format", "json",
                    "--no-session-persistence", "--safe-mode", "--tools", ""]
        return [CLAUDE_BIN, "auth", "status"]
    if probe:
        return [CODEX_BIN, "exec", "--ephemeral", "--ignore-user-config", "--ignore-rules",
                "--json", "--skip-git-repo-check", "-s", "read-only", "Ответь только: OK"]
    return [CODEX_BIN, "login", "status"]


def _engine_env(engine):
    if engine == "claude":
        return model_env(CLAUDE_CONFIG_DIR=os.path.dirname(HOST_CREDS))
    return model_env(CODEX_HOME=codex_service_home())


def _run_check(engine, probe=False):
    try:
        result = subprocess.run(_command(engine, probe), cwd=HERE, env=_engine_env(engine),
                                text=True, capture_output=True, timeout=90)
        return result.returncode, (result.stdout + "\n" + result.stderr).strip()
    except subprocess.TimeoutExpired:
        return -1, "проверка не ответила за 90 секунд"
    except OSError as exc:
        return -1, str(exc)


def check_engine(engine):
    """Проверить установку, вход и короткий настоящий ответ сети."""
    if engine not in ENGINES:
        raise ValueError("Неизвестная нейросеть")
    now = time.time()
    if not installed(engine):
        state = {"engine": engine, "name": ENGINE_NAMES[engine], "state": "missing",
                 "available": False, "installed": False, "login": False,
                 "message": "CLI не установлен на сервере", "checked": now}
    else:
        code, output = _run_check(engine)
        if code:
            state = {"engine": engine, "name": ENGINE_NAMES[engine], "state": "login",
                     "available": False, "installed": True, "login": True,
                     "message": "Аккаунт не подключён", "checked": now}
        else:
            code, output = _run_check(engine, True)
            low = output.lower()
            if code == 0:
                kind, available, message = "available", True, "Доступна и отвечает"
            elif any(word in low for word in LIMIT_ERRORS):
                kind, available, message = "limited", True, "Подключена, но лимит сейчас исчерпан"
            elif any(word in low for word in AUTH_ERRORS):
                kind, available, message = "login", False, "Вход устарел или не завершён"
            else:
                kind, available = "error", False
                message = output.splitlines()[-1] if output else "Не удалось получить ответ"
            state = {"engine": engine, "name": ENGINE_NAMES[engine], "state": kind,
                     "available": available, "installed": True, "login": not available,
                     "message": message[:300], "checked": now}
    with _engine_lock:
        _engine_states[engine] = state
    return dict(state)


def refresh_engines(engine=None):
    """Проверка всех поддерживаемых сетей при старте и по кнопке администратора."""
    names = (engine,) if engine else ENGINES
    return [check_engine(name) for name in names]


def engine_statuses():
    """Все поддерживаемые сети, включая отсутствующие и ещё не проверенные."""
    with _engine_lock:
        known = {name: dict(value) for name, value in _engine_states.items()}
    result = []
    for engine in ENGINES:
        if engine in known:
            result.append(known[engine])
        else:
            present = installed(engine)
            result.append({"engine": engine, "name": ENGINE_NAMES[engine],
                           "state": "checking" if present else "missing", "available": present,
                           "installed": present, "login": present,
                           "message": "Проверка ещё не выполнена" if present else "CLI не установлен на сервере",
                           "checked": 0})
    return result


def engines():
    """Установленные, подключённые и ответившие сети (лимит не отключает сеть)."""
    return [item["engine"] for item in engine_statuses() if item["available"]]


def codex_version():
    global _codex_version_cache
    if _codex_version_cache is not None:
        return _codex_version_cache
    if not installed("codex"):
        return ""
    try:
        output = subprocess.check_output([CODEX_BIN, "--version"], text=True, timeout=10,
                                         stderr=subprocess.DEVNULL)
        found = re.search(r"(\d+\.\d+\.\d+)", output)
        _codex_version_cache = found.group(1) if found else ""
    except (OSError, subprocess.SubprocessError):
        _codex_version_cache = ""
    return _codex_version_cache


def _codex_catalog_file():
    """Свежий каталог, созданный той же версией CLI, что запускает сайт."""
    version = codex_version()
    candidates = [os.path.join(DATA, "engine", "codex", "models_cache.json")]
    candidates += glob.glob(os.path.join(DATA, "users", "*", "codex-home", "models_cache.json"))
    candidates.append(MODELS_CACHE)
    found = []
    for path in candidates:
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
            if data.get("client_version") == version:
                found.append((os.path.getmtime(path), data))
        except (OSError, ValueError):
            continue
    return max(found, default=(0, None))[1]


def codex_models():
    """Видимые модели из каталога именно той версии Codex, которую запускает сайт."""
    if not installed("codex"):
        return []
    data = _codex_catalog_file()
    if not data:
        return []
    listed = [m for m in data.get("models", []) if m.get("visibility") == "list"]
    listed.sort(key=lambda m: m.get("priority", 999))
    return [{"slug": m["slug"], "name": m.get("display_name") or m["slug"],
             "about": m.get("description", ""),
             "efforts": [lvl["effort"] for lvl in m.get("supported_reasoning_levels", [])]}
            for m in listed]


def _clean_login_line(line):
    return ANSI.sub("", line).strip()


def _public_login(session):
    return {key: session.get(key) for key in
            ("engine", "running", "complete", "url", "device_code", "needs_code", "error")}


def _watch_login(engine, session):
    proc = session["proc"]
    try:
        for raw in proc.stdout:
            line = _clean_login_line(raw)
            with session["condition"]:
                if found := URL.search(line):
                    session["url"] = found.group(0)
                if engine == "codex" and (found := DEVICE_CODE.search(line)):
                    session["device_code"] = found.group(0)
                session["condition"].notify_all()
        code = proc.wait()
        with session["condition"]:
            session["running"] = False
            session["complete"] = code == 0
            if code:
                session["error"] = "Вход не завершён"
            session["condition"].notify_all()
        if code == 0:
            check_engine(engine)
    except Exception as exc:
        with session["condition"]:
            session["running"] = False
            session["error"] = str(exc)
            session["condition"].notify_all()


def start_login(engine):
    """Запустить штатный OAuth CLI и вернуть ссылку/код для браузера администратора."""
    if engine not in ENGINES:
        raise ValueError("Неизвестная нейросеть")
    if not installed(engine):
        raise ValueError("CLI не установлен на сервере")
    with _login_lock:
        old = _login_sessions.get(engine)
        if old and old["running"]:
            return _public_login(old)
        if engine == "claude":
            args = [CLAUDE_BIN, "auth", "login", "--claudeai"]
            env = model_env(CLAUDE_CONFIG_DIR=os.path.dirname(HOST_CREDS), BROWSER="/bin/true")
        else:
            args = [CODEX_BIN, "login", "--device-auth"]
            env = model_env(CODEX_HOME=os.path.dirname(CODEX_HOST_AUTH))
        proc = subprocess.Popen(args, cwd=HERE, env=env, text=True, stdin=subprocess.PIPE,
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, bufsize=1,
                                start_new_session=True)
        session = {"engine": engine, "proc": proc, "running": True, "complete": False,
                   "url": "", "device_code": "", "needs_code": engine == "claude", "error": "",
                   "condition": threading.Condition()}
        _login_sessions[engine] = session
        threading.Thread(target=_watch_login, args=(engine, session), daemon=True).start()
    with session["condition"]:
        session["condition"].wait_for(lambda: (session["url"] and (engine == "claude" or session["device_code"]))
                                      or session["error"] or not session["running"], timeout=10)
    return _public_login(session)


def login_state(engine):
    if engine not in ENGINES:
        raise ValueError("Неизвестная нейросеть")
    with _login_lock:
        session = _login_sessions.get(engine)
    return _public_login(session) if session else {"engine": engine, "running": False,
                                                     "complete": False, "url": "", "device_code": "",
                                                     "needs_code": engine == "claude", "error": ""}


def submit_login_code(engine, code):
    if engine != "claude":
        raise ValueError("Код вводится на странице OpenAI")
    with _login_lock:
        session = _login_sessions.get(engine)
    if not session or not session["running"]:
        raise ValueError("Сначала запустите вход")
    code = str(code).strip()
    if not code or len(code) > 2000:
        raise ValueError("Пустой или слишком длинный код")
    session["proc"].stdin.write(code + "\n")
    session["proc"].stdin.flush()
    return _public_login(session)


def stop_logins():
    with _login_lock:
        sessions = list(_login_sessions.values())
    for session in sessions:
        if session["running"]:
            stop(session["proc"])


def models():
    return {"claude": [{"slug": slug, "name": slug, "about": about, "efforts": CLAUDE_EFFORTS}
                       for slug, about in MODELS.items()],
            "codex": codex_models()}


def efforts(engine, model):
    return next((m["efforts"] for m in models()[engine] if m["slug"] == model), None)


# --- Запуск ---

def model_env(**extra):
    # Секреты сайта не нужны ни CLI, ни запускаемым им инструментам.
    return {**{k: v for k, v in os.environ.items() if not k.startswith("NEXUS_")},
            "PATH": os.path.expanduser("~/.local/bin") + os.pathsep + os.environ.get("PATH", os.defpath),
            **extra}


def file_prompt(cwd, text):
    return (text + f"\n\n[Рабочая папка сессии: {cwd}. Перед ответом обязательно проверь "
            "files/from_user и MEMORY.md. Готовые файлы сохраняй в files/for_user; "
            "после ответа вложения будут перенесены в files/general.]")


def fit_images(images, limit=MAX_IMAGES):
    kept, total = [], 0
    for data, mime in images:
        if total + len(data) > limit:
            break
        kept.append((data, mime))
        total += len(data)
    return kept


def build_input(text, images):
    """Одна строка stream-json: текст и/или картинки как контент-блоки."""
    content = [{"type": "text", "text": text}] if text else []
    for data, mime in images:
        content.append({"type": "image", "source": {
            "type": "base64", "media_type": mime, "data": base64.b64encode(data).decode()}})
    return json.dumps({"type": "user", "message": {"role": "user", "content": content}}) + "\n"


# Команда в ленте раскрывается целиком; предел — от гигантских вставок и heredoc.
def short(value, limit=4000):
    value = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
    return value if len(value) <= limit else value[:limit] + "…"


def tool_input(name, data):
    """Самое говорящее поле вызова инструмента, остальное в ленте не нужно."""
    for key in ("command", "file_path", "notebook_path", "pattern", "url", "query"):
        if isinstance(data, dict) and data.get(key):
            return short(data[key])
    return short(data)


def claude_events(rec):
    """События ленты из строки stream-json claude."""
    if rec.get("type") == "system" and rec.get("subtype") == "compact_boundary":
        info = rec.get("compact_metadata") or rec.get("compactMetadata") or {}
        yield {"kind": "compacted", "tokens": info.get("pre_tokens") or info.get("preTokens") or 0,
               "auto": (info.get("trigger") == "auto")}
    if rec.get("type") == "assistant":
        for block in rec.get("message", {}).get("content", []):
            if block.get("type") == "text" and block.get("text"):
                yield {"kind": "text", "text": block["text"]}
            elif block.get("type") == "tool_use":
                yield {"kind": "tool", "name": block.get("name", ""),
                       "input": tool_input(block.get("name"), block.get("input"))}


def codex_events(rec):
    """События ленты из строки --json codex."""
    if rec.get("type") != "item.completed":
        return
    item = rec.get("item", {})
    kind = item.get("type")
    if kind == "agent_message" and item.get("text"):
        yield {"kind": "text", "text": item["text"]}
    elif kind == "command_execution":
        yield {"kind": "tool", "name": "shell", "input": short(item.get("command", ""))}
    elif kind == "file_change":
        yield {"kind": "tool", "name": "edit",
               "input": short(", ".join(c.get("path", "") for c in item.get("changes", [])))}
    elif kind == "web_search":
        yield {"kind": "tool", "name": "web", "input": short(item.get("query", ""))}
    elif kind == "mcp_tool_call":
        yield {"kind": "tool", "name": f'{item.get("server")}.{item.get("tool")}',
               "input": short(item.get("arguments", ""))}


def claude_limits(rec):
    """Проценты окон подписки из rate_limit_event: utilization там — доля от 0 до 1."""
    info = rec.get("rate_limit_info") or {}
    windows = info.get("unifiedWindows") or {}
    if not windows and info.get("utilization") is not None and info.get("rateLimitType"):
        windows = {info["rateLimitType"]: info}
    for window, w in windows.items():
        if window in WINDOWS and w.get("utilization") is not None:
            yield {"kind": "limits", "window": window, "percent": round(w["utilization"] * 100, 2),
                   "resets_at": w.get("resetsAt") or 0}


def codex_limits(uid, sid, since):
    """Проценты окон codex из журнала сессии: в --json их нет.

    Берём строки token_count, записанные не раньше начала запуска.
    """
    found = glob.glob(os.path.join(codex_home(uid), "sessions", "*", "*", "*", f"rollout-*-{sid}.jsonl"))
    events = []
    if not sid or not found:
        return events
    with open(found[0], encoding="utf-8", errors="replace") as f:
        for line in f:
            if '"token_count"' not in line:
                continue
            try:
                rec = json.loads(line)
                stamp = datetime.fromisoformat(rec["timestamp"].replace("Z", "+00:00")).timestamp()
            except (ValueError, KeyError):
                continue
            limits = (rec.get("payload") or {}).get("rate_limits") or {}
            if stamp < since or limits.get("limit_id", "codex") != "codex":
                continue
            for w in (limits.get("primary"), limits.get("secondary")):
                window = {300: "five_hour", 10080: "seven_day"}.get((w or {}).get("window_minutes"))
                if window:
                    events.append({"kind": "limits", "window": window, "percent": w["used_percent"],
                                   "resets_at": w.get("resets_at") or 0})
    return events


# Общие скиллы сайта: data/plugin/skills/<имя>/SKILL.md. Claude получает их одним
# плагином nexus, Codex — ссылками в своей изолированной CODEX_HOME. Обе модели
# читают и запускают их скрипты, но менять не могут.
PLUGIN = os.path.join(DATA, "plugin")
# Python, node и пакеты, которые нужны скиллам (как ставить — в README).
TOOLS = os.path.join(PLUGIN, "tools")


# Скиллы аккаунта claude.ai: CLI хозяина сам скачивает их сюда. Админ выкладывает
# нужные на сайт ссылками в плагин — обновления аккаунта подхватываются сами.
SYNCED = os.path.expanduser("~/.claude/skills/synced")


def skill_list(pattern):
    """Скиллы по шаблону пути к SKILL.md: имя -> описание."""
    found = {}
    for path in sorted(glob.glob(pattern)):
        with open(path, encoding="utf-8", errors="replace") as f:
            about = re.search(r"^description:\s*(.+)$", f.read(), re.M)
        found[os.path.basename(os.path.dirname(path))] = about.group(1).strip(" '\"") if about else ""
    return found


def account_skills():
    return skill_list(os.path.join(SYNCED, "*", "*", "SKILL.md"))


def publish_skills(names):
    """Выложить на сайт ровно эти скиллы аккаунта. Свои папки в плагине не трогаем."""
    os.makedirs(os.path.join(PLUGIN, "skills"), exist_ok=True)
    for path in glob.glob(os.path.join(SYNCED, "*", "*", "SKILL.md")):
        source = os.path.dirname(path)
        link = os.path.join(PLUGIN, "skills", os.path.basename(source))
        if os.path.basename(source) in names and not os.path.lexists(link):
            os.symlink(source, link)
        elif os.path.basename(source) not in names and os.path.islink(link):
            os.unlink(link)
    return skills()


def skills():
    """Установленные скиллы: имя -> описание из SKILL.md."""
    found = skill_list(os.path.join(PLUGIN, "skills", "*", "SKILL.md"))
    if found and not os.path.exists(os.path.join(PLUGIN, ".claude-plugin", "plugin.json")):
        os.makedirs(os.path.join(PLUGIN, ".claude-plugin"), exist_ok=True)
        with open(os.path.join(PLUGIN, ".claude-plugin", "plugin.json"), "w") as f:
            json.dump({"name": "nexus", "version": "1.0.0"}, f)
    return found


def codex_skills(uid, enabled=True, disabled=()):
    """Подключить опубликованные скиллы к Codex конкретного пользователя.

    Системную папку .system и установленные самим Codex элементы не трогаем. Нашими
    считаются только ссылки в data/plugin/skills. Возвращаем настоящие каталоги
    включённых скиллов: их надо открыть песочнице на чтение для скриптов/assets.
    """
    available = skills() if enabled else {}
    wanted = set(available) - set(disabled)
    folder = os.path.join(codex_home(uid), "skills")
    os.makedirs(folder, exist_ok=True)
    root = os.path.realpath(os.path.join(PLUGIN, "skills"))
    for old in glob.glob(os.path.join(folder, "*")):
        if os.path.islink(old) and os.path.commonpath([os.path.realpath(old), root]) == root \
                and os.path.basename(old) not in wanted:
            os.unlink(old)
    linked = []
    for name in sorted(wanted):
        source = os.path.join(root, name)
        link = os.path.join(folder, name)
        if not os.path.lexists(link):
            os.symlink(source, link)
        if os.path.islink(link) and os.path.realpath(link) == os.path.realpath(source):
            linked.append(os.path.realpath(source))
    return linked


def other_workspaces(uid, chat, access):
    """Рабочие папки чужих бесед и библиотека с их файлами, если пользователю нельзя их видеть."""
    if access["all_chats"]:
        return []
    root = os.path.join(data_dir(uid), "workspaces")
    return [os.path.join(root, name) for name in sorted(os.listdir(root)) if name != chat] + [library(uid)]


def protected(uid, cwd, access):
    """Что модели нельзя менять: files/ и инструкции без «Изменения файлов», обе памяти
    без «Изменения памяти». Корень рабочей папки остаётся записываемым: иначе bwrap
    не создаёт там защитные файлы и Bash не стартует."""
    paths = [] if access["write"] else [os.path.join(cwd, n) for n in ("files", "CLAUDE.md", "AGENTS.md")] + [library(uid)]
    return paths + ([] if access["memory"] else [memory_file(uid), os.path.join(cwd, "MEMORY.md")])


def patterns(commands):
    """«git push» совпадает и с самой командой, и с ней же с аргументами."""
    return list(commands) + [f"{c} *" for c in commands if "*" not in c]


def bash_rules(commands):
    return [f"Bash({p})" for p in patterns(commands)]


def system_prompt(uid, cwd, channel, commands=()):
    return (SYSTEM_PROMPT[channel] + OWNER_EMAIL + "\n" + session_files.instructions(cwd, memory_file(uid))
            + outside_note(commands))


def outside(access):
    """Команды вне песочницы из «Опасной зоны»: только с галочкой и «Запуском команд»."""
    return access["unsandboxed_commands"] if access["bash"] and access["unsandboxed"] else []


def outside_note(commands):
    """Без этой подсказки модель склеивает разрешённую команду с другими (find …; ls /home)
    — и вся строка остаётся в песочнице, а модель решает, что доступа нет."""
    if not commands:
        return ""
    listed = ", ".join(f"`{c}`" for c in commands)
    return (f"\nВне песочницы, прямо на сервере, выполняются только эти команды: {listed}. "
            "Такую команду запускай отдельным вызовом, одну, без ;, &&, | и других команд в той же строке, "
            "и начинай её точно так, как в списке (дальше можно дописать аргументы). "
            "Всё остальное выполняется в песочнице и видит только папку пользователя.\n")


# --- Повтор последнего запроса: откат сессии CLI на ход назад ---

def _prompt_text(rec):
    """Текст реплики пользователя в транскрипте claude; не реплика (ответ инструмента,
    служебная вставка) — None."""
    if rec.get("type") != "user" or rec.get("isMeta") or rec.get("isSidechain") or rec.get("isCompactSummary"):
        return None
    content = (rec.get("message") or {}).get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list) and not any(c.get("type") == "tool_result" for c in content):
        return "\n".join(c.get("text", "") for c in content if c.get("type") == "text")
    return None


def _fresh_session(uid, meta):
    meta["sid"] = str(uuid.uuid4()) if meta["engine"] == "claude" else ""
    save_chat(uid, meta)


def rewind(uid, meta, text):
    """Убрать из сессии CLI последний ход, если это запрос text. Для claude — флаги
    следующего запуска (--resume-session-at), codex переписываем на месте. Хода
    в сессии нет (CLI упал до записи) — ничего не делаем, None."""
    if not meta["sid"]:
        return None
    if meta["engine"] == "claude":
        found = glob.glob(os.path.join(claude_home(uid), "projects", "*", meta["sid"] + ".jsonl"))
        last = None
        if found:
            with open(found[0], encoding="utf-8", errors="replace") as f:
                for line in f:
                    try:
                        rec = json.loads(line)
                    except ValueError:
                        continue
                    if _prompt_text(rec) is not None:
                        last = rec
        if not last or not _prompt_text(last).startswith(text):
            return None
        if not last.get("parentUuid"):
            _fresh_session(uid, meta)  # это был первый ход: начинаем сессию заново
            return None
        # drops-turn: CLI откажется, если в отрезаемом куске окажется не только этот ход.
        return {"at": last["parentUuid"], "drop": last["uuid"]}
    found = glob.glob(os.path.join(codex_home(uid), "sessions", "*", "*", "*", f"rollout-*-{meta['sid']}.jsonl"))
    if not found:
        return None
    with open(found[0], "rb") as f:
        lines = f.readlines()
    starts = [i for i, line in enumerate(lines) if b'"task_started"' in line and _codex_kind(line) == "task_started"]
    if not starts or not any(_codex_user(line, text) for line in lines[starts[-1]:]):
        return None
    if len(starts) == 1:
        _fresh_session(uid, meta)
        return None
    # ponytail: в codex exec отката нет, а thread/rollback app-server устарел и не берёт
    # такие беседы. Журнал сессии — источник истории, его база thread_history — только
    # проекция: обрезаем журнал по началу хода и сбрасываем проекцию беседы.
    tmp = found[0] + ".tmp"
    with open(tmp, "wb") as f:
        f.writelines(lines[:starts[-1]])
    os.replace(tmp, found[0])
    for db in glob.glob(os.path.join(codex_home(uid), "thread_history_*.sqlite")):
        try:
            with contextlib.closing(sqlite3.connect(db, timeout=5)) as conn, conn:
                tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                for table in ("thread_turns", "thread_items", "thread_history_projection_state",
                              "thread_realtime_items"):
                    if table in tables:
                        conn.execute(f"DELETE FROM {table} WHERE thread_id = ?", (meta["sid"],))
        except sqlite3.Error:
            pass
    return None


def _codex_kind(line):
    try:
        return (json.loads(line).get("payload") or {}).get("type")
    except ValueError:
        return None


def _codex_user(line, text):
    """Строка журнала codex — запрос пользователя, начинающийся с text."""
    if b'"user' not in line:
        return False
    try:
        payload = json.loads(line).get("payload") or {}
    except ValueError:
        return False
    if payload.get("type") == "user_message":
        return (payload.get("message") or "").startswith(text)
    if payload.get("type") == "message" and payload.get("role") == "user":
        return any((c.get("text") or "").startswith(text) for c in payload.get("content") or [])
    return False


def claude_args(uid, meta, cwd, access, channel="web", rewind=None):
    found = glob.glob(os.path.join(claude_home(uid), "projects", "*", meta["sid"] + ".jsonl"))
    data = data_dir(uid)
    others = other_workspaces(uid, meta["id"], access)
    writes = access["write"] or access["memory"]
    installed = skills() if access["skills"] else {}
    tools = (["Read", "Glob", "Grep"] + (["Bash"] if access["bash"] else [])
             + (["Write", "Edit", "NotebookEdit"] if writes else [])
             + (["WebFetch", "WebSearch"] if access["web"] else [])
             + (["Skill"] if installed else []))
    allowed = list(tools)
    commands = access["bash"] and access["commands"]
    # Белый список команд: в dontAsk всё, что не в allow, — отказ. Но
    # autoAllowBashIfSandboxed разрешил бы любую команду, поэтому он выключается.
    only_listed = commands and bool(access["allowed_commands"])
    if only_listed:
        i = allowed.index("Bash")
        allowed[i:i + 1] = bash_rules(access["allowed_commands"])
    # Опасная зона: эти команды идут мимо песочницы с правами хозяина сервера.
    # excludedCommands только выводит их наружу, разрешение нужно отдельно.
    outside_cmds = outside(access)
    allowed += bash_rules(outside_cmds)
    # Без allowedDomains прокси песочницы спрашивал бы разрешение на каждый хост,
    # а в dontAsk это отказ. strictAllowlist — отказ наверняка.
    network = {"allowedDomains": ["*"]} if access["network"] else {"strictAllowlist": True}
    if access["web"] and access["site_allow"]:
        # Пустой белый список — WebFetch не открывает ничего, остаётся только поиск.
        i = allowed.index("WebFetch")
        allowed[i:i + 1] = [f"WebFetch(domain:{d})" for d in access["allowed_sites"]]
        # Правила WebFetch(domain:) CLI добавляет и в сеть песочницы — даже при
        # strictAllowlist. deniedDomains действует только на команды: закрываем их там.
        if not access["network"] and access["allowed_sites"]:
            network["deniedDomains"] = access["allowed_sites"]
    filesystem = {"denyRead": [os.path.expanduser("~"), HERE, DATA] + others,
                  "allowRead": [data], "allowWrite": [data] if writes else []}
    # --add-dir делает папку данных записываемой и для Bash — запреты точечные.
    # Скиллы аккаунта — ссылки в домашнюю папку: открываем настоящие пути включённых.
    linked = [os.path.realpath(os.path.join(PLUGIN, "skills", name)) for name in installed
              if os.path.islink(os.path.join(PLUGIN, "skills", name)) and name not in access["skills_off"]]
    locked = protected(uid, cwd, access) + ([PLUGIN] + linked if installed else [])
    if installed:
        # Только для Bash: --add-dir открыл бы её и Read, но песочница кладёт в корень
        # рабочей папки заглушки (.mcp.json), а корень закрыт на запись — Bash не стартует.
        filesystem["allowRead"] += [PLUGIN] + linked
    if others + locked:
        filesystem["denyWrite"] = others + locked
    # Файловые инструменты: правило Read закрывает и Glob/Grep, Edit — Write и
    # NotebookEdit; абсолютный путь — через //. Для файла и папки разные шаблоны.
    deny = ([f"{tool}(/{path}/**)" for path in others for tool in ("Read", "Edit")]
            + [rule for path in locked for rule in (f"Edit(/{path})", f"Edit(/{path}/**)")])
    if commands:
        deny += bash_rules(access["denied_commands"])
    if access["web"] and access["site_deny"]:
        # Запрет WebFetch(domain:) CLI переносит и в сеть песочницы: curl туда тоже не пустит.
        deny += [f"WebFetch(domain:{d})" for d in access["denied_sites"]]
    # Снятый скилл остаётся в списке у модели (skillOverrides из --settings его не
    # прячет), но запуск блокируется.
    deny += [f"Skill(nexus:{name})" for name in access["skills_off"] if name in installed]
    cli_settings = {}
    if installed:
        # --safe-mode выключил бы и наш плагин, поэтому то же закрываем по отдельности.
        # Проверено ловушками в рабочей папке: .claude/skills, .claude/settings.json,
        # .mcp.json и CLAUDE.md в родительской папке не срабатывают. Хуки и MCP
        # выполнялись бы вне песочницы.
        cli_settings = {"disableBundledSkills": True, "disableAllHooks": True, "disableWorkflows": True,
                    "disableSkillShellExecution": True, "claudeMdExcludes": ["**/*"],
                    "enabledPlugins": {"cc-plugin-plugin-authoring@builtin": False,
                                       "cc-plugin-agents-md@builtin": False}}
    # Без скиллов --safe-mode выключает CLAUDE.md, хуки, скиллы, MCP и плагины разом.
    mode = ["--strict-mcp-config", "--plugin-dir", PLUGIN] if installed else ["--safe-mode"]
    return [
        CLAUDE_BIN, "-p",
        "--input-format", "stream-json",
        "--output-format", "stream-json", "--verbose",
        *mode,
        "--restricted", "--tools", ",".join(tools),
        "--permission-mode", "dontAsk",
        "--add-dir", data,  # файловым инструментам — та же папка, что и Bash
        "--settings", json.dumps({
            **cli_settings,
            "sandbox": {
                "enabled": True, "failIfUnavailable": True,
                "autoAllowBashIfSandboxed": not only_listed, "allowUnsandboxedCommands": False,
                "excludedCommands": patterns(outside_cmds),
                # Закрыто всё хозяйство сайта (база, токены подписок, чужие
                # пользователи), открыта только папка данных этого пользователя.
                "filesystem": filesystem,
                # WebFetch(domain:*) в allow не кладём: оно открыло бы сеть и для Bash.
                "network": network,
            },
            # Правила списком, а не через --allowedTools: там пробел и запятая — разделители.
            "permissions": {"allow": allowed, "deny": deny},
        }),
        "--system-prompt-snapshot", "off",
        "--model", meta["model"],
        "--system-prompt", system_prompt(uid, cwd, channel, outside_cmds),
        # Транскрипт уже есть — продолжаем, иначе создаём сессию с нашим id.
        "--resume" if found else "--session-id", meta["sid"],
    ] + (["--effort", meta["effort"]] if meta["effort"] else []) + (
        # Повтор: продолжаем с хода перед отменённым — новая ветка остаётся в сессии.
        ["--resume-session-at", rewind["at"], "--resume-drops-turn", rewind["drop"]] if rewind and found else [])


def codex_untrusted(cwd):
    """codex сам помечает рабочую папку доверенной и тогда берёт из её .codex правила
    команд, хуки и настройки: модель с правом записи положила бы туда правило и вышла
    из песочницы. Корень проекта — сама папка, и она явно недоверенная."""
    return ["-c", "project_root_markers=[]",
            "-c", "projects={%s={trust_level=\"untrusted\"}}" % json.dumps(cwd)]


def codex_rules(uid, commands):
    """Команды вне песочницы у codex — правила allow в rules/ его папки (её модель не
    видит). Наружу команда выходит, только если разрешена каждая её часть; лишние
    аргументы правилу не мешают. host_executable не даёт подсунуть свой «docker»
    абсолютным путём: совпадает только настоящая программа из PATH."""
    folder = os.path.join(codex_home(uid), "rules")
    os.makedirs(folder, exist_ok=True)
    for old in glob.glob(os.path.join(folder, "*.rules")):
        if os.path.basename(old) != "nexus.rules":
            os.remove(old)  # одобрения codex сюда не пишет (approval never), чужое не держим
    target = os.path.join(folder, "nexus.rules")
    if not commands:
        with contextlib.suppress(FileNotFoundError):
            os.remove(target)
        return
    lines, programs = [], {}
    for command in commands:
        tokens = shlex.split(command)
        lines.append(f"prefix_rule(pattern={json.dumps(tokens, ensure_ascii=False)}, decision=\"allow\")")
        if not tokens[0].startswith("/"):
            found = shutil.which(tokens[0])
            programs[tokens[0]] = sorted({found, os.path.realpath(found)}) if found else []
    lines += [f"host_executable(name={json.dumps(name, ensure_ascii=False)}, paths={json.dumps(paths, ensure_ascii=False)})"
              for name, paths in programs.items()]
    tmp = f"{target}.{os.getpid()}.tmp"
    with open(tmp, "w") as f:
        f.write("\n".join(lines) + "\n")
    os.replace(tmp, target)


def codex_args(uid, meta, cwd, files, access, channel="web"):
    """Права Codex CLI: инструменты, файловая песочница и сеть команд."""
    args = [CODEX_BIN, "exec", "-C", cwd] + (["resume", meta["sid"]] if meta["sid"] else []) + [
        "--json", "--skip-git-repo-check", "-m", meta["model"]] + codex_untrusted(cwd)
    # Опасная зона: эти команды идут мимо песочницы с правами хозяина сервера.
    codex_rules(uid, outside(access))
    if meta["effort"]:
        args += ["-c", f'model_reasoning_effort="{meta["effort"]}"']
    mode = "write" if access["write"] else "read"
    data = data_dir(uid)
    rules = {data: mode}
    # Системные скиллы Codex остаются как есть; опубликованные скиллы сайта у
    # каждого пользователя свои и доступны только на чтение.
    for path in codex_skills(uid, access["skills"], access["skills_off"]):
        rules[path] = "read"
    # Запрет памяти действует и в доступных соседних беседах. Не выдаём write
    # отдельным файлам: Codex 0.154.0 считает их каталогами и ломает bwrap.
    if not access["memory"]:
        rules[memory_file(uid)] = "read"
        workspaces = glob.glob(os.path.join(data, "workspaces", "*")) if access["all_chats"] else [cwd]
        for path in workspaces:
            if os.path.isdir(path):
                rules[os.path.join(path, "MEMORY.md")] = "read"
    rules.update((p, "deny") for p in other_workspaces(uid, meta["id"], access))
    paths = ", ".join(f"{json.dumps(p)} = {json.dumps(rule)}" for p, rule in rules.items())
    filtered_network = access["network"] and (access["site_allow"] or access["site_deny"])
    domains = {}
    if filtered_network:
        domains.update((d, "allow") for d in (access["allowed_sites"] if access["site_allow"] else ["*"]))
        if access["site_deny"]:
            domains.update((d, "deny") for d in access["denied_sites"])
    domain_rules = ", ".join(f"{json.dumps(d)} = {json.dumps(rule)}" for d, rule in domains.items())
    web = ("cached" if access["web_cached"] else "live") if access["web"] else "disabled"
    # Профиль прав сильнее sandbox_mode: читать и писать можно только папку данных
    # пользователя, из остального — системные каталоги на чтение.
    for setting in ('default_permissions="chat"', 'approval_policy="never"',
                    'permissions.chat.filesystem={":minimal" = "read", %s}' % paths,
                    "permissions.chat.network.enabled=" + ("true" if access["network"] else "false"),
                    "features.shell_tool=" + ("true" if access["bash"] else "false"),
                    "features.multi_agent=" + ("true" if access["multi_agent"] else "false"),
                    "features.network_proxy.enabled=" + ("true" if filtered_network else "false"),
                    "permissions.chat.network.domains={%s}" % domain_rules,
                    'web_search="%s"' % web,
                    'developer_instructions=' + json.dumps(system_prompt(uid, cwd, channel, outside(access)))):
        args += ["-c", setting]
    if filtered_network:
        # Свой случайный порт у каждого запроса: параллельные беседы не конфликтуют.
        args += ["-c", 'permissions.chat.network.proxy_url="http://127.0.0.1:0"',
                 "-c", 'permissions.chat.network.socks_url="http://127.0.0.1:0"']
    for path in files:
        args += ["-i", path]
    return args + ["-"]  # промпт со stdin: длинный текст в аргумент не влезет


async def run(uid, meta, text, access, images=(), on_proc=lambda proc: None, channel="web", raw=False,
              rewind=None):
    """Асинхронный генератор событий ответа. Ошибку CLI поднимает RuntimeError.

    Кроме событий ленты отдаёт служебные: limits (проценты окон подписки у claude)
    и tokens — их в историю не пишут.
    meta меняется на месте: codex выдаёт id сессии только в первом ответе.
    raw — служебная команда (/compact): без приписки о папке, иначе CLI примет её за аргументы.
    on_proc получает процесс, чтобы его можно было остановить через stop().
    rewind — флаги отката claude от rewind(): ответ заново на последний запрос.
    """
    cwd = workspace(uid, meta["id"])
    if raw and text == "/compact" and meta["engine"] == "codex":
        async for event in codex_compact(uid, meta, cwd, on_proc):
            yield event
        return
    prompt = text if raw else file_prompt(cwd, text)
    images = fit_images(images)
    files = []
    if meta["engine"] == "claude":
        args = claude_args(uid, meta, cwd, access, channel, rewind)
        # Без --safe-mode (скиллы) CLAUDE.md и автопамять выключаются только так.
        env = model_env(CLAUDE_CONFIG_DIR=claude_home(uid), CLAUDE_CODE_DISABLE_CLAUDE_MDS="1",
                        CLAUDE_CODE_DISABLE_AUTO_MEMORY="1", NODE_PATH=os.path.join(TOOLS, "node_modules"))
        # Инструменты скиллов первыми: без «Скиллов» песочница их прячет и
        # команды находятся в системе как обычно.
        env["PATH"] = os.pathsep.join([os.path.join(TOOLS, "bin"), os.path.join(TOOLS, "venv", "bin"), env["PATH"]])
        payload = build_input(prompt, images)
        parse = claude_events
    else:
        if not CODEX_BIN:
            raise RuntimeError("codex не установлен")
        tmp = os.path.join(codex_home(uid), "tmp")
        os.makedirs(tmp, exist_ok=True)
        for data, mime in images:  # codex принимает картинки только файлами
            path = os.path.join(tmp, uuid.uuid4().hex + "." + (mime.split("/")[-1] or "jpg"))
            with open(path, "wb") as f:
                f.write(data)
            files.append(path)
        args = codex_args(uid, meta, cwd, files, access, channel)
        env = model_env(CODEX_HOME=codex_home(uid))
        payload = prompt
        parse = codex_events
    proc = await asyncio.create_subprocess_exec(
        *args, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE, cwd=cwd, env=env,
        start_new_session=True, limit=LINE_LIMIT)
    on_proc(proc)
    stderr = asyncio.create_task(proc.stderr.read())
    said, fatal, err = False, False, ""
    timeout = settings.get(settings.TIMEOUT.name)
    deadline = time.monotonic() + timeout
    try:
        try:
            proc.stdin.write(payload.encode())
            await proc.stdin.drain()
            proc.stdin.close()
        except (BrokenPipeError, ConnectionResetError):
            pass  # CLI упал на старте — причина будет в stderr
        while True:
            # Дедлайн на чтение, а не asyncio.timeout вокруг yield: иначе отмена
            # могла бы прилететь в код потребителя, пока генератор стоит на yield.
            try:
                line = await asyncio.wait_for(proc.stdout.readline(), deadline - time.monotonic())
            except TimeoutError:
                raise RuntimeError(f"Ответ идёт дольше {max(1, timeout // 60)} мин — остановлен") from None
            if not line:
                break
            try:
                rec = json.loads(line)
            except ValueError:
                continue  # человекочитаемые строки вперемешку с событиями
            kind = rec.get("type")
            if kind == "thread.started":
                meta["sid"] = rec.get("thread_id", "")
            elif kind == "rate_limit_event":
                for event in claude_limits(rec):
                    yield event
            elif kind in ("result", "turn.completed") and rec.get("usage"):
                u = rec["usage"]
                yield {"kind": "tokens", "out": u.get("output_tokens", 0),
                       "in": sum(u.get(k, 0) for k in ("input_tokens", "cache_creation_input_tokens",
                                                       "cache_read_input_tokens"))}
            if kind == "result" and rec.get("is_error"):
                fatal = True
                err = rec.get("result") or "\n".join(rec.get("errors", []))
            elif kind == "result" and not said and rec.get("result"):
                said = True  # ответ без отдельного assistant-сообщения
                yield {"kind": "text", "text": rec["result"]}
            elif kind in ("turn.failed", "error"):
                # codex шлёт error и на переподключениях: фатален только turn.failed.
                fatal = fatal or kind == "turn.failed"
                err = (rec.get("error") or {}).get("message") or rec.get("message") or "ошибка codex"
            for event in parse(rec):
                said = said or event["kind"] in ("text", "compacted")  # у /compact текста нет
                yield event
        await proc.wait()
    finally:
        stop(proc)
        for path in files:
            os.remove(path)
        # Обновлённый токен сразу отдаём хосту, а не при следующем запросе:
        # остальные пользователи и программы хоста читают его оттуда.
        if meta["engine"] == "claude":
            claude_home(uid)
    raw_err = (await stderr).decode(errors="replace").strip()
    if proc.returncode != 0 or fatal or not said:
        raise RuntimeError(err or raw_err or f"{meta['engine']} завершился с ошибкой")


def export_markdown(meta, events):
    """Беседа одним Markdown-файлом: реплики, ответы, инструменты и файлы."""
    name = {"claude": "Claude", "codex": "Codex"}.get(meta["engine"], meta["engine"])
    stamp = lambda ts: time.strftime("%d.%m.%Y %H:%M", time.localtime(ts))  # noqa: E731
    out = [f"# {meta['title'] or 'Новая беседа'}", "",
           f"{name} · {meta['model']}" + (f" · {meta['effort']}" if meta.get("effort") else "")
           + f" · создана {stamp(meta['created'])}", ""]
    answering = False
    for e in visible(events):
        kind = e.get("kind")
        if kind == "user":
            out += ["---", "", "### Вы" + (f" · {stamp(e['ts'])}" if e.get("ts") else ""), "", e.get("text", ""), ""]
            if e.get("files"):
                out += ["Вложения: " + ", ".join(e["files"]), ""]
            answering = False
        elif kind in ("text", "tool", "error", "warning", "stopped", "compacted") and not answering:
            out += [f"### {name}", ""]
            answering = True
        if kind == "text":
            out += [e["text"], ""]
        elif kind == "tool":
            out += [f"> `{e.get('name', '')}` {e.get('input', '')}".rstrip(), ""]
        elif kind in ("error", "warning"):
            out += [f"> **{'Ошибка' if kind == 'error' else 'Внимание'}:** {e.get('text', '')}", ""]
        elif kind == "stopped":
            out += ["> Остановлено", ""]
        elif kind == "compacted":
            out += ["> Беседа сжата", ""]
        elif kind == "done" and e.get("files"):
            out += ["Файлы: " + ", ".join(e["files"]), ""]
            answering = False
    return "\n".join(out).rstrip() + "\n"


def search(uid, query, limit=50):
    """Беседы, где query есть в названии или в тексте реплик и ответов.
    ponytail: читаем журналы целиком на каждый поиск — для личных бесед быстро."""
    needle = query.casefold()
    found = []
    for meta in list_chats(uid):
        title = meta["title"] or "Новая беседа"
        hits, snippet = 0, ""
        for e in visible(history(uid, meta["id"])):
            if e.get("kind") not in ("user", "text", "transcript"):
                continue
            text = e.get("text") or ""
            at = text.casefold().find(needle)
            if at < 0:
                continue
            hits += text.casefold().count(needle)
            if not snippet:
                start = max(0, at - 40)
                snippet = (("…" if start else "") + text[start:at + len(needle) + 80]).replace("\n", " ")
        in_title = needle in title.casefold()
        if hits or in_title:
            found.append({"id": meta["id"], "title": title, "engine": meta["engine"], "hits": hits,
                          "snippet": snippet, "updated": meta["updated"]})
        if len(found) >= limit:
            break
    return found


async def codex_compact(uid, meta, cwd, on_proc):
    """«Сжать беседу» у codex: в codex exec сжатия нет, оно есть в codex app-server
    (JSON-RPC по stdin/stdout): thread/resume и thread/compact/start. Отметка compacted
    ложится в тот же журнал сессии, дальше беседа идёт через codex exec как обычно.
    ponytail: app-server у codex помечен экспериментальным — протокол может смениться."""
    if not CODEX_BIN:
        raise RuntimeError("codex не установлен")
    if not meta["sid"]:
        raise RuntimeError("Сжимать пока нечего")
    proc = await asyncio.create_subprocess_exec(
        CODEX_BIN, "app-server", *codex_untrusted(cwd), stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL, cwd=cwd, env=model_env(CODEX_HOME=codex_home(uid)),
        start_new_session=True, limit=LINE_LIMIT)
    on_proc(proc)

    def send(message):
        proc.stdin.write((json.dumps(message) + "\n").encode())

    timeout = settings.get(settings.TIMEOUT.name)
    deadline = time.monotonic() + timeout
    send({"id": 0, "method": "initialize", "params": {"clientInfo": {"name": "nexus", "version": "1"}}})
    send({"method": "initialized"})
    # Сжатие инструментов не запускает: песочница — только чтение, без вопросов.
    send({"id": 1, "method": "thread/resume", "params": {
        "threadId": meta["sid"], "model": meta["model"], "cwd": cwd, "sandbox": "read-only",
        "approvalPolicy": "never", "excludeTurns": True}})
    said = False
    try:
        await proc.stdin.drain()
        while True:
            try:
                line = await asyncio.wait_for(proc.stdout.readline(), deadline - time.monotonic())
            except TimeoutError:
                raise RuntimeError(f"Сжатие идёт дольше {max(1, timeout // 60)} мин — остановлено") from None
            if not line:
                raise RuntimeError("codex app-server завершился до конца сжатия")
            try:
                msg = json.loads(line)
            except ValueError:
                continue
            if msg.get("error"):
                raise RuntimeError(msg["error"].get("message") or "ошибка codex app-server")
            if msg.get("id") == 1:
                send({"id": 2, "method": "thread/compact/start", "params": {"threadId": meta["sid"]}})
                await proc.stdin.drain()
                continue
            method, params = msg.get("method", ""), msg.get("params") or {}
            item = params.get("item") or {}
            if method == "item/completed" and item.get("type") == "contextCompaction" and not said:
                said = True
                yield {"kind": "compacted", "tokens": 0, "auto": False}
            elif method == "turn/completed":
                turn = params.get("turn") or {}
                if turn.get("status") != "completed":
                    raise RuntimeError((turn.get("error") or {}).get("message") or "codex не сжал беседу")
                if not said:
                    yield {"kind": "compacted", "tokens": 0, "auto": False}
                return
    finally:
        stop(proc)
        await proc.wait()


def stop(proc):
    if proc.returncode is None:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass


def finish(uid, chat, ok):
    """Файлы после ответа: готовые уходят в general и возвращаются ссылками.

    Вложения пользователя переносим только после успешного ответа — при ошибке
    они остаются в очереди для следующей попытки. Всё новое в general
    (и то, что модель положила туда сама) попадает в библиотеку.
    """
    cwd = workspace(uid, chat)
    if ok:
        session_files.archive(cwd, "from_user")
    ready = [os.path.relpath(p, cwd) for p in session_files.archive(cwd, "for_user")]
    shelve(uid, chat)
    return ready


def links(uid, chat):
    """Карта беседы: путь в files/general -> путь в библиотеке."""
    try:
        with open(chat_file(uid, chat, ".links.json"), encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def delete_chat_file(uid, chat, path):
    """Убирает файл из беседы. Копия в библиотеке остаётся. False — нет такого файла."""
    return bool(delete_chat_files(uid, chat, [path]))


def delete_chat_files(uid, chat, paths):
    """Убирает файлы из беседы, копии в библиотеке остаются. Возвращает удалённые пути."""
    root = Path(workspace(uid, chat))
    shelved = None
    deleted = []
    for path in paths:
        target = session_files.contained(root, path)
        if target is None or not target.is_relative_to(root / "files"):
            continue
        try:
            info = target.lstat()
        except OSError:
            continue
        if not stat.S_ISREG(info.st_mode):
            continue
        # Вторая жёсткая ссылка допустима только на файл библиотеки, как и при скачивании.
        if info.st_nlink > 1:
            if shelved is None:
                shelved = session_files.shelved(library(uid))
            if session_files.inode(info) not in shelved:
                continue
        target.unlink()
        deleted.append(path)
    if deleted:
        shelve(uid, chat)  # забыть записи о файлах в карте беседы
    return deleted


def chats_with(uid, path):
    """Беседы, в files/general которых лежит файл библиотеки path."""
    return [meta for meta in list_chats(uid) if path in links(uid, meta["id"]).values()]


def delete_library_file(uid, path):
    """Удаляет файл из библиотеки и из files/general всех бесед. False — нет такого файла."""
    return bool(delete_library_files(uid, [path]))


def delete_library_files(uid, paths):
    """Удаляет файлы из библиотеки и из files/general всех бесед; беседы обходятся один
    раз на всю пачку. Возвращает пути, которые удалены (несуществующие пропускаются)."""
    root = library(uid)
    targets = {}  # номер файла -> (путь в библиотеке, файл)
    for path in paths:
        target = session_files.contained(root, path)
        try:
            info = target.lstat() if target else None
        except OSError:
            info = None
        if info is not None and stat.S_ISREG(info.st_mode):
            targets.setdefault(session_files.inode(info), (path, target))
    if not targets:
        return []
    keys = frozenset(targets)
    for meta in list_chats(uid):
        cwd = os.path.join(data_dir(uid), "workspaces", meta["id"])
        if not os.path.isdir(cwd):
            continue
        found = [p for p in session_files.queued(cwd, "general", keys) if session_files.inode(p.lstat()) in keys]
        for p in found:
            p.unlink()
        if found:
            shelve(uid, meta["id"])
    for _, target in targets.values():
        target.unlink(missing_ok=True)
    return [path for path, _ in targets.values()]


def shelve(uid, chat):
    """Новое и перезаписанное в files/general беседы — в библиотеку."""
    path = chat_file(uid, chat, ".links.json")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    return session_files.shelve(workspace(uid, chat), library(uid), path)
