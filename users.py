"""Пользователи сайта и их входы: sqlite из стандартной библиотеки.

Создать первого администратора: python users.py add <имя> --admin
"""
import argparse
import getpass
import hashlib
import hmac
import json
import os
import re
import secrets
import shlex
import sqlite3
import time

import core
import settings as general  # общие настройки сервиса; settings() здесь — настройки пользователя

LOGIN_TTL = 30 * 24 * 3600
LOCK_AFTER, LOCK_FOR = 5, 15 * 60  # столько неудачных входов подряд — и вход по имени закрыт на 15 мин
# Порог «Сжать беседу» у тех, кому его ещё не задали; 0 — не предлагать.
# Галочки «Доступ» у каждой сети свои: что модели можно делать в песочнице.
# Значение — умолчание для того, у кого галочки ещё нет: прежние права остаются,
# новые ограничения выключены.
ACCESS = {"claude": {"bash": True, "write": True, "memory": True, "network": True, "web": True,
                     "all_chats": True, "site_allow": False, "site_deny": False, "commands": False,
                     "unsandboxed": False, "skills": True},
          "codex": {"bash": True, "write": True, "memory": True, "network": True,
                    "web": True, "web_cached": False, "all_chats": True,
                    "multi_agent": True, "site_allow": False, "site_deny": False, "unsandboxed": False}}
# Списки к галочкам site_allow, site_deny, commands и unsandboxed; skills_off — снятые скиллы.
LISTS = {"claude": ("allowed_sites", "denied_sites", "allowed_commands", "denied_commands",
                    "unsandboxed_commands", "skills_off"),
         "codex": ("allowed_sites", "denied_sites", "unsandboxed_commands")}
SITE = re.compile(r"(\*\.)?([a-z0-9-]+\.)*[a-z0-9-]+")


