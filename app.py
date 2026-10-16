"""Веб-сервер NexusAI: вход, беседы, стриминг ответов, файлы, пользователи.

Запуск: .venv/bin/python app.py (адрес — NEXUS_HOST/NEXUS_PORT).
"""
import asyncio
import contextlib
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
import jobs
import session_files
import tg
import usage
import users

COOKIE = "nexus"
MAX_UPLOAD = 50 * 1024 * 1024
LOCK_AFTER, LOCK_FOR = 5, 15 * 60

TG_TOKEN = os.environ.get("NEXUS_TG_TOKEN", "")


@contextlib.asynccontextmanager
async def lifespan(app):
    """Бот Telegram живёт в процессе сайта: общие беседы, ответы и уведомления."""
    task = None
    if TG_TOKEN:
        task = asyncio.create_task(tg.run(TG_TOKEN))
    yield
    if task:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task


app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)
# ponytail: счётчик неудачных входов в памяти — сбрасывается рестартом,
# для одного сервера этого достаточно.
failures = {}  # имя -> (неудач подряд, время последней)


@app.middleware("http")
async def security_headers(request: Request, call_next):
    response = await call_next(request)
    # Свой CSP ответа сильнее: файлы библиотеки отдаются с sandbox.
    response.headers.setdefault("Content-Security-Policy",
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


def in_tg(meta):
    if jobs.in_tg(meta):
        raise HTTPException(409, "Беседа идёт в Telegram — нажмите «Продолжить на сайте»")


def idle(user, chat):
    if (user["id"], chat) in jobs.jobs:
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
    return {**user, "engines": list(avail), "tg_bot": tg.bot_name,
            "disk": {"used": usage.disk(user["id"])["total"], "quota": data["quota_mb"] * 2 ** 20},
            "limits": {e: spent[e] for e in avail}}


# --- Telegram ---

@app.post("/api/me/telegram")
def tg_link(user=Depends(current_user)):
    """Ссылка на бота: /start с одноразовым кодом привязывает Telegram к этому пользователю."""
    if not tg.bot_name:
        raise HTTPException(503, "Бот Telegram не запущен")
    return {"url": tg.link(user["id"])}


class TgPrefs(BaseModel):
    show: bool | None = None
    notify: bool | None = None


@app.patch("/api/me/telegram")
def tg_prefs(body: TgPrefs, user=Depends(current_user)):
    users.tg_prefs(user["id"], body.show, body.notify)
    return {}


@app.delete("/api/me/telegram")
def tg_unlink(user=Depends(current_user)):
    users.unlink_tg(user["id"])
    return {}


# --- Беседы ---

@app.get("/api/models")
def models(user=Depends(current_user)):
    return users.available(users.settings(user["id"]))


@app.get("/api/chats")
def chats(user=Depends(current_user)):
    show = user["tg"] and user["tg"]["show"]
    return [{**m, "running": (user["id"], m["id"]) in jobs.jobs} for m in core.list_chats(user["id"])
            if show or not jobs.in_tg(m)]


class NewChat(BaseModel):
    engine: str


@app.post("/api/chats")
def new_chat(body: NewChat, user=Depends(current_user)):
    try:
        return jobs.new_chat(user["id"], body.engine)
    except jobs.Refused as e:
        raise HTTPException(e.status, str(e))


@app.get("/api/chats/{chat}")
def get_chat(chat: str, user=Depends(current_user)):
    return {"chat": own_chat(user, chat), "history": core.history(user["id"], chat),
            "running": (user["id"], chat) in jobs.jobs}


class ChatChange(BaseModel):
    title: str | None = None
    model: str | None = None
    effort: str | None = None


@app.patch("/api/chats/{chat}")
def change_chat(chat: str, body: ChatChange, user=Depends(current_user)):
    meta = own_chat(user, chat)
    idle(user, chat)
    in_tg(meta)
    avail = users.available(users.settings(user["id"]))
    if body.title is not None:
        meta["title"] = body.title.strip()[:200]
    if body.model is not None:
        if not any(m["slug"] == body.model for m in avail.get(meta["engine"], [])):
            raise HTTPException(403, "Эта модель вам недоступна")
        meta["model"] = body.model
        if body.effort is None and jobs.allowed_model(avail, meta["engine"], meta["model"], meta["effort"]):
            # новая модель не знает прежний уровень — берём допустимый
            meta["effort"] = jobs.default_effort(avail, meta["engine"], meta["model"])
    if body.effort is not None:
        reason = jobs.allowed_model(avail, meta["engine"], meta["model"], body.effort)
        if reason:
            raise HTTPException(403, reason)
        meta["effort"] = body.effort
    return core.save_chat(user["id"], meta)


@app.post("/api/chats/{chat}/move")
def move_chat(chat: str, user=Depends(current_user)):
    """Беседа из Telegram переезжает на сайт: бот в ней больше не отвечает."""
    meta = own_chat(user, chat)
    idle(user, chat)
    if meta.get("tg"):
        meta["tg"]["moved"] = True
        core.save_chat(user["id"], meta)
    return meta


@app.delete("/api/chats/{chat}")
def delete_chat(chat: str, files: bool = False, user=Depends(current_user)):
    """files — удалить и файлы беседы (из беседы и из библиотеки); иначе они остаются в библиотеке."""
    meta = own_chat(user, chat)
    idle(user, chat)
    core.delete_chat(user["id"], meta, files)
    return {}


@app.post("/api/chats/{chat}/messages")
async def send(chat: str, text: str = Form(""), files: list[UploadFile] = File([]),
               user=Depends(current_user)):
    meta = own_chat(user, chat)
    idle(user, chat)
    in_tg(meta)
    if not text.strip():
        raise HTTPException(400, "Напишите, что сделать")
    try:
        data = jobs.check(user["id"], meta, sum(f.size or 0 for f in files))
    except jobs.Refused as e:
        raise HTTPException(e.status, str(e))
    inbox = session_files.directory(Path(core.workspace(user["id"], chat)) / "files" / "from_user")
    saved, images = [], []
    for upload in files:
        if upload.size is not None and upload.size > MAX_UPLOAD:
            raise HTTPException(413, f"{upload.filename}: больше {MAX_UPLOAD // 2 ** 20} МБ")
        target = session_files.vacant(inbox / session_files.safe_name(upload.filename))
        with open(target, "wb") as f:
            shutil.copyfileobj(upload.file, f)
        saved.append(target.name)
        if upload.content_type in jobs.IMAGE_TYPES and target.stat().st_size <= core.MAX_IMAGE:
            images.append((target.read_bytes(), upload.content_type))
    # От idle() до jobs.start нет ни одного await: второй запрос не займёт беседу между ними.
    await jobs.start(user["id"], meta, text, data, saved, images)
    return {}


@app.get("/api/chats/{chat}/events")
async def events(chat: str, start: int = 0, user=Depends(current_user)):
    """SSE: события истории начиная с номера start, пока идёт ответ."""
    own_chat(user, chat)
    key = (user["id"], chat)

    async def stream():
        jobs.watchers[key] += 1  # открытая вкладка: уведомление в Telegram не нужно
        try:
            async for chunk in follow():
                yield chunk
        finally:
            jobs.watchers[key] -= 1

    async def follow():
        n = start
        while True:
            job = jobs.jobs.get(key)
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
                if len(core.history(*key)) == n and key in jobs.jobs:
                    try:
                        await asyncio.wait_for(job.cond.wait(), 15)
                    except TimeoutError:
                        yield ": ping\n\n"  # прокси не рвёт тихое соединение

    return StreamingResponse(stream(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.post("/api/chats/{chat}/stop")
def stop(chat: str, user=Depends(current_user)):
    own_chat(user, chat)
    job = jobs.jobs.get((user["id"], chat))
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
    linked = session_files.shelved(core.library(user["id"]))
    return [{"path": str(p.relative_to(root)), "size": p.lstat().st_size}
            for folder in ("general", "from_user", "for_user")
            for p in session_files.queued(root, folder, linked if folder == "general" else frozenset())]


@app.get("/api/chats/{chat}/file")
def get_file(chat: str, path: str, user=Depends(current_user)):
    root = chat_files_root(user, chat)
    target = (root / path).resolve()  # resolve раскрывает ../ и ссылки наружу
    if not target.is_relative_to(root / "files") or not target.is_file():
        raise HTTPException(404, "Нет такого файла")
    info = target.lstat()
    # Вторая жёсткая ссылка допустима только в библиотеке: иначе это может быть чужой файл.
    if info.st_nlink > 1 and session_files.inode(info) not in session_files.shelved(core.library(user["id"])):
        raise HTTPException(404, "Нет такого файла")
    return FileResponse(target, filename=target.name)


# --- Библиотека ---

@app.get("/api/library")
def library(user=Depends(current_user)):
    """Все файлы пользователя, свежие сверху, и беседы, в которых они лежат."""
    root = Path(core.library(user["id"])).resolve()
    where = {}
    for meta in core.list_chats(user["id"]):
        for path in core.links(user["id"], meta["id"]).values():
            where.setdefault(path, []).append({"id": meta["id"], "title": meta["title"]})
    items = []
    for path in session_files.shelved(root).values():  # только обычные файлы, без ссылок
        if session_files.TEMP.fullmatch(path.name):
            continue  # временный файл замены версии
        info = path.lstat()
        rel = str(path.relative_to(root))
        items.append({"path": rel, "size": info.st_size, "mtime": info.st_mtime, "chats": where.get(rel, [])})
    return sorted(items, key=lambda i: i["mtime"], reverse=True)


@app.get("/api/library/file")
def library_file(path: str, download: bool = False, user=Depends(current_user)):
    root = Path(core.library(user["id"])).resolve()
    target = (root / path).resolve()  # resolve раскрывает ../ и ссылки наружу
    if not target.is_relative_to(root) or not target.is_file():
        raise HTTPException(404, "Нет такого файла")
    if download:
        return FileResponse(target, filename=target.name)
    # Просмотр в браузере: файлы делают модели и приходят из сети. sandbox не даёт
    # HTML и SVG выполнить скрипт от имени сайта.
    return FileResponse(target, filename=target.name, content_disposition_type="inline",
                        headers={"Content-Security-Policy": "sandbox"})


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
                      "running": sum(1 for owner, _ in jobs.jobs if owner == uid),
                      "disk": usage.disk(uid), "limits": usage.summary(uid, data)},
            "catalog": {e: catalog[e] for e in core.engines()},
            "skills": core.skills(),
            "subscription": usage.global_snapshot()}


@app.put("/api/users/{uid}/settings")
def user_settings(uid: int, body: dict, user=Depends(admin_user)):
    if not any(u["id"] == uid for u in users.listing()):
        raise HTTPException(404, "Нет такого пользователя")
    try:
        return users.save_settings(uid, body)
    except (ValueError, TypeError, AttributeError) as e:
        raise HTTPException(400, f"Неверные настройки: {e}")


@app.get("/api/skills")
def site_skills(user=Depends(admin_user)):
    """Скиллы аккаунта claude.ai и какие из них выложены на сайт."""
    return {"account": core.account_skills(), "site": core.skills()}


@app.put("/api/skills")
def publish_skills(body: list[str], user=Depends(admin_user)):
    return {"account": core.account_skills(), "site": core.publish_skills(set(body))}


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
    if any(owner == uid for owner, _ in jobs.jobs):
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
