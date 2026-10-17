"""Идущие ответы: проверки, запуск модели и журнал беседы. Общие для сайта и бота."""
import asyncio
import collections
import logging
import time

import core
import usage
import users

log = logging.getLogger("nexus")
IMAGE_TYPES = {"image/jpeg", "image/png", "image/gif", "image/webp"}


class Refused(Exception):
    """Отказ запустить ответ: сайт отдаёт его HTTP-кодом, бот — текстом."""

    def __init__(self, status, text):
        super().__init__(text)
        self.status = status


class Job:
    """Идущий ответ. Живёт отдельно от HTTP: закрытая вкладка его не обрывает."""

    def __init__(self):
        self.proc = None
        self.task = None  # ссылка держит задачу от сборщика мусора
        self.stopped = False
        self.cond = asyncio.Condition()

    def attach(self, proc):
        self.proc = proc
        if self.stopped:  # «Стоп» нажали раньше, чем процесс успел запуститься
            core.stop(proc)


# Один ответ на беседу за раз: одна сессия CLI не переживёт два параллельных --resume.
jobs = {}  # (uid, chat) -> Job
watchers = collections.Counter()  # (uid, chat) -> открытые вкладки с этой беседой
notify = None  # async (uid, text): бот Telegram, если запущен


def in_tg(meta):
    """Беседа идёт в Telegram: писать в неё с сайта нельзя, пока её не перенесут."""
    return bool(meta.get("tg")) and not meta["tg"].get("moved")


def allowed_model(avail, engine, model, effort):
    """Текст отказа, если пользователю нельзя эту сеть, модель или уровень."""
    found = next((m for m in avail.get(engine, []) if m["slug"] == model), None)
    if engine not in avail:
        return "Эта сеть вам недоступна"
    if not found:
        return "Эта модель вам недоступна — выберите другую"
    if effort not in found["efforts"] and not (effort == "" and found["auto"]):
        return "Этот уровень размышления вам недоступен — выберите другой"
    return None


def default_effort(avail, engine, model):
    m = next(m for m in avail[engine] if m["slug"] == model)
    return "" if m["auto"] else m["efforts"][0]


def new_chat(uid, engine, **extra):
    """Беседа с моделью по умолчанию, если она пользователю доступна."""
    avail = users.available(users.settings(uid))
    if engine not in avail:
        raise Refused(403, "Эта сеть вам недоступна")
    slugs = [m["slug"] for m in avail[engine]]
    model = core.DEFAULT_MODEL if core.DEFAULT_MODEL in slugs else slugs[0]
    meta = core.create_chat(uid, engine, model, default_effort(avail, engine, model))
    return core.save_chat(uid, {**meta, **extra}) if extra else meta


def human(n):
    for unit in ("Б", "КБ", "МБ", "ГБ"):
        if n < 1024 or unit == "ГБ":
            return f"{n:.0f} {unit}" if unit == "Б" else f"{n:.1f} {unit}"
        n /= 1024


def check(uid, meta, incoming=0):
    """Настройки пользователя, если ему можно отправить сообщение в эту беседу."""
    data = users.settings(uid)
    reason = allowed_model(users.available(data), meta["engine"], meta["model"], meta["effort"])
    if reason:
        raise Refused(403, reason)
    reason = usage.check(uid, meta["engine"], data)
    if reason:
        raise Refused(429, reason)
    room(uid, data, incoming)
    return data


def room(uid, data, incoming):
    """Влезут ли новые файлы в лимит памяти пользователя."""
    quota = data["quota_mb"] * 2 ** 20
    if quota and incoming:
        used = usage.disk(uid)["total"]
        if used + incoming > quota:
            raise Refused(507, f"Память закончилась: занято {human(used)} из {human(quota)}, "
                               f"а файлы весят {human(incoming)}. Удалите ненужные беседы "
                               "или попросите администратора увеличить лимит.")


transcribing = collections.Counter()  # (uid, chat) -> сколько голосовых сейчас расшифровывается


async def start(uid, meta, text, data, saved=(), images=(), channel="web"):
    """Запуск ответа. saved — имена уже сохранённых в files/from_user вложений."""
    key = (uid, meta["id"])
    if key in jobs:
        raise Refused(409, "Дождитесь ответа или остановите его")
    jobs[key] = job = Job()  # занимаем беседу до первого await
    notes = "\n".join(f"Вложение: files/from_user/{name}" for name in saved)
    prompt = "\n\n".join(p for p in (text, notes) if p)
    if not meta["title"]:
        meta["title"] = text.strip().replace("\n", " ")[:60]
    meta["updated"] = time.time()
    core.save_chat(uid, meta)
    await push(key, job, {"kind": "user", "text": text, "files": list(saved), "ts": time.time()})
    job.task = asyncio.create_task(work(uid, meta, prompt, list(images), job, data, channel))
    return job


async def push(key, job, event):
    async with job.cond:
        core.append_history(*key, event)
        job.cond.notify_all()


async def work(uid, meta, prompt, images, job, data, channel="web"):
    key, ok = (uid, meta["id"]), False
    engine, started = meta["engine"], time.time()
    points, tokens, limited = [], [0, 0], None
    try:
        async for event in core.run(uid, meta, prompt, data["access"][engine], images,
                                    on_proc=job.attach, channel=channel):
            if event["kind"] == "limits":
                points.append(event)
                limited = limited or usage.check(uid, engine, data, points, started)
                if limited and job.proc:
                    core.stop(job.proc)  # лимит исчерпан посреди ответа
            elif event["kind"] == "tokens":
                tokens[0] += event["in"]
                tokens[1] += event["out"]
            else:
                await push(key, job, event)
        ok = True
    except Exception as e:
        if not (job.stopped or limited):
            log.exception("model failed")
        await push(key, job, {"kind": "error", "text": limited} if limited
                   else {"kind": "stopped"} if job.stopped else {"kind": "error", "text": str(e)})
    finally:
        try:
            if engine == "codex":
                points += core.codex_limits(uid, meta["sid"], started)
            usage.charge(uid, engine, points, started, tokens)
        except Exception:
            log.exception("usage failed")
        files = []
        try:
            files = core.finish(uid, meta["id"], ok)
        except Exception:
            log.exception("files failed")
        meta["updated"] = time.time()
        core.save_chat(uid, meta)  # у codex здесь появляется id сессии
        quota = data["quota_mb"] * 2 ** 20
        if quota and usage.disk(uid)["total"] > quota:
            await push(key, job, {"kind": "warning", "text": "Память закончилась: новые файлы загрузить "
                                  "не получится. Удалите ненужные беседы или попросите администратора "
                                  "увеличить лимит."})
        async with job.cond:
            core.append_history(uid, meta["id"], {"kind": "done", "files": files, "ts": time.time()})
            del jobs[key]
            job.cond.notify_all()
        if notify and not in_tg(meta) and not watchers[key]:
            # Вкладку закрыли, не дождавшись: ответ готов — скажем в Telegram.
            await notify(uid, f"Ответ готов: {meta['title'] or 'беседа без названия'}")
