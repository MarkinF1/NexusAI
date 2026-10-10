"""Пользователи сайта и их входы: sqlite из стандартной библиотеки.

Создать первого администратора: python users.py add <имя> --admin
"""
import argparse
import getpass
import hashlib
import hmac
import json
import os
import secrets
import sqlite3
import time

import core

LOGIN_TTL = 30 * 24 * 3600
# Галочки «Доступ» у каждой сети свои: что модели можно делать в песочнице.
# Shell у codex не отключается, поэтому галочки «Запуск команд» у него нет.
ACCESS = {"claude": ("bash", "write", "network", "web", "all_chats"),
          "codex": ("write", "network", "web", "all_chats")}


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
    """)
    if "settings" not in [r["name"] for r in conn.execute("PRAGMA table_info(users)")]:
        conn.execute("ALTER TABLE users ADD COLUMN settings TEXT NOT NULL DEFAULT '{}'")
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
    return {"id": row["id"], "name": row["name"], "is_admin": bool(row["is_admin"])}


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
            "access": {engine: engine_access(access, engine) for engine in ACCESS},
            "engines": engines}


def engine_access(access, engine):
    conf = access.get(engine)
    if not isinstance(conf, dict):
        conf = access  # старый формат: одни галочки на все сети
    return {k: bool(conf.get(k, True)) for k in ACCESS[engine]}


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
