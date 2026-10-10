"""Веб-сервер NexusAI: вход, беседы, стриминг ответов, файлы, пользователи.

Запуск: .venv/bin/python app.py (адрес — NEXUS_HOST/NEXUS_PORT).
"""
import asyncio
import json
import logging
import os
import shutil
import time
from pathlib import Path

import uvicorn
from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, Response, UploadFile
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

import core
import session_files
import usage
import users

COOKIE = "nexus"
IMAGE_TYPES = {"image/jpeg", "image/png", "image/gif", "image/webp"}
MAX_UPLOAD = 50 * 1024 * 1024
LOCK_AFTER, LOCK_FOR = 5, 15 * 60

log = logging.getLogger("nexus")
app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
# ponytail: счётчик неудачных входов в памяти — сбрасывается рестартом,
# для одного сервера этого достаточно.
failures = {}  # имя -> (неудач подряд, время последней)


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


@app.middleware("http")
async def security_headers(request: Request, call_next):
    response = await call_next(request)
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; img-src 'self' data: blob:; style-src 'self' 'unsafe-inline'")
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["X-Content-Type-Options"] = "nosniff"
    # Статику браузер сверяет с сервером при каждой загрузке (по ETag — без
    # повторного скачивания). Иначе он часами держит старый style.css рядом
    # с новыми index.html и app.js, и вёрстка разваливается.
    response.headers.setdefault("Cache-Control", "no-cache")
    return response


def current_user(request: Request):
    user = users.by_token(request.cookies.get(COOKIE))
    if not user:
        raise HTTPException(401, "Нужно войти")
    return user


def admin_user(user=Depends(current_user)):
    if not user["is_admin"]:
        raise HTTPException(403, "Только для администратора")
    return user


def own_chat(user, chat):
    meta = core.load_chat(user["id"], chat)
    if not meta:
        raise HTTPException(404, "Нет такой беседы")
    return meta


def idle(user, chat):
    if (user["id"], chat) in jobs:
        raise HTTPException(409, "Дождитесь ответа или остановите его")


# --- Вход ---

class Credentials(BaseModel):
    name: str
    password: str


@app.post("/api/login")
def login(body: Credentials, request: Request, response: Response):
    # Обычная def: FastAPI выполнит её в пуле потоков, scrypt и sleep не держат цикл.
    name = body.name.strip()
    count, last = failures.get(name, (0, 0))
    if count >= LOCK_AFTER and time.time() - last < LOCK_FOR:
        raise HTTPException(429, "Слишком много попыток, подождите 15 минут")
    user = users.check(name, body.password)
    if not user:
        failures[name] = (count + 1, time.time())
        time.sleep(1)
        raise HTTPException(401, "Неверное имя или пароль")
    failures.pop(name, None)
    secure = (request.url.scheme == "https"
              or request.headers.get("x-forwarded-proto") == "https")
    response.set_cookie(COOKIE, users.login(user["id"]), max_age=users.LOGIN_TTL,
                        httponly=True, samesite="strict", secure=secure)
    return user


@app.post("/api/logout")
def logout(request: Request, response: Response):
    users.logout(request.cookies.get(COOKIE, ""))
    response.delete_cookie(COOKIE)
    return {}


@app.get("/api/me")
def me(user=Depends(current_user)):
    """Пользователь и сводка для боковой панели: сети, диск, расход подписки."""
    data = users.settings(user["id"])
    avail = users.available(data)
    spent = usage.summary(user["id"], data)
    return {**user, "engines": list(avail),
            "disk": {"used": usage.disk(user["id"])["total"], "quota": data["quota_mb"] * 2 ** 20},
            "limits": {e: spent[e] for e in avail}}


# --- Беседы ---

def allowed_model(avail, engine, model, effort):
    """Текст отказа, если пользователю нельзя эту сеть, модель или уровень."""
    found = next((m for m in avail.get(engine, []) if m["slug"] == model), None)
    if engine not in avail:
        return "Эта сеть вам недоступна"
    if not found:
        return "Эта модель вам недоступна — выберите другую в шапке беседы"
    if effort not in found["efforts"] and not (effort == "" and found["auto"]):
        return "Этот уровень размышления вам недоступен — выберите другой в шапке беседы"
    return None


def default_effort(avail, engine, model):
    m = next(m for m in avail[engine] if m["slug"] == model)
    return "" if m["auto"] else m["efforts"][0]


@app.get("/api/models")
def models(user=Depends(current_user)):
    return users.available(users.settings(user["id"]))


@app.get("/api/chats")
def chats(user=Depends(current_user)):
    return [{**m, "running": (user["id"], m["id"]) in jobs} for m in core.list_chats(user["id"])]


class NewChat(BaseModel):
    engine: str


@app.post("/api/chats")
def new_chat(body: NewChat, user=Depends(current_user)):
    avail = users.available(users.settings(user["id"]))
    if body.engine not in avail:
        raise HTTPException(403, "Эта сеть вам недоступна")
    slugs = [m["slug"] for m in avail[body.engine]]
    model = core.DEFAULT_MODEL if core.DEFAULT_MODEL in slugs else slugs[0]
    return core.create_chat(user["id"], body.engine, model, default_effort(avail, body.engine, model))


