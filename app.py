"""Веб-сервер NexusAI: вход, беседы, стриминг ответов, файлы, пользователи.

Запуск: .venv/bin/python app.py (адрес — NEXUS_HOST/NEXUS_PORT).
"""
import asyncio
import contextlib
import json
import logging
import os
import re
import shutil
import time
import uuid
from pathlib import Path
from urllib.parse import quote

import uvicorn
from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, Response, UploadFile
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

import core
import jobs
import push
import session_files
import settings
import stt
import tg
import thumbs
import usage
import users

log = logging.getLogger("nexus.web")

COOKIE = "nexus"
MAX_UPLOAD = 50 * 1024 * 1024
MAX_VOICE = 20 * 1024 * 1024
MAX_VOICE_SECONDS = 10 * 60
VOICE_TYPES = {"audio/webm": "webm", "audio/ogg": "ogg", "audio/mp4": "m4a", "audio/aac": "aac",
               "audio/mpeg": "mp3", "audio/wav": "wav", "audio/x-wav": "wav"}
VOICE_ID = re.compile(r"[0-9a-f]{32}")

bot_task = None  # опрос Telegram; новый токен в «Общих настройках» перезапускает его


async def stop_task(task):
    if task:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task


async def restart_bot():
    global bot_task
    await stop_task(bot_task)
    tg.bot_name = None
    token = settings.get(settings.TG_TOKEN.name)
    bot_task = asyncio.create_task(tg.run(token)) if token else None


@contextlib.asynccontextmanager
async def lifespan(app):
    """Бот Telegram живёт в процессе сайта: общие беседы, ответы и уведомления."""
    # Новый процесс: флаг обновления от прежнего больше не нужен, счётчики — с нуля.
    with contextlib.suppress(FileNotFoundError):
        os.remove(jobs.DRAIN)
    jobs.save_busy()
    poll = asyncio.create_task(usage.poll())
    await restart_bot()
    yield
    await stop_task(poll)
    await stop_task(bot_task)


# Смена токена приходит из обработчика запроса — он в том же цикле событий.
settings.watch(settings.TG_TOKEN.name, lambda _: asyncio.get_running_loop().create_task(restart_bot()))


app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)


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
    if jobs.transcribing[(user["id"], chat)]:
        raise HTTPException(409, "Дождитесь расшифровки голосового")


# --- Вход ---

class Credentials(BaseModel):
    name: str
    password: str


@app.post("/api/login")
def login(body: Credentials, request: Request, response: Response):
    # Обычная def: FastAPI выполнит её в пуле потоков, scrypt и sleep не держат цикл.
    name = body.name.strip()
    if users.locked(name):
        raise HTTPException(429, "Слишком много попыток, подождите 15 минут")
    user = users.check(name, body.password)
    if not user:
        users.failed(name)
        time.sleep(1)
        raise HTTPException(401, "Неверное имя или пароль")
    users.succeeded(name)
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


# --- Уведомления Web Push («Ответ готов» на этом устройстве) ---

@app.get("/api/push/key")
def push_key(user=Depends(current_user)):
    return {"key": push.public_key()}


class PushSubscription(BaseModel):
    subscription: dict
    label: str = ""


@app.post("/api/push")
def push_subscribe(body: PushSubscription, user=Depends(current_user)):
    try:
        push.add(user["id"], body.subscription, body.label)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {}


class PushEndpoint(BaseModel):
    endpoint: str


@app.post("/api/push/remove")
def push_unsubscribe(body: PushEndpoint, user=Depends(current_user)):
    push.remove(user["id"], body.endpoint)
    return {}


@app.post("/api/push/test")
async def push_test(user=Depends(current_user)):
    if not await push.notify(user["id"], "NexusAI", "Уведомления включены"):
        raise HTTPException(502, "Не удалось отправить: проверьте, что уведомления включены")
    return {}


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
            "running": (user["id"], chat) in jobs.jobs,
            "transcribing": jobs.transcribing[(user["id"], chat)] > 0}


class Retry(BaseModel):
    text: str | None = None  # изменённый запрос; нет — тот же