def db():
    os.makedirs(core.DATA, exist_ok=True)
    conn = sqlite3.connect(os.path.join(core.DATA, "nexus.db"))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY,
            name TEXT UNIQUE NOT NULL,
            pw TEXT NOT NULL,
            is_admin INTEGER NOT NULL DEFAULT 0);
        CREATE TABLE IF NOT EXISTS logins (
            token TEXT PRIMARY KEY,
            user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            expires REAL NOT NULL);
        CREATE TABLE IF NOT EXISTS tg_groups (
            chat_id INTEGER PRIMARY KEY,
            owner INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE);
        CREATE TABLE IF NOT EXISTS login_failures (
            name TEXT PRIMARY KEY,
            count INTEGER NOT NULL,
            last REAL NOT NULL);
        CREATE TABLE IF NOT EXISTS tg_codes (
            code TEXT PRIMARY KEY,  -- sha256 кода: сам код есть только в ссылке
            user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            expires REAL NOT NULL);
    """)
    columns = [r["name"] for r in conn.execute("PRAGMA table_info(users)")]
    for name, kind in (("settings", "TEXT NOT NULL DEFAULT '{}'"), ("tg_id", "INTEGER"), ("tg_name", "TEXT"),
                       ("tg_show", "INTEGER NOT NULL DEFAULT 0"), ("tg_notify", "INTEGER NOT NULL DEFAULT 1")):
        if name not in columns:
            conn.execute(f"ALTER TABLE users ADD COLUMN {name} {kind}")
    conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS users_tg ON users(tg_id)")
    return conn


def hash_password(password):
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, n=2 ** 14, r=8, p=1)
    return salt.hex() + "$" + digest.hex()


def verify_password(password, stored):
    salt, digest = stored.split("$")
    actual = hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt), n=2 ** 14, r=8, p=1)
    return hmac.compare_digest(actual.hex(), digest)


def public(row):
    return {"id": row["id"], "name": row["name"], "is_admin": bool(row["is_admin"]),
            "tg": row["tg_id"] and {"id": row["tg_id"], "name": row["tg_name"],
                                    "show": bool(row["tg_show"]), "notify": bool(row["tg_notify"])}}


def add(name, password, admin=False):
    name = name.strip()
    if not name or not password:
        raise ValueError("Нужны имя и пароль")
    with db() as conn:
        try:
            cur = conn.execute("INSERT INTO users (name, pw, is_admin) VALUES (?, ?, ?)",
                               (name, hash_password(password), int(admin)))
        except sqlite3.IntegrityError:
            raise ValueError(f"Пользователь {name} уже есть") from None
        return cur.lastrowid


def set_password(user_id, password):
    if not password:
        raise ValueError("Пустой пароль")
    with db() as conn:
        conn.execute("UPDATE users SET pw = ? WHERE id = ?", (hash_password(password), user_id))
        # Смена пароля выкидывает все открытые входы.
        conn.execute("DELETE FROM logins WHERE user_id = ?", (user_id,))


def delete(user_id):
    with db() as conn:
        conn.execute("DELETE FROM users WHERE id = ?", (user_id,))


def listing():
    with db() as conn:
        return [public(r) for r in conn.execute("SELECT * FROM users ORDER BY id")]


def check(name, password):
    with db() as conn:
        row = conn.execute("SELECT * FROM users WHERE name = ?", (name.strip(),)).fetchone()
    if row and verify_password(password, row["pw"]):
        return public(row)
    return None


def login(user_id):
    token = secrets.token_urlsafe(32)
    with db() as conn:
        conn.execute("DELETE FROM logins WHERE expires < ?", (time.time(),))
        conn.execute("INSERT INTO logins VALUES (?, ?, ?)", (token, user_id, time.time() + LOGIN_TTL))
    return token


def by_token(token):
    if not token:
        return None
    with db() as conn:
        row = conn.execute("SELECT users.* FROM logins JOIN users ON users.id = logins.user_id "
                           "WHERE token = ? AND expires > ?", (token, time.time())).fetchone()
    return public(row) if row else None


def logout(token):
    with db() as conn:
        conn.execute("DELETE FROM logins WHERE token = ?", (token,))


# Неудачные входы — в базе: перезапуск сервиса не обнуляет счётчик подбора пароля.

def locked(name):
    """Вход по имени закрыт: LOCK_AFTER неудач подряд, последняя — меньше LOCK_FOR назад."""
    with db() as conn:
        row = conn.execute("SELECT count, last FROM login_failures WHERE name = ?", (name,)).fetchone()
    return bool(row) and row["count"] >= LOCK_AFTER and time.time() - row["last"] < LOCK_FOR


def failed(name):
    """Ещё одна неудача. Если прошлая была дольше LOCK_FOR назад, счёт начинается заново."""
    now = time.time()
    with db() as conn:
        conn.execute("DELETE FROM login_failures WHERE last < ?", (now - LOCK_FOR,))  # старые не копим
        conn.execute("INSERT INTO login_failures VALUES (?, 1, ?) "
                     "ON CONFLICT(name) DO UPDATE SET count = count + 1, last = excluded.last", (name, now))


def succeeded(name):
    with db() as conn:
        conn.execute("DELETE FROM login_failures WHERE name = ?", (name,))


# Коды привязки Telegram — в базе: ссылка с сайта работает и после перезапуска.

def code_hash(code):
    return hashlib.sha256(code.encode()).hexdigest()


def new_tg_code(user_id, ttl):
    code = secrets.token_urlsafe(16)
    now = time.time()
    with db() as conn:
        conn.execute("DELETE FROM tg_codes WHERE expires < ?", (now,))
        conn.execute("INSERT INTO tg_codes VALUES (?, ?, ?)", (code_hash(code), user_id, now + ttl))
    return code


def take_tg_code(code):
    """id пользователя по коду; код одноразовый. None — нет такого или срок вышел."""
    with db() as conn:
        row = conn.execute("DELETE FROM tg_codes WHERE code = ? RETURNING user_id, expires",
                           (code_hash(code),)).fetchone()
    return row["user_id"] if row and row["expires"] >= time.time() else None


# --- Telegram ---

def link_tg(user_id, tg_id, tg_name):
    """Привязка Telegram-аккаунта; занятый другим пользователем аккаунт — ошибка."""
    with db() as conn:
        try:
            conn.execute("UPDATE users SET tg_id = ?, tg_name = ? WHERE id = ?", (tg_id, tg_name, user_id))
        except sqlite3.IntegrityError:
            raise ValueError("Этот Telegram уже привязан к другому пользователю") from None


def unlink_tg(user_id):
    with db() as conn:
        conn.execute("UPDATE users SET tg_id = NULL, tg_name = NULL WHERE id = ?", (user_id,))
        conn.execute("DELETE FROM tg_groups WHERE owner = ?", (user_id,))


def tg_prefs(user_id, show=None, notify=None):
    with db() as conn:
        if show is not None:
            conn.execute("UPDATE users SET tg_show = ? WHERE id = ?", (int(show), user_id))
        if notify is not None:
            conn.execute("UPDATE users SET tg_notify = ? WHERE id = ?", (int(notify), user_id))


def get(user_id):
    with db() as conn:
        row = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
    return public(row) if row else None


def by_tg(tg_id):
    with db() as conn:
        row = conn.execute("SELECT * FROM users WHERE tg_id = ?", (tg_id,)).fetchone()
    return public(row) if row else None


def group_owner(chat_id):
    with db() as conn:
        row = conn.execute("SELECT owner FROM tg_groups WHERE chat_id = ?", (chat_id,)).fetchone()
    return row["owner"] if row else None


def set_group_owner(chat_id, user_id):
    with db() as conn:
        conn.execute("INSERT OR REPLACE INTO tg_groups VALUES (?, ?)", (chat_id, user_id))


# --- Права пользователя ---
# Отсутствующий ключ значит «разрешено» или «без лимита»: так новые модели
# из каталога codex доступны сразу, а старые пользователи ничего не теряют.

def settings(user_id):
    with db() as conn:
        row = conn.execute("SELECT settings FROM users WHERE id = ?", (user_id,)).fetchone()
    return clean(json.loads(row["settings"]) if row else {})


def save_settings(user_id, raw):
    data = clean(raw)
    with db() as conn:
        conn.execute("UPDATE users SET settings = ? WHERE id = ?", (json.dumps(data), user_id))
    return data


def percent(value):
    value = int(value)
    if not 0 <= value <= 100:
        raise ValueError("Лимит — от 0 до 100%")
    return value


def clean(raw):
    """Настройки с умолчаниями; мусор и неизвестные ключи отбрасываются."""
    quota = int(raw.get("quota_mb") or 0)
    if quota < 0:
        raise ValueError("Квота не может быть отрицательной")
    compact = int(raw.get("compact_mb", general.get(general.COMPACT_MB.name)) or 0)
    if compact < 0:
        raise ValueError("Порог сжатия не может быть отрицательным")
    access = raw.get("access") or {}
    engines = {}
    for engine, conf in (raw.get("engines") or {}).items():
        if engine not in core.ENGINES:
            raise ValueError(f"Неизвестная сеть {engine}")
        engines[engine] = {
            "on": bool(conf.get("on", True)),
            "limit_5h": percent(conf.get("limit_5h", 100)),
            "limit_week": percent(conf.get("limit_week", 100)),
            "models": {str(slug): {"on": bool(m.get("on", True)),
                                   "efforts": [str(e) for e in m.get("efforts", [])]}
                       for slug, m in (conf.get("models") or {}).items()},
        }
    return {"quota_mb": quota,
            "compact_mb": compact,  # сессия больше — предложить «Сжать беседу»; 0 — не предлагать
            "access": {engine: engine_access(access, engine) for engine in ACCESS},
            "engines": engines}


def engine_access(access, engine):
    conf = access.get(engine)
    if not isinstance(conf, dict):
        conf = access  # старый формат: одни галочки на все сети
    result = {k: bool(conf.get(k, default)) for k, default in ACCESS[engine].items()}
    if "memory" in result and "memory" not in conf:
        result["memory"] = result["write"]  # раньше память шла под «Изменением файлов»
    if engine == "codex" and not result["write"]:
        result["memory"] = False  # CLI не поддерживает writable-root в виде отдельного файла
    for key in LISTS[engine]:
        check = (site if key.endswith("sites") else skill if key == "skills_off"
                 else codex_command if engine == "codex" else command)
        result[key] = [check(v) for v in conf.get(key) or [] if str(v).strip()]
    return result


def site(value):
    """«https://Пример.рф/путь» -> «xn--e1afmkfd.xn--p1ai»: в правило CLI идёт только хост."""
    host = re.sub(r"^[a-z]+://", "", str(value).strip().lower()).split("/")[0]
    try:
        host = host.encode("idna").decode()
    except UnicodeError:
        pass  # SITE ниже отклонит
    if not SITE.fullmatch(host):
        raise ValueError(f"Не похоже на адрес сайта: {value}")
    return host