@app.get("/api/chats/{chat}")
def get_chat(chat: str, user=Depends(current_user)):
    return {"chat": own_chat(user, chat), "history": core.history(user["id"], chat),
            "running": (user["id"], chat) in jobs}


class ChatChange(BaseModel):
    title: str | None = None
    model: str | None = None
    effort: str | None = None


@app.patch("/api/chats/{chat}")
def change_chat(chat: str, body: ChatChange, user=Depends(current_user)):
    meta = own_chat(user, chat)
    idle(user, chat)
    avail = users.available(users.settings(user["id"]))
    if body.title is not None:
        meta["title"] = body.title.strip()[:200]
    if body.model is not None:
        if not any(m["slug"] == body.model for m in avail.get(meta["engine"], [])):
            raise HTTPException(403, "Эта модель вам недоступна")
        meta["model"] = body.model
        if body.effort is None and allowed_model(avail, meta["engine"], meta["model"], meta["effort"]):
            # новая модель не знает прежний уровень — берём допустимый
            meta["effort"] = default_effort(avail, meta["engine"], meta["model"])
    if body.effort is not None:
        reason = allowed_model(avail, meta["engine"], meta["model"], body.effort)
        if reason:
            raise HTTPException(403, reason)
        meta["effort"] = body.effort
    return core.save_chat(user["id"], meta)


@app.delete("/api/chats/{chat}")
def delete_chat(chat: str, user=Depends(current_user)):
    meta = own_chat(user, chat)
    idle(user, chat)
    core.delete_chat(user["id"], meta)
    return {}


async def push(key, job, event):
    async with job.cond:
        core.append_history(*key, event)
        job.cond.notify_all()


async def work(uid, meta, prompt, images, job, data):
    key, ok = (uid, meta["id"]), False
    engine, started = meta["engine"], time.time()
    points, tokens, limited = [], [0, 0], None
    try:
        async for event in core.run(uid, meta, prompt, data["access"][engine], images, on_proc=job.attach):
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


def human(n):
    for unit in ("Б", "КБ", "МБ", "ГБ"):
        if n < 1024 or unit == "ГБ":
            return f"{n:.0f} {unit}" if unit == "Б" else f"{n:.1f} {unit}"
        n /= 1024


@app.post("/api/chats/{chat}/messages")
async def send(chat: str, text: str = Form(""), files: list[UploadFile] = File([]),
               user=Depends(current_user)):
    meta = own_chat(user, chat)
    idle(user, chat)
    if not text.strip():
        raise HTTPException(400, "Напишите, что сделать")
    data = users.settings(user["id"])
    reason = allowed_model(users.available(data), meta["engine"], meta["model"], meta["effort"])
    if reason:
        raise HTTPException(403, reason)
    reason = usage.check(user["id"], meta["engine"], data)
    if reason:
        raise HTTPException(429, reason)
    quota = data["quota_mb"] * 2 ** 20
    if quota and files:
        used = usage.disk(user["id"])["total"]
        incoming = sum(f.size or 0 for f in files)
        if used + incoming > quota:
            raise HTTPException(507, f"Память закончилась: занято {human(used)} из {human(quota)}, "
                                     f"а файлы весят {human(incoming)}. Удалите ненужные беседы "
                                     "или попросите администратора увеличить лимит.")
    key = (user["id"], chat)
    jobs[key] = job = Job()  # занимаем беседу до первого await
    try:
        inbox = session_files.directory(Path(core.workspace(*key)) / "files" / "from_user")
        saved, images = [], []
        for upload in files:
            if upload.size is not None and upload.size > MAX_UPLOAD:
                raise HTTPException(413, f"{upload.filename}: больше {MAX_UPLOAD // 2 ** 20} МБ")
            target = session_files.vacant(inbox / session_files.safe_name(upload.filename))
            with open(target, "wb") as f:
                shutil.copyfileobj(upload.file, f)
            saved.append(target.name)
            if upload.content_type in IMAGE_TYPES and target.stat().st_size <= core.MAX_IMAGE:
                images.append((target.read_bytes(), upload.content_type))
    except BaseException:
        del jobs[key]
        raise
    notes = "\n".join(f"Вложение: files/from_user/{name}" for name in saved)
    prompt = "\n\n".join(p for p in (text, notes) if p)
    if not meta["title"]:
        meta["title"] = text.strip().replace("\n", " ")[:60]
    meta["updated"] = time.time()
    core.save_chat(user["id"], meta)
    await push(key, job, {"kind": "user", "text": text, "files": saved, "ts": time.time()})
    job.task = asyncio.create_task(work(user["id"], meta, prompt, images, job, data))
    return {}