@app.post("/api/chats/{chat}/retry")
async def retry(chat: str, body: Retry, user=Depends(current_user)):
    """Повторить или изменить последний запрос: прежний ответ заменяется новым."""
    meta = own_chat(user, chat)
    idle(user, chat)
    in_tg(meta)
    try:
        await jobs.retry(user["id"], meta, body.text)
    except jobs.Refused as e:
        raise HTTPException(e.status, str(e))
    return {}


@app.get("/api/chats/{chat}/export")
def export(chat: str, user=Depends(current_user)):
    """Беседа одним Markdown-файлом."""
    meta = own_chat(user, chat)
    body = core.export_markdown(meta, core.history(user["id"], chat))
    name = re.sub(r"\s+", " ", re.sub(r'[\\/:*?"<>|\x00-\x1f]+', " ", meta["title"] or "Беседа")).strip()[:80] or "Беседа"
    return Response(body, media_type="text/markdown; charset=utf-8", headers={
        "Content-Disposition": f"attachment; filename=\"chat.md\"; filename*=UTF-8''{quote(name + '.md')}"})


@app.get("/api/search")
def search(q: str = "", user=Depends(current_user)):
    """Поиск по названиям бесед и тексту сообщений."""
    q = q.strip()
    return core.search(user["id"], q) if len(q) >= 2 else []


@app.get("/api/chats/{chat}/size")
def chat_size(chat: str, user=Depends(current_user)):
    """Размер беседы; limit — порог «Сжать беседу» пользователя (0 — не предлагать)."""
    meta = own_chat(user, chat)
    size = core.chat_size(user["id"], meta)
    return {**size, "limit": users.settings(user["id"])["compact_mb"] * 2 ** 20,
            "compactable": size["context"] > 0}


@app.post("/api/chats/{chat}/compact")
async def compact(chat: str, user=Depends(current_user)):
    """«Сжать беседу»: claude заменяет старую часть сессии кратким содержанием (/compact).
    Идёт как обычный ответ — с лимитами подписки и событиями в ленте."""
    meta = own_chat(user, chat)
    idle(user, chat)
    in_tg(meta)
    try:
        await jobs.compact(user["id"], meta)
    except jobs.Refused as e:
        raise HTTPException(e.status, str(e))
    return {}


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
               user=Depends(current_user), voice: str = Form("")):
    """voice — голосовое, расшифровку которого пользователь подтвердил: текст уходит от него."""
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
    if isinstance(voice, str) and VOICE_ID.fullmatch(voice):  # прямой вызов: там объект Form
        # До jobs.start без await: ответ «да» и реплика встают в журнал подряд.
        core.append_history(user["id"], chat, {"kind": "voice_answer", "voice": voice, "ok": True})
    # От idle() до jobs.start нет ни одного await: второй запрос не займёт беседу между ними.
    await jobs.start(user["id"], meta, text, data, saved, images)
    return {}


def voice_peaks(raw):
    """Уровни громкости для дорожки: до 64 чисел 0..1, посчитаны браузером при записи."""
    try:
        peaks = json.loads(raw or "[]")
        return [round(min(max(float(p), 0.0), 1.0), 3) for p in peaks[:64]]
    except (ValueError, TypeError):
        return []


@app.post("/api/chats/{chat}/voice")
async def voice(chat: str, audio: UploadFile = File(...), duration: float = Form(...), peaks: str = Form(""),
                user=Depends(current_user)):
    """Голосовое: сохраняется, встаёт в журнал и сразу расшифровывается.
    Возвращает новые события журнала — голосовое и расшифровку или ошибку."""
    meta = own_chat(user, chat)
    idle(user, chat)
    in_tg(meta)
    if jobs.draining():
        raise HTTPException(503, jobs.UPDATING)
    if not 0 < duration <= MAX_VOICE_SECONDS:
        raise HTTPException(400, f"Голосовое должно быть до {MAX_VOICE_SECONDS // 60} минут")
    data = await upload_bytes(audio, MAX_VOICE)
    ext = VOICE_TYPES.get((audio.content_type or "").split(";")[0].strip(), "webm")
    vid = uuid.uuid4().hex
    folder = session_files.directory(core.voice_dir(user["id"], chat))
    (Path(folder) / f"{vid}.{ext}").write_bytes(data)
    key = (user["id"], chat)
    added = [{"kind": "voice", "id": vid, "ext": ext, "duration": round(duration, 2),
              "peaks": voice_peaks(peaks), "ts": time.time()}]
    core.append_history(*key, added[0])
    jobs.transcribing[key] += 1
    jobs.save_busy()
    try:
        text = await stt.transcribe(data)
        added.append({"kind": "transcript", "voice": vid, "text": text} if text else
                     {"kind": "warning", "text": "В голосовом не удалось разобрать речь"})
    except Exception as e:  # битый файл, нет модели, не хватило памяти — беседа не должна встать
        log.exception("расшифровка голосового")
        added.append({"kind": "warning", "text": f"Не удалось расшифровать голосовое: {e}"})
    finally:
        jobs.transcribing[key] -= 1
        if not jobs.transcribing[key]:
            del jobs.transcribing[key]
        jobs.save_busy()
    core.append_history(*key, added[1])
    return {"events": added}


