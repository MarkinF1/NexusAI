"""Telegram-бот сайта: те же пользователи, права, лимиты и беседы, что и на сайте.

Пишут боту только пользователи сайта, привязавшие Telegram в «Настройках». Беседа
Telegram — обычная беседа пользователя с полем tg; в группе с темами одна тема —
одна беседа владельца группы (кто сделал /start). Запускается из app.py, если
задан NEXUS_TG_TOKEN.
"""
import asyncio
import contextlib
import io
import json
import logging
import os
import secrets
import time
import uuid
from collections import defaultdict
from contextlib import nullcontext
from pathlib import Path

from aiogram import BaseMiddleware, Bot, Dispatcher, F
from aiogram.exceptions import (TelegramBadRequest, TelegramForbiddenError, TelegramNetworkError,
                                TelegramUnauthorizedError)
from aiogram.filters import Command
from aiogram.types import (BotCommand, CallbackQuery, FSInputFile, InlineKeyboardButton,
                           InlineKeyboardMarkup, Message)
from aiogram.utils.chat_action import ChatActionSender

import core
import jobs
import stt
import session_files
import users

LIMIT = 4000  # лимит Telegram 4096, с запасом
PAGE = 5  # бесед на страницу списка
MAX_DOWNLOAD = 20 * 1024 * 1024  # больше Bot API скачать не даёт
MAX_UPLOAD = 50 * 1024 * 1024
ALBUM_WAIT = 1.5  # альбом приходит пачкой сообщений, ждём остальные
CODE_TTL = 10 * 60
MARKS = {"claude": "🟠", "codex": "🟢"}
LINK_HINT = "Привяжите Telegram на сайте: «Настройки» → «Привязать Telegram»."
MOVED = "Эта беседа перенесена на сайт: продолжайте её там. /new — начать новую здесь."
HELP = ("Пишите текстом, присылайте фото, документы и голосовые. Права, лимиты и файлы — те же, "
        "что на сайте.\n\n"
        "/info — текущая модель и беседа\n/model — модель и движок: Claude или Codex\n"
        "/effort — уровень размышления\n/new — новая беседа (в группе: /new Название — новая тема)\n"
        "/sessions — прошлые беседы или темы группы\n/delete — удалить беседу")

log = logging.getLogger("nexus.tg")
dp = Dispatcher()
bot = None  # запущенный бот: через него идут уведомления
bot_name = None  # username бота: из него собирается ссылка привязки
codes = {}  # код привязки -> (uid, срок); ponytail: в памяти, рестарт сбрасывает коды
# Сообщения одной беседы идут по очереди: одна сессия CLI — один ответ за раз.
locks = defaultdict(asyncio.Lock)
# Альбом Telegram шлёт отдельными сообщениями с общим media_group_id — копим их
# и отдаём модели одним запросом, когда поток сообщений прекратился.
albums = defaultdict(list)
album_deadline = {}


# --- Привязка и уведомления ---

def link(uid):
    """Ссылка t.me, по которой бот привяжет Telegram к пользователю сайта."""
    now = time.time()
    for code in [c for c, (_, expires) in codes.items() if expires < now]:
        del codes[code]
    code = secrets.token_urlsafe(16)
    codes[code] = (uid, now + CODE_TTL)
    return f"https://t.me/{bot_name}?start={code}"


async def notify(uid, text):
    user = users.get(uid)
    if not bot or not user or not user["tg"] or not user["tg"]["notify"]:
        return
    try:
        await bot.send_message(user["tg"]["id"], text)
    except Exception:
        log.exception("notify failed")


# --- Доступ ---

def is_group(message):
    return message.chat.type != "private"


