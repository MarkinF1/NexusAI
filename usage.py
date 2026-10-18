"""Учёт подписки и диска: сколько процентов окон и места занял пользователь.

Подписка одна на всех, и сколько потратил конкретный запрос, она не сообщает —
видны только общие проценты 5-часового и недельного окна. Поэтому пользователю
списывается сдвиг общего процента за время его запуска.
ponytail: использование подписки мимо сайта (TG-бот, свой CLI) в момент запуска
спишется на пользователя сайта, а одновременные запуски получат общий сдвиг оба —
лимиты срабатывают с запасом. Точность ~1%: окна отдаются целыми процентами.
"""
import asyncio
import json
import logging
import os
import time
import urllib.request
from datetime import datetime

import core
import settings
import users

log = logging.getLogger("nexus.usage")
FRESH = 300  # общий снимок старше этого не годится как база: сдвиг мог сделать кто-то ещё
LIMIT_KEYS = {"five_hour": "limit_5h", "seven_day": "limit_week"}
NAMES = {"five_hour": "на 5 часов", "seven_day": "на неделю"}


def _load(path):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def _save(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path + ".tmp", "w", encoding="utf-8") as f:
        json.dump(data, f)
    os.replace(path + ".tmp", path)


def global_path():
    return os.path.join(core.DATA, "limits.json")


def user_path(uid):
    return os.path.join(core.user_dir(uid), "usage.json")


# Свежая занятость подписки: те же адреса, что у /usage в claude и /status в codex.
# Как часто — настройка usage_refresh. Так видно и расход мимо сайта (свой CLI, другие программы), а не только снимок
# последнего запуска на сайте.
CLAUDE_URL = "https://api.anthropic.com/api/oauth/usage"
CODEX_URL = "https://chatgpt.com/backend-api/wham/usage"
CODEX_WINDOWS = {18000: "five_hour", 604800: "seven_day"}


def _get(url, headers):
    request = urllib.request.Request(url, headers={"User-Agent": "nexus-ai", **headers})
    with urllib.request.urlopen(request, timeout=15) as response:
        return json.load(response)


def _stamp(value):
    return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp() if value else 0


def fetch_claude():
    """{окно: (процент, сброс)}. Токен берём общий, обновляет его сам CLI.
    ponytail: если сайт долго не запускал claude и токен истёк, опрос молчит до запуска."""
    token = _load(core.HOST_CREDS)["claudeAiOauth"]["accessToken"]
    data = _get(CLAUDE_URL, {"Authorization": f"Bearer {token}", "anthropic-beta": "oauth-2025-04-20"})
    return {window: (float(w["utilization"]), _stamp(w.get("resets_at")))
            for window in core.WINDOWS if (w := data.get(window) or {}).get("utilization") is not None}


def fetch_codex():
    tokens = _load(core.CODEX_HOST_AUTH)["tokens"]
    data = _get(CODEX_URL, {"Authorization": f"Bearer {tokens['access_token']}",
                            "ChatGPT-Account-Id": tokens.get("account_id", "")})
    found = {}
    for w in ((data.get("rate_limit") or {}).get(k) for k in ("primary_window", "secondary_window")):
        window = CODEX_WINDOWS.get((w or {}).get("limit_window_seconds"))
        if window:
            found[window] = (float(w["used_percent"]), w.get("reset_at") or 0)
    return found


FETCH = {"claude": fetch_claude, "codex": fetch_codex}
failing = set()


def store(engine, found):
    snapshot = _load(global_path())
    shared = snapshot.setdefault(engine, {})
    for window, (percent, resets) in found.items():
        shared[window] = {"percent": percent, "resets_at": resets, "seen": time.time()}
    _save(global_path(), snapshot)


def refresh(engine):
    """Обновить общий снимок с сервера подписки; False — не получилось."""
    try:
        found = FETCH[engine]()
    except Exception as e:
        if engine not in failing:  # одна запись на полосу неудач, а не каждые две минуты
            log.warning("usage %s: %s", engine, e)
        failing.add(engine)
        return False
    failing.discard(engine)
    if found:
        store(engine, found)
    return True


async def poll():
    """Опрос раз в usage_refresh секунд; 0 — пауза, настройку проверяем раз в минуту."""
    while True:
        every = settings.get(settings.USAGE_REFRESH.name)
        if every > 0:
            for engine in core.engines():
                await asyncio.to_thread(refresh, engine)
        await asyncio.sleep(every or 60)


def same_window(a, b):
    # Время сброса у окна фиксировано; небольшой допуск на округление.
    return abs((a or 0) - (b or 0)) < 600