@app.get("/api/chats/{chat}/events")
async def events(chat: str, start: int = 0, user=Depends(current_user)):
    """SSE: события истории начиная с номера start, пока идёт ответ."""
    own_chat(user, chat)
    key = (user["id"], chat)

    async def stream():
        n = start
        while True:
            job = jobs.get(key)
            # ponytail: перечитываем журнал целиком на каждое событие — для
            # личных бесед дёшево; хранить события в памяти, если станет тесно.
            past = core.history(*key)
            for event in past[n:]:
                yield "data: " + json.dumps(event, ensure_ascii=False) + "\n\n"
            n = len(past)
            if job is None:
                yield "event: end\ndata: {}\n\n"
                return
            async with job.cond:
                if len(core.history(*key)) == n and key in jobs:
                    try:
                        await asyncio.wait_for(job.cond.wait(), 15)
                    except TimeoutError:
                        yield ": ping\n\n"  # прокси не рвёт тихое соединение

    return StreamingResponse(stream(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.post("/api/chats/{chat}/stop")
def stop(chat: str, user=Depends(current_user)):
    own_chat(user, chat)
    job = jobs.get((user["id"], chat))
    if job:
        job.stopped = True
        if job.proc:
            core.stop(job.proc)
    return {}


def chat_files_root(user, chat):
    own_chat(user, chat)
    return Path(core.workspace(user["id"], chat)).resolve()


@app.get("/api/chats/{chat}/files")
def list_files(chat: str, user=Depends(current_user)):
    root = chat_files_root(user, chat)
    return [str(p.relative_to(root)) for folder in ("general", "from_user", "for_user")
            for p in session_files.queued(root, folder)]


@app.get("/api/chats/{chat}/file")
def get_file(chat: str, path: str, user=Depends(current_user)):
    root = chat_files_root(user, chat)
    target = (root / path).resolve()  # resolve раскрывает ../ и ссылки наружу
    if not target.is_relative_to(root / "files") or not target.is_file():
        raise HTTPException(404, "Нет такого файла")
    return FileResponse(target, filename=target.name)


# --- Пользователи ---

@app.get("/api/users")
def list_users(user=Depends(admin_user)):
    return users.listing()


@app.get("/api/users/{uid}")
def user_card(uid: int, user=Depends(admin_user)):
    """Карточка для окна «Пользователи»: всё про пользователя и полный каталог сетей."""
    found = next((u for u in users.listing() if u["id"] == uid), None)
    if not found:
        raise HTTPException(404, "Нет такого пользователя")
    data = users.settings(uid)
    chats = core.list_chats(uid)
    messages = sum(1 for c in chats for e in core.history(uid, c["id"]) if e.get("kind") == "user")
    catalog = core.models()
    return {"user": found, "settings": data,
            "stats": {"chats": len(chats), "messages": messages,
                      "last": max((c["updated"] for c in chats), default=0),
                      "running": sum(1 for owner, _ in jobs if owner == uid),
                      "disk": usage.disk(uid), "limits": usage.summary(uid, data)},
            "catalog": {e: catalog[e] for e in core.engines()},
            "subscription": usage.global_snapshot()}


@app.put("/api/users/{uid}/settings")
def user_settings(uid: int, body: dict, user=Depends(admin_user)):
    if not any(u["id"] == uid for u in users.listing()):
        raise HTTPException(404, "Нет такого пользователя")
    try:
        return users.save_settings(uid, body)
    except (ValueError, TypeError, AttributeError) as e:
        raise HTTPException(400, f"Неверные настройки: {e}")


class NewUser(BaseModel):
    name: str
    password: str
    admin: bool = False


@app.post("/api/users")
def add_user(body: NewUser, user=Depends(admin_user)):
    try:
        return {"id": users.add(body.name, body.password, body.admin)}
    except ValueError as e:
        raise HTTPException(400, str(e))


class Password(BaseModel):
    password: str


@app.post("/api/users/{uid}/password")
def change_password(uid: int, body: Password, user=Depends(current_user)):
    if uid != user["id"] and not user["is_admin"]:
        raise HTTPException(403, "Можно менять только свой пароль")
    try:
        users.set_password(uid, body.password)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {}


@app.delete("/api/users/{uid}")
def delete_user(uid: int, user=Depends(admin_user)):
    if uid == user["id"]:
        raise HTTPException(400, "Себя удалить нельзя")
    if any(owner == uid for owner, _ in jobs):
        raise HTTPException(409, "У пользователя идёт ответ")
    users.delete(uid)
    shutil.rmtree(core.user_dir(uid), ignore_errors=True)
    return {}


app.mount("/", StaticFiles(directory=os.path.join(core.HERE, "static"), html=True), name="static")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    uvicorn.run(app, host=os.environ.get("NEXUS_HOST", "127.0.0.1"),
                port=int(os.environ.get("NEXUS_PORT", "8080")),
                # За обратным прокси (KeenDNS, Caddy) доверяем его X-Forwarded-*.
                proxy_headers=True,
                forwarded_allow_ips=os.environ.get("NEXUS_TRUSTED_PROXY", "127.0.0.1"))