async def upload_bytes(upload, limit):
    if upload.size is not None and upload.size > limit:
        raise HTTPException(413, f"Больше {limit // 2 ** 20} МБ")
    data = await upload.read(limit + 1)
    if len(data) > limit:
        raise HTTPException(413, f"Больше {limit // 2 ** 20} МБ")
    if not data:
        raise HTTPException(400, "Пустая запись")
    return data


@app.get("/api/chats/{chat}/voice/{vid}")
def voice_file(chat: str, vid: str, user=Depends(current_user)):
    own_chat(user, chat)
    if not VOICE_ID.fullmatch(vid):
        raise HTTPException(404, "Нет такого голосового")
    for ext in set(VOICE_TYPES.values()):
        path = Path(core.voice_dir(user["id"], chat)) / f"{vid}.{ext}"
        if path.is_file():
            # FileResponse отвечает и на Range: без этого Safari не играет звук.
            media = next(t for t, e in VOICE_TYPES.items() if e == ext)
            return FileResponse(path, media_type=media)
    raise HTTPException(404, "Нет такого голосового")


@app.post("/api/chats/{chat}/voice/{vid}/decline")
def voice_decline(chat: str, vid: str, user=Depends(current_user)):
    """«Нет» под расшифровкой: кнопки убираются, текст остаётся в ленте."""
    own_chat(user, chat)
    idle(user, chat)
    if not VOICE_ID.fullmatch(vid):
        raise HTTPException(404, "Нет такого голосового")
    core.append_history(user["id"], chat, {"kind": "voice_answer", "voice": vid, "ok": False})
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
    files = []
    for folder in ("general", "from_user", "for_user"):
        for p in session_files.queued(root, folder, linked if folder == "general" else frozenset()):
            info = p.lstat()
            files.append({"path": str(p.relative_to(root)), "size": info.st_size, "mtime": info.st_mtime})
    return sorted(files, key=lambda f: f["mtime"], reverse=True)  # свежие сверху


def chat_file_target(user, chat, path):
    root = chat_files_root(user, chat)
    target = (root / path).resolve()  # resolve раскрывает ../ и ссылки наружу
    if not target.is_relative_to(root / "files") or not target.is_file():
        raise HTTPException(404, "Нет такого файла")
    info = target.lstat()
    # Вторая жёсткая ссылка допустима только в библиотеке: иначе это может быть чужой файл.
    if info.st_nlink > 1 and session_files.inode(info) not in session_files.shelved(core.library(user["id"])):
        raise HTTPException(404, "Нет такого файла")
    return target


@app.get("/api/chats/{chat}/file")
def get_file(chat: str, path: str, user=Depends(current_user)):
    target = chat_file_target(user, chat, path)
    return FileResponse(target, filename=target.name)


def thumb_response(user, target):
    """Миниатюра фото вместо оригинала. В адресе — время изменения файла (v=), поэтому
    браузер держит её в кэше, пока файл не заменят. 415 — не картинка: плитка
    покажет значок."""
    try:
        path = thumbs.thumbnail(Path(core.user_dir(user["id"])) / "thumbs", target)
    except thumbs.Unsupported:
        raise HTTPException(415, "Нет миниатюры")
    return FileResponse(path, media_type=thumbs.MEDIA,
                        headers={"Cache-Control": "private, max-age=31536000, immutable"})