class Access(BaseMiddleware):
    """who — пользователь сайта, который пишет; owner — чьи беседы, права и лимит."""

    async def __call__(self, handler, event, data):
        callback = isinstance(event, CallbackQuery)
        message = event.message if callback else event
        actor = event.from_user
        if not message or not actor or actor.is_bot:
            return
        who = users.by_tg(actor.id)
        command = "" if callback else (message.text or "").split(maxsplit=1)[0:1]
        command = command[0].split("@")[0] if command else ""
        if not is_group(message):
            if not who and command != "/start":
                await (event.answer(LINK_HINT, show_alert=True) if callback else message.answer(LINK_HINT))
                return
            data.update(who=who, owner=who and who["id"])
            return await handler(event, data)
        if not who:
            return  # чужие в группе: молчим
        if not message.chat.is_forum:
            if command:
                await message.answer("Включите в группе темы: одна тема — одна беседа.")
            return
        owner = users.group_owner(message.chat.id)
        if owner is None and command != "/start":
            if command:
                await message.answer("Для подключения группы отправьте /start.")
            return
        data.update(who=who, owner=owner)
        return await handler(event, data)


dp.message.outer_middleware(Access())
dp.callback_query.outer_middleware(Access())


# --- Беседы ---

def thread(message):
    """Тема группы; у General темы нет id, ей отвечает 1. В личке — None."""
    return (message.message_thread_id or 1) if is_group(message) else None


def find(uid, chat_id, thread_id):
    # ponytail: перебор бесед владельца на каждое сообщение — индекс, если бесед станут тысячи.
    return next((m for m in core.list_chats(uid) if (m.get("tg") or {}).get("chat") == chat_id
                 and m["tg"].get("thread") == thread_id), None)


def pointer(uid):
    return os.path.join(core.user_dir(uid), "tg.json")


def set_current(uid, chat):
    with open(pointer(uid), "w") as f:
        json.dump({"current": chat}, f)


def default_engine(uid):
    avail = users.available(users.settings(uid))
    if not avail:
        raise jobs.Refused(403, "Вам не доступна ни одна нейросеть")
    return "claude" if "claude" in avail else next(iter(avail))


def create(uid, chat_id, thread_id, engine=None, by=None, title=""):
    """Беседа Telegram; беседа лички становится текущей."""
    meta = jobs.new_chat(uid, engine or default_engine(uid),
                         tg={"chat": chat_id, "thread": thread_id, "by": by, "moved": False})
    if title:
        meta = core.save_chat(uid, {**meta, "title": title})
    if thread_id is None:
        set_current(uid, meta["id"])
    return meta


def current(uid, message, make=True):
    """Беседа этого чата или темы; make — создать, если её нет."""
    if is_group(message):
        meta = find(uid, message.chat.id, thread(message))
    else:
        try:
            with open(pointer(uid)) as f:
                meta = core.load_chat(uid, json.load(f)["current"])
        except (OSError, ValueError, KeyError):
            meta = None
    if not meta and make:
        meta = create(uid, message.chat.id, thread(message), by=message.from_user.id if is_group(message) else None)
    return meta


def manager(who, owner, meta, actor_id):
    """Настройки темы меняют её создатель, владелец группы и администратор сайта."""
    return (who["id"] == owner or who["is_admin"]
            or not meta or not (meta.get("tg") or {}).get("by") or meta["tg"]["by"] == actor_id)


def busy(uid, meta):
    return (uid, meta["id"]) in jobs.jobs


def about(meta):
    return f"{MARKS[meta['engine']]} {meta['model']} · размышление: {meta['effort'] or 'по умолчанию'}"


def model_kb(uid, meta):
    rows = [[InlineKeyboardButton(text=("✓ " if meta and (e, m["slug"]) == (meta["engine"], meta["model"]) else "")
                                  + f"{MARKS[e]} {m['name']}", callback_data=f"model:{e}:{m['slug']}")]
            for e, listed in users.available(users.settings(uid)).items() for m in listed]
    return InlineKeyboardMarkup(inline_keyboard=rows)


def model_text(uid, meta):
    lines = [f"Модель: {about(meta)}", "", "Для чего использовать:"]
    for engine, listed in users.available(users.settings(uid)).items():
        lines += [f"{MARKS[engine]} {m['name']} — {m['about'].rstrip('.')}" for m in listed]
    return "\n".join(lines)