def skill(value):
    value = str(value).strip()
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]*", value):
        raise ValueError(f"Неверное имя скилла: {value}")
    return value


def command(value):
    value = " ".join(str(value).split())
    # Скобки закрыли бы правило Bash(...) раньше времени.
    if len(value) > 200 or "(" in value or ")" in value:
        raise ValueError(f"Команда должна быть без скобок и короче 200 знаков: {value}")
    return value


def codex_command(value):
    """У codex правило — начало команды по словам: без звёздочек, кавычки закрыты."""
    value = command(value)
    try:
        words = shlex.split(value)
    except ValueError:
        words = []
    if not words or "*" in value:
        raise ValueError(f"Команда для Codex — без звёздочек и с закрытыми кавычками: {value}")
    return value


def engine_conf(data, engine):
    return data["engines"].get(engine) or {"on": True, "limit_5h": 100, "limit_week": 100, "models": {}}


def available(data):
    """Сети и модели, доступные пользователю: в .env, в системе и в его правах.

    У модели efforts — разрешённые уровни, auto — можно ли не задавать уровень
    (только если ни один уровень не урезан: иначе CLI мог бы выбрать запрещённый).
    """
    catalog = core.models()
    result = {}
    for engine in core.engines():
        conf = engine_conf(data, engine)
        if not conf["on"]:
            continue
        listed = []
        for m in catalog[engine]:
            rule = conf["models"].get(m["slug"])
            if rule and not rule["on"]:
                continue
            efforts = [e for e in m["efforts"] if not rule or e in rule["efforts"]]
            if m["efforts"] and not efforts:
                continue  # сняты все уровни — модель недоступна
            listed.append({**m, "efforts": efforts, "auto": efforts == m["efforts"]})
        if listed:
            result[engine] = listed
    return result


def main():
    parser = argparse.ArgumentParser(description="Пользователи NexusAI")
    sub = parser.add_subparsers(dest="cmd", required=True)
    cmd = sub.add_parser("add", help="создать пользователя")
    cmd.add_argument("name")
    cmd.add_argument("--admin", action="store_true")
    sub.add_parser("passwd", help="сменить пароль").add_argument("name")
    sub.add_parser("list", help="список пользователей")
    args = parser.parse_args()
    if args.cmd == "list":
        for u in listing():
            print(u["id"], u["name"], "(админ)" if u["is_admin"] else "")
        return
    password = getpass.getpass("Пароль: ")
    if password != getpass.getpass("Ещё раз: "):
        raise SystemExit("Пароли не совпали")
    if args.cmd == "add":
        print("Создан пользователь", add(args.name, password, args.admin))
    else:
        found = [u for u in listing() if u["name"] == args.name]
        if not found:
            raise SystemExit("Нет такого пользователя")
        set_password(found[0]["id"], password)
        print("Пароль изменён")


if __name__ == "__main__":
    main()