@app.get("/api/chats/{chat}/thumb")
def chat_thumb(chat: str, path: str, user=Depends(current_user)):
    return thumb_response(user, chat_file_target(user, chat, path))


class FilePaths(BaseModel):
    paths: list[str]


@app.post("/api/chats/{chat}/files/delete")
def delete_files(chat: str, body: FilePaths, user=Depends(current_user)):
    """Несколько файлов сразу (выделение в «Файлах беседы»); из библиотеки не удаляются."""
    own_chat(user, chat)
    idle(user, chat)
    paths = list(dict.fromkeys(body.paths))
    if not paths or len(paths) > 5000:
        raise HTTPException(400, "Не выбраны файлы")
    return {"deleted": core.delete_chat_files(user["id"], chat, paths)}


@app.delete("/api/chats/{chat}/file")
def delete_file(chat: str, path: str, user=Depends(current_user)):
    """Файл уходит из беседы; если он уже в библиотеке, там остаётся."""
    own_chat(user, chat)
    idle(user, chat)
    if not core.delete_chat_file(user["id"], chat, path):
        raise HTTPException(404, "Нет такого файла")
    return {}


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


def library_target(user, path):
    root = Path(core.library(user["id"])).resolve()
    target = (root / path).resolve()  # resolve раскрывает ../ и ссылки наружу
    if not target.is_relative_to(root) or not target.is_file():
        raise HTTPException(404, "Нет такого файла")
    return target


@app.get("/api/library/thumb")
def library_thumb(path: str, user=Depends(current_user)):
    return thumb_response(user, library_target(user, path))


@app.get("/api/library/file")
def library_file(path: str, download: bool = False, user=Depends(current_user)):
    target = library_target(user, path)
    if download:
        return FileResponse(target, filename=target.name)
    # Просмотр в браузере: файлы делают модели и приходят из сети. sandbox не даёт
    # HTML и SVG выполнить скрипт от имени сайта.
    return FileResponse(target, filename=target.name, content_disposition_type="inline",
                        headers={"Content-Security-Policy": "sandbox"})


@app.post("/api/library/delete")
def delete_library_files(body: FilePaths, user=Depends(current_user)):
    """Несколько файлов сразу (выделение в библиотеке). Если какая-то из бесед с этими
    файлами сейчас отвечает, не удаляется ничего."""
    paths = list(dict.fromkeys(body.paths))
    if not paths or len(paths) > 5000:
        raise HTTPException(400, "Не выбраны файлы")
    wanted = frozenset(paths)
    for meta in core.list_chats(user["id"]):
        if wanted & set(core.links(user["id"], meta["id"]).values()):
            idle(user, meta["id"])
    return {"deleted": core.delete_library_files(user["id"], paths)}


@app.delete("/api/library/file")
def delete_library_file(path: str, user=Depends(current_user)):
    """Файл удаляется совсем: из библиотеки и из всех бесед, где он лежит."""
    for meta in core.chats_with(user["id"], path):
        idle(user, meta["id"])
    if not core.delete_library_file(user["id"], path):
        raise HTTPException(404, "Нет такого файла")
    return {}


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
    messages = sum(1 for c in chats for e in core.visible(core.history(uid, c["id"])) if e.get("kind") == "user")
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


# --- Общие настройки сервиса (data/settings.json) ---

@app.get("/api/settings")
def general_settings(user=Depends(admin_user)):
    return settings.listing()


@app.put("/api/settings")
async def save_settings(body: dict, user=Depends(admin_user)):
    """Изменённые параметры {имя: значение}: сначала проверяем все, потом сохраняем."""
    unknown = [name for name in body if name not in settings.BY_NAME]
    if unknown:
        raise HTTPException(400, "Нет такого параметра: " + ", ".join(unknown))
    try:
        values = {name: settings.parameter(name).convert(value) for name, value in body.items()}
    except ValueError as e:
        raise HTTPException(400, str(e))
    for name, value in values.items():
        settings.set(name, value)
    return settings.listing()


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