def effort_kb(uid, meta):
    avail = users.available(users.settings(uid))
    found = next((m for m in avail.get(meta["engine"], []) if m["slug"] == meta["model"]), None)
    levels = (found["efforts"] if found else []) + ([""] if found and found["auto"] else [])
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=("✓ " if level == meta["effort"] else "") + (level or "по умолчанию"),
                              callback_data=f"effort:{level}")] for level in levels])


def tg_chats(uid, message):
    """Непереехавшие беседы этого чата Telegram, свежие первыми."""
    return [m for m in core.list_chats(uid) if (m.get("tg") or {}).get("chat") == message.chat.id
            and not m["tg"].get("moved")]


def sessions_page(uid, message, page, prefix="sess"):
    items = tg_chats(uid, message)
    pages = max(1, -(-len(items) // PAGE))
    page = max(0, min(page, pages - 1))
    active = current(uid, message, make=False)
    lines, buttons = [], []
    for n, meta in enumerate(items[page * PAGE:(page + 1) * PAGE], page * PAGE + 1):
        mark = "✓ " if active and meta["id"] == active["id"] else ""
        lines.append(f"{n}. {mark}{MARKS[meta['engine']]} {meta['title'] or 'новая, пока пустая'}")
        buttons.append(InlineKeyboardButton(text=str(n), callback_data=f"{prefix}:{meta['id']}"))
    rows = [buttons]
    if pages > 1:
        rows.append([InlineKeyboardButton(text="◀", callback_data=f"{prefix}page:{page - 1}"),
                     InlineKeyboardButton(text=f"{page + 1}/{pages}", callback_data=f"{prefix}page:{page}"),
                     InlineKeyboardButton(text="▶", callback_data=f"{prefix}page:{page + 1}")])
    title = "Беседы (✓ — текущая):" if prefix == "sess" else "Какую беседу удалить?"
    return (title + "\n" + "\n".join(lines)) if items else "Бесед пока нет.", InlineKeyboardMarkup(inline_keyboard=rows)


def topic_link(chat_id, thread_id):
    # Внутренние ссылки Telegram работают и для закрытых групп у их участников.
    return f"https://t.me/c/{str(chat_id).removeprefix('-100')}/{thread_id}"


def split_message(text, limit=LIMIT):
    return [text[i:i + limit] for i in range(0, len(text), limit)]


async def edit(call, text, kb=None):
    try:
        await call.message.edit_text(text, reply_markup=kb)
    except TelegramBadRequest as e:
        if "message is not modified" not in str(e).lower():
            raise


# --- Команды ---

@dp.message(Command("start"))
async def on_start(message: Message, who, owner):
    code = (message.text or "").split(maxsplit=1)[1:]
    if not is_group(message) and code:
        uid, expires = codes.pop(code[0], (None, 0))
        if not uid or expires < time.time():
            await message.answer("Ссылка устарела. Нажмите «Привязать Telegram» на сайте ещё раз.")
            return
        try:
            users.link_tg(uid, message.from_user.id, message.from_user.username or message.from_user.full_name)
        except ValueError as e:
            await message.answer(str(e))
            return
        await message.answer(f"Telegram привязан к пользователю {users.get(uid)['name']}.\n\n" + HELP)
        return
    if not who:
        await message.answer(LINK_HINT)
        return
    if is_group(message) and owner is None:
        users.set_group_owner(message.chat.id, who["id"])
        await message.answer(f"Группа подключена к пользователю {who['name']}: темы — его беседы, его "
                             "права и лимит. Писать могут все участники с привязанным Telegram.\n\n" + HELP)
        return
    await message.answer(HELP)


@dp.message(Command("info"))
async def on_info(message: Message, owner):
    meta = current(owner, message, make=False)
    if not meta:
        await message.answer("Беседы ещё нет — просто напишите.")
        return
    if not jobs.in_tg(meta):
        await message.answer(MOVED)
        return
    count = sum(1 for e in core.history(owner, meta["id"]) if e.get("kind") == "user")
    await message.answer(f"Модель: {about(meta)}\nБеседа: {meta['title'] or 'новая'}\n"
                         f"Ваших сообщений: {count}\n"
                         "Модель: /model · размышление: /effort\nБеседы: /sessions · новая: /new · удалить: /delete")


@dp.message(Command("new"))
async def on_new(message: Message, who, owner):
    if is_group(message):
        name = ((message.text or "").split(maxsplit=1)[1:] or ["Новая беседа"])[0].strip()[:128]
        try:
            created = await message.bot.create_forum_topic(message.chat.id, name)
        except (TelegramBadRequest, TelegramForbiddenError):
            await message.answer("Не удалось создать тему. Дайте боту право «Управление темами» "
                                 "или создайте тему вручную.")
            return
        # Событие создания темы придёт от бота, и Access его пропустит: беседу заводим сами.
        try:
            meta = create(owner, message.chat.id, created.message_thread_id, by=message.from_user.id,
                          title=created.name)
        except jobs.Refused as e:
            await message.answer(str(e))
            return
        await message.bot.send_message(message.chat.id, model_text(owner, meta),
                                       message_thread_id=created.message_thread_id,
                                       reply_markup=model_kb(owner, meta))
        await message.answer("Тема создана.", reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text=created.name, url=topic_link(message.chat.id, created.message_thread_id))]]))
        return
    old = current(owner, message, make=False)
    try:
        create(owner, message.chat.id, None, old and old["engine"])
    except jobs.Refused as e:
        await message.answer(str(e))
        return
    await message.answer("Новая беседа. Контекст чистый, прошлые беседы целы — /sessions")