def tally(uid, engine, points, started, base=None):
    """(новый расход пользователя, новый общий снимок) после запуска; ничего не пишет.

    points — события limits запуска в порядке поступления; base — общий снимок на момент
    старта (опрос посреди запуска уже включает часть его расхода и базой быть не может).
    """
    snapshot, mine = _load(global_path()), _load(user_path(uid))
    shared, own = snapshot.setdefault(engine, {}), mine.setdefault(engine, {})
    before = shared if base is None else (base.get(engine) or {})
    for window in core.WINDOWS:
        seen = [p for p in points if p["window"] == window]
        if not seen:
            continue
        last = seen[-1]
        prev = before.get(window)
        if prev and same_window(prev["resets_at"], last["resets_at"]) and started - prev["seen"] < FRESH:
            base = prev["percent"]
        elif same_window(seen[0]["resets_at"], last["resets_at"]):
            base = seen[0]["percent"]  # первый снимок уже включает первый вызов модели
        else:
            base = 0  # окно сменилось прямо во время запуска
        record = own.get(window)
        if not record or not same_window(record["resets_at"], last["resets_at"]):
            record = {"resets_at": last["resets_at"], "used": 0}
        record["used"] = round(record["used"] + max(0, last["percent"] - base), 2)
        own[window] = record
        shared[window] = {"percent": last["percent"], "resets_at": last["resets_at"], "seen": time.time()}
    return mine, snapshot


def charge(uid, engine, points, started, tokens=(0, 0), base=None):
    mine, snapshot = tally(uid, engine, points, started, base)
    totals = mine[engine].setdefault("tokens", {"in": 0, "out": 0})
    totals["in"] += tokens[0]
    totals["out"] += tokens[1]
    _save(user_path(uid), mine)
    if points:
        _save(global_path(), snapshot)


def spent(mine, engine):
    """{окно: {used, resets_at}} с учётом сброса окна по времени."""
    result = {}
    for window in core.WINDOWS:
        record = (mine.get(engine) or {}).get(window)
        if record and record["resets_at"] > time.time():
            result[window] = record
        else:
            result[window] = {"used": 0, "resets_at": 0}
    return result


def subscription(snapshot, engine, window):
    """(сколько процентов окна подписки занято, сброс); окно уже сбросилось — (0, 0)."""
    record = (snapshot.get(engine) or {}).get(window)
    if record and record["resets_at"] > time.time():
        return record["percent"], record["resets_at"]
    return 0, 0


def windows(mine, snapshot, engine, conf):
    """По окнам: свой расход (used), лимит, занятость подписки и share — сколько процентов
    своей части уже нет. Часть пропадает и от своих запросов, и от чужих: если подписки
    осталось меньше, чем пользователю по лимиту, доступен только остаток подписки.
    Двое по 60%: первый потратил 60% — у него 100%, у второго осталось 40 из 60 — 33.33%."""
    result = {}
    for window, own in spent(mine, engine).items():
        limit = conf[LIMIT_KEYS[window]]
        total, resets = subscription(snapshot, engine, window)
        left = max(0.0, min(limit - own["used"], 100 - total))
        share = round(100 - left / limit * 100, 2) if limit else 100
        result[window] = {"used": own["used"], "limit": limit, "subscription": total, "share": share,
                          "resets_at": own["resets_at"] or resets}
    return result


def summary(uid, data):
    """Расход и лимиты пользователя по всем сетям, токены за всё время."""
    mine, snapshot = _load(user_path(uid)), _load(global_path())
    result = {}
    for engine in core.ENGINES:
        result[engine] = windows(mine, snapshot, engine, users.engine_conf(data, engine))
        result[engine]["tokens"] = (mine.get(engine) or {}).get("tokens", {"in": 0, "out": 0})
    return result


def over(state):
    """Текст «лимит исчерпан» по первому окну, где своей части не осталось, или None."""
    for window in core.WINDOWS:
        w = state[window]
        if w["share"] < 100:
            continue
        reset = time.strftime("%d.%m %H:%M", time.localtime(w["resets_at"])) if w["resets_at"] else "—"
        if w["used"] >= w["limit"]:
            return f"Лимит {NAMES[window]} исчерпан ({w['used']:g}% из {w['limit']}%), сброс {reset}"
        return (f"Лимит {NAMES[window]} исчерпан: подписка занята на {w['subscription']:g}%, "
                f"вашей части не осталось, сброс {reset}")
    return None


def check(uid, engine, data, points=(), started=0, base=None):
    """Превышен ли лимит; с points — с учётом ещё не списанного текущего запуска."""
    if points:
        mine, snapshot = tally(uid, engine, list(points), started, base)
    else:
        mine, snapshot = _load(user_path(uid)), _load(global_path())
    return over(windows(mine, snapshot, engine, users.engine_conf(data, engine)))


def disk(uid):
    """Байты в папке пользователя: сессии CLI и история отдельно от файлов.

    ponytail: обход на каждый запрос; кэшировать, если папки станут большими.
    """
    sizes, seen = {}, set()
    for part in ("claude", "codex-home", "chats", "data"):
        total = 0
        for current, _, names in os.walk(os.path.join(core.user_dir(uid), part)):
            for name in names:
                try:
                    info = os.lstat(os.path.join(current, name))
                except OSError:
                    continue
                # Файл беседы и его ссылка в библиотеке — одно место на диске.
                if info.st_nlink > 1:
                    if (info.st_dev, info.st_ino) in seen:
                        continue
                    seen.add((info.st_dev, info.st_ino))
                total += info.st_size
        sizes[part] = total
    files = sizes.pop("data")
    sessions = sum(sizes.values())
    return {"sessions": sessions, "files": files, "total": sessions + files}


def global_snapshot():
    return _load(global_path())