@dp.message(F.forum_topic_created | F.forum_topic_edited)
async def on_topic_event(message: Message, owner):
    meta = find(owner, message.chat.id, thread(message))
    if message.forum_topic_created:
        if meta:
            return
        try:
            meta = create(owner, message.chat.id, thread(message), by=message.from_user.id,
                          title=message.forum_topic_created.name)
        except jobs.Refused as e:
            await message.answer(str(e))
            return
        await message.answer("Тема — отдельная беседа. Выберите модель или просто напишите вопрос.\n\n"
                             + model_text(owner, meta), reply_markup=model_kb(owner, meta))
    elif meta and message.forum_topic_edited.name:
        core.save_chat(owner, {**meta, "title": message.forum_topic_edited.name})


@dp.message(Command("sessions"))
async def on_sessions(message: Message, owner):
    if is_group(message):
        items = tg_chats(owner, message)
        rows = [[InlineKeyboardButton(text=f"{MARKS[m['engine']]} {m['title'] or 'без названия'}"[:60],
                                      url=topic_link(message.chat.id, m["tg"]["thread"]))]
                for m in items if m["tg"]["thread"] != 1]
        await message.answer("Темы с беседами — нажмите для перехода:" if rows else "Тем с беседами пока нет.",
                             reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))
        return
    text, kb = sessions_page(owner, message, 0)
    await message.answer(text, reply_markup=kb)


@dp.callback_query(F.data.regexp(r"^(sess|del)page:-?\d+$"))
async def on_sessions_page(call: CallbackQuery, owner):
    prefix, page = call.data.split("page:")
    await edit(call, *sessions_page(owner, call.message, int(page), prefix))
    await call.answer()


@dp.callback_query(F.data.startswith("sess:"))
async def on_session_pick(call: CallbackQuery, owner):
    meta = core.load_chat(owner, call.data[5:])
    if not meta or not jobs.in_tg(meta) or meta["tg"]["chat"] != call.message.chat.id:
        await call.answer("Беседы больше нет")
        return
    set_current(owner, meta["id"])
    await edit(call, *sessions_page(owner, call.message, 0))
    await call.answer(f"Переключил на «{meta['title'] or 'новую'}»")


@dp.message(Command("delete"))
async def on_delete(message: Message, who, owner):
    if not is_group(message):
        text, kb = sessions_page(owner, message, 0, "del")
        await message.answer(text, reply_markup=kb)
        return
    meta = current(owner, message, make=False)
    if not meta or not jobs.in_tg(meta):
        await message.answer("В этой теме нет беседы.")
        return
    if not manager(who, owner, meta, message.from_user.id):
        await message.answer("Удалить беседу может создатель темы или владелец группы.")
        return
    await message.answer("Удалить историю ИИ и файлы этой темы? Восстановить их будет нельзя.",
                         reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                             InlineKeyboardButton(text="Удалить", callback_data=f"delyes:{meta['id']}"),
                             InlineKeyboardButton(text="Отмена", callback_data="delno")]]))


@dp.callback_query(F.data.startswith("del:"))
async def on_delete_pick(call: CallbackQuery, owner):
    meta = core.load_chat(owner, call.data[4:])
    if not meta:
        await call.answer("Беседы больше нет")
        return
    await edit(call, f"Удалить беседу {MARKS[meta['engine']]} «{meta['title'] or 'без названия'}»? "
                     "История и файлы будут удалены. Восстановить будет нельзя.",
               InlineKeyboardMarkup(inline_keyboard=[[
                   InlineKeyboardButton(text="Да", callback_data=f"delyes:{meta['id']}"),
                   InlineKeyboardButton(text="Нет", callback_data="delno")]]))
    await call.answer()


@dp.callback_query(F.data.startswith("delyes:"))
async def on_delete_confirm(call: CallbackQuery, who, owner):
    meta = core.load_chat(owner, call.data[7:])
    if not meta or not jobs.in_tg(meta) or meta["tg"]["chat"] != call.message.chat.id:
        await call.answer("Беседы больше нет")
        return
    if not manager(who, owner, meta, call.from_user.id):
        await call.answer("Удалить беседу может создатель темы или владелец группы.", show_alert=True)
        return
    if busy(owner, meta):
        await call.answer("Дождитесь ответа", show_alert=True)
        return
    core.delete_chat(owner, meta)
    if is_group(call.message):
        await edit(call, "История ИИ и файлы темы удалены. Следующее сообщение начнёт новую беседу.")
    else:
        await edit(call, *sessions_page(owner, call.message, 0, "del"))
    await call.answer("Удалено")


@dp.callback_query(F.data == "delno")
async def on_delete_cancel(call: CallbackQuery, owner):
    if is_group(call.message):
        await edit(call, "Удаление отменено.")
    else:
        await edit(call, *sessions_page(owner, call.message, 0, "del"))
    await call.answer("Отменено")


async def settings_target(event, who, owner):
    """Беседа, чьи настройки меняют, или None (причина уже сказана)."""
    message = event.message if isinstance(event, CallbackQuery) else event
    say = (lambda t: event.answer(t, show_alert=True)) if isinstance(event, CallbackQuery) else message.answer
    try:
        meta = current(owner, message)
    except jobs.Refused as e:
        await say(str(e))
        return None
    if not jobs.in_tg(meta):
        await say(MOVED)
    elif not manager(who, owner, meta, event.from_user.id):
        await say("Настройки темы меняет её создатель или владелец группы.")
    elif busy(owner, meta):
        await say("Дождитесь ответа и повторите.")
    else:
        return meta
    return None


@dp.message(Command("model"))
async def on_model(message: Message, who, owner):
    meta = await settings_target(message, who, owner)
    if meta:
        await message.answer(model_text(owner, meta), reply_markup=model_kb(owner, meta))


@dp.callback_query(F.data.startswith("model:"))
async def on_model_pick(call: CallbackQuery, who, owner):
    meta = await settings_target(call, who, owner)
    if not meta:
        return
    engine, slug = call.data.split(":", 2)[1:]
    avail = users.available(users.settings(owner))
    if not any(m["slug"] == slug for m in avail.get(engine, [])):
        await call.answer("Эта модель вам недоступна", show_alert=True)
        return
    if engine != meta["engine"]:
        # Сессии у движков разные. Пустую беседу просто пересоздаём, иначе нужна новая.
        if core.history(owner, meta["id"]):
            await call.answer("Другой движок — это новая беседа: /new" if not is_group(call.message)
                              else "За темой закреплён движок. Для другого создайте тему: /new", show_alert=True)
            return
        core.delete_chat(owner, meta)
        meta = create(owner, meta["tg"]["chat"], meta["tg"]["thread"], engine, by=meta["tg"]["by"],
                      title=meta["title"])
    meta["model"] = slug
    if jobs.allowed_model(avail, engine, slug, meta["effort"]):
        meta["effort"] = jobs.default_effort(avail, engine, slug)
    core.save_chat(owner, meta)
    await edit(call, model_text(owner, meta), model_kb(owner, meta))
    await call.answer(f"Теперь {slug}")


@dp.message(Command("effort"))
async def on_effort(message: Message, who, owner):
    meta = await settings_target(message, who, owner)
    if meta:
        await message.answer(f"Размышление: {meta['effort'] or 'по умолчанию'}", reply_markup=effort_kb(owner, meta))


@dp.callback_query(F.data.startswith("effort:"))
async def on_effort_pick(call: CallbackQuery, who, owner):
    meta = await settings_target(call, who, owner)
    if not meta:
        return
    level = call.data.split(":", 1)[1]
    reason = jobs.allowed_model(users.available(users.settings(owner)), meta["engine"], meta["model"], level)
    if reason:
        await call.answer(reason, show_alert=True)
        return
    meta["effort"] = level
    core.save_chat(owner, meta)
    await edit(call, f"Размышление: {level or 'по умолчанию'}", effort_kb(owner, meta))
    await call.answer(f"Теперь {level or 'по умолчанию'}")


# --- Сообщения ---

async def transcribe_attachment(message):
    """Аудио существует только в памяти и не попадает в файлы беседы."""
    source = message.voice or message.audio
    if (source.file_size or 0) > MAX_DOWNLOAD:
        raise ValueError(f"Аудио больше {jobs.human(MAX_DOWNLOAD)} — его не скачать.")
    with io.BytesIO() as buffer:
        await message.bot.download(source, destination=buffer)
        return await stt.transcribe(buffer.getvalue())


async def download_attachment(uid, message, cwd):
    source, name, mime = session_files.attachment(message)
    if (source.file_size or 0) > MAX_DOWNLOAD:
        raise ValueError(f"Файл {name} больше {jobs.human(MAX_DOWNLOAD)} — Telegram не даёт его скачать.")
    inbox = session_files.directory(Path(cwd) / "files" / "from_user")
    # Недокачанный файл не должен попасть к модели, даже после перезапуска.
    partial = session_files.directory(Path(core.user_dir(uid)) / "downloads") / (uuid.uuid4().hex + ".part")
    try:
        await message.bot.download(source, destination=partial)
        destination = session_files.vacant(inbox / name)
        partial.rename(destination)
    finally:
        partial.unlink(missing_ok=True)
    return destination, mime


def compact_kb(uid, meta):
    """Кнопка «Сжать беседу», если сессия Claude больше порога пользователя."""
    due = jobs.compact_due(uid, meta)
    if not due:
        return None
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(
        text=f"🗜 Сжать беседу · {jobs.human(due[0])}", callback_data=f"compact:{meta['id']}")]])


def compacted_text(e):
    was = f": было ≈{round(e['tokens'] / 1000):,} тыс. токенов".replace(",", " ") if e.get("tokens") else ""
    return ("Claude сам сжал беседу" if e.get("auto") else "Беседа сжата") + was


async def deliver(message, uid, meta, events):
    """Ответ из журнала беседы: текст, ошибки и готовые файлы. Инструменты видно на сайте.
    Под последним куском текста — «Сжать беседу», если сессия выросла больше порога."""
    text = "\n\n".join(e["text"] for e in events if e["kind"] == "text")
    chunks = split_message(text)
    kb = compact_kb(uid, meta) if chunks else None
    for i, chunk in enumerate(chunks):
        await message.answer(chunk, reply_markup=kb if i == len(chunks) - 1 else None)
    for e in events:
        if e["kind"] in ("error", "warning"):
            await message.answer(("Ошибка: " if e["kind"] == "error" else "") + e["text"])
        elif e["kind"] == "stopped":
            await message.answer("Ответ остановлен.")
        elif e["kind"] == "compacted":
            await message.answer(compacted_text(e))
    cwd = core.workspace(uid, meta["id"])
    for e in events:
        for rel in e.get("files", []) if e["kind"] == "done" else []:
            path = Path(cwd) / rel
            try:
                if path.stat().st_size > MAX_UPLOAD:
                    raise ValueError(f"больше {jobs.human(MAX_UPLOAD)}")
                if not path.stat().st_size:
                    raise ValueError("Telegram не принимает пустые файлы")
                await message.answer_document(FSInputFile(path), disable_content_type_detection=True)
            except Exception as e:
                log.exception("file delivery failed: %s", rel)
                await message.answer(f"Не удалось отправить {path.name}: {e}. Файл есть на сайте в 📁.")


async def respond(message, owner, text, attachments=()):
    try:
        meta = current(owner, message)
    except jobs.Refused as e:
        await message.answer(str(e))
        return
    async with locks[(owner, meta["id"])]:
        meta = core.load_chat(owner, meta["id"]) or meta  # пока ждали, беседу могли перенести
        if not jobs.in_tg(meta):
            await message.answer(MOVED)
            return
        voiced = [p for p in attachments if p.voice or p.audio]
        files = [p for p in attachments if not (p.voice or p.audio)]
        has_prompt = bool(text.strip()) or bool(voiced)
        cwd = core.workspace(owner, meta["id"])
        saved, images, notes = [], [], []
        try:
            data = users.settings(owner)
            jobs.room(owner, data, sum(session_files.attachment(p)[0].file_size or 0 for p in files))
            typing = (ChatActionSender.typing(bot=message.bot, chat_id=message.chat.id,
                                              message_thread_id=message.message_thread_id)
                      if has_prompt else nullcontext())
            async with typing:
                for part in voiced:
                    try:
                        transcript = await transcribe_attachment(part)
                    except Exception as e:
                        log.exception("stt failed")
                        await message.answer(f"Не смог расшифровать аудио: {e}")
                        continue
                    if not transcript:
                        await message.answer("Тишина или не разобрал — попробуйте ещё раз.")
                        continue
                    notes.append("Расшифровка аудио: " + transcript)
                    for chunk in split_message("🎤 " + transcript):
                        await message.answer(chunk)
                for part in files:
                    try:
                        path, mime = await download_attachment(owner, part, cwd)
                    except Exception as e:
                        log.exception("download failed")
                        await message.answer(f"Не удалось скачать вложение: {e}")
                        continue
                    saved.append(path.name)
                    if mime in jobs.IMAGE_TYPES and path.stat().st_size <= core.MAX_IMAGE:
                        images.append((path.read_bytes(), mime))
                prompt = "\n\n".join(p for p in (text.strip(), *notes) if p)
                if not prompt:
                    return  # вложения без подписи только сохраняем: следующий текст их подхватит
                if is_group(message):
                    prompt = f"{message.from_user.full_name}: {prompt}"  # в теме пишут несколько человек
                jobs.check(owner, meta)
                start = len(core.history(owner, meta["id"]))
                job = await jobs.start(owner, meta, prompt, data, saved, images, channel="tg")
                await job.task
        except jobs.Refused as e:
            await message.answer(str(e))
            return
        await deliver(message, owner, meta, core.history(owner, meta["id"])[start + 1:])


@dp.callback_query(F.data.startswith("compact:"))
async def on_compact(call: CallbackQuery, owner):
    """«Сжать беседу» под ответом: то же, что кнопка на сайте."""
    meta = core.load_chat(owner, call.data[len("compact:"):])
    if not meta or not jobs.in_tg(meta) or meta["tg"]["chat"] != call.message.chat.id:
        await call.answer("Беседы больше нет")
        return
    with contextlib.suppress(TelegramBadRequest):  # кнопка одноразовая
        await call.message.edit_reply_markup(reply_markup=None)
    await call.answer("Сжимаю беседу…")
    async with locks[(owner, meta["id"])]:
        meta = core.load_chat(owner, meta["id"]) or meta
        start = len(core.history(owner, meta["id"]))
        try:
            async with ChatActionSender.typing(bot=call.bot, chat_id=call.message.chat.id,
                                               message_thread_id=call.message.message_thread_id):
                job = await jobs.compact(owner, meta, channel="tg")
                await job.task
        except jobs.Refused as e:
            await call.message.answer(str(e))
            return
        await deliver(call.message, owner, meta, core.history(owner, meta["id"])[start + 1:])


@dp.message(F.text)
async def on_text(message: Message, owner):
    await respond(message, owner, message.text)


async def collect_album(group, owner):
    """Ждём, пока альбом догрузится, и отвечаем на него одним запросом."""
    try:
        while (left := album_deadline.get(group, 0) - time.monotonic()) > 0:
            await asyncio.sleep(left)
        parts = albums.pop(group, [])
        album_deadline.pop(group, None)
        if parts:
            await respond(parts[-1], owner, "\n".join(p.caption for p in parts if p.caption), parts)
    except Exception:
        log.exception("album failed")


@dp.message(F.photo | F.document | F.video | F.animation | F.video_note | F.sticker | F.voice | F.audio)
async def on_file(message: Message, owner):
    if not message.media_group_id:
        await respond(message, owner, message.caption or "", [message])
        return
    group = (message.chat.id, message.media_group_id)
    albums[group].append(message)
    album_deadline[group] = time.monotonic() + ALBUM_WAIT
    if len(albums[group]) == 1:
        asyncio.create_task(collect_album(group, owner))


@dp.message(F.chat.type == "private")
async def on_other(message: Message):
    await message.answer("Пришлите текст, фото, документ, аудио или другое вложение.")


# --- Запуск ---

async def run(token):
    """Опрос Telegram до отмены задачи. Публичный адрес сайту для этого не нужен."""
    global bot, bot_name
    bot = Bot(token)
    while bot_name is None:
        try:
            bot_name = (await bot.get_me()).username
        except TelegramNetworkError as e:
            log.warning("Telegram недоступен: %s", e)
            await asyncio.sleep(30)
        except TelegramUnauthorizedError:
            log.error("Telegram не принял NEXUS_TG_TOKEN — бот выключен")
            await bot.session.close()
            bot = None
            return
    try:
        await bot.set_my_commands([
            BotCommand(command="start", description="что умеет бот"),
            BotCommand(command="info", description="текущая модель и беседа"),
            BotCommand(command="model", description="модель и движок"),
            BotCommand(command="effort", description="уровень размышления"),
            BotCommand(command="new", description="новая беседа или тема группы"),
            BotCommand(command="sessions", description="беседы и темы группы"),
            BotCommand(command="delete", description="удалить беседу")])
    except TelegramNetworkError as e:
        log.warning("меню команд не обновилось: %s", e)  # косметика, не роняем
    jobs.notify = notify
    log.info("бот @%s запущен", bot_name)
    try:
        # resolve_used_update_types — иначе нажатия кнопок до бота не доедут.
        await dp.start_polling(bot, handle_signals=False, allowed_updates=dp.resolve_used_update_types())
    finally:
        jobs.notify = None
        await bot.session.close()
        bot = None
