"""python -m unittest test_tg — бот через настоящий Dispatcher aiogram, без сети."""
import os
import unittest

import test_core  # noqa: F401  фальшивый claude и временная папка данных — до импорта app
import app
import core
import jobs
import tg
import users
from aiogram import Bot
from aiogram.client.session.base import BaseSession
from aiogram.types import CallbackQuery, ForumTopic, Message, Update

GROUP = -1001234567890


class Telegram(BaseSession):
    """Транспорт без сети: запоминает вызовы API и отвечает правдоподобно."""

    def __init__(self):
        super().__init__()
        self.calls = []
        self.next_id = 1000

    async def close(self):
        pass

    async def stream_content(self, *args, **kwargs):
        yield b"test"

    async def make_request(self, client, method, timeout=None):
        self.calls.append(method)
        self.next_id += 1
        name = method.__api_method__
        if name == "createForumTopic":
            return ForumTopic(message_thread_id=self.next_id, name=method.name, icon_color=0)
        if name in ("sendMessage", "editMessageText"):
            return Message.model_validate(dict(
                message_id=self.next_id, date=0, text=method.text,
                chat=dict(id=method.chat_id, type="supergroup" if method.chat_id < 0 else "private"),
                from_user=dict(id=999, is_bot=True, first_name="Bot")), context={"bot": client})
        return True

    def texts(self):
        return [c.text for c in self.calls if c.__api_method__ == "sendMessage"]


def tg_chats(uid):
    return [m for m in core.list_chats(uid) if m.get("tg")]  # id пользователей делим с test_core


class Bot_(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.api = Telegram()
        self.bot = Bot("999:TEST", session=self.api)
        tg.bot_name = "test_bot"
        self.seq = 0

    async def feed(self, text, user=500, chat=None, thread=None):
        self.seq += 1
        data = dict(message_id=self.seq, date=0, text=text,
                    chat=dict(id=chat or user, type="supergroup" if chat else "private", is_forum=bool(chat)),
                    from_user=dict(id=user, is_bot=False, first_name=f"U{user}"))
        if thread:
            data.update(message_thread_id=thread, is_topic_message=True)
        message = Message.model_validate(data, context={"bot": self.bot})
        await tg.dp.feed_update(self.bot, Update(update_id=self.seq, message=message))

    async def test_link_chat_move(self):
        uid = users.add("tg-owner", "pw")
        await self.feed("hi", user=501)  # не привязан: модель не запускается
        self.assertEqual(self.api.texts()[-1], tg.LINK_HINT)

        code = tg.link(uid).split("start=")[1]
        await self.feed(f"/start {code}")
        self.assertEqual(users.by_tg(500)["id"], uid)
        await self.feed(f"/start {code}", user=501)  # код одноразовый
        self.assertIsNone(users.by_tg(501))

        await self.feed("привет")
        self.assertEqual(self.api.texts()[-1], "Привет")  # ответ фальшивого claude
        [meta] = tg_chats(uid)
        self.assertEqual(meta["tg"]["chat"], 500)
        self.assertEqual([e["kind"] for e in core.history(uid, meta["id"])][0], "user")

        user = users.get(uid)
        self.assertNotIn(meta["id"], [c["id"] for c in app.chats(user)])  # без галочки «Показывать» скрыта
        with self.assertRaises(app.HTTPException):  # с сайта в неё не пишут
            await app.send(meta["id"], "x", [], user)

        app.move_chat(meta["id"], user)
        await self.feed("ещё")
        self.assertEqual(self.api.texts()[-1], tg.MOVED)
        self.assertIn(meta["id"], [c["id"] for c in app.chats(user)])  # перенесённая видна всегда
        await app.send(meta["id"], "с сайта", [], user)
        await jobs.jobs[(uid, meta["id"])].task
        self.assertEqual(sum(e["kind"] == "user" for e in core.history(uid, meta["id"])), 2)

    async def press(self, data, message_id, user=500):
        self.seq += 1
        message = Message.model_validate(dict(message_id=message_id, date=0, text="x",
                                              chat=dict(id=user, type="private"),
                                              from_user=dict(id=999, is_bot=True, first_name="Bot")),
                                         context={"bot": self.bot})
        call = CallbackQuery(id=str(self.seq), from_user=dict(id=user, is_bot=False, first_name="U"),
                             chat_instance="c", data=data, message=message)
        await tg.dp.feed_update(self.bot, Update(update_id=self.seq, callback_query=call))

    async def test_compact_button(self):
        uid = users.add("tg-compact", "pw")
        users.save_settings(uid, {**users.settings(uid), "compact_mb": 1})
        code = tg.link(uid).split("start=")[1]
        await self.feed(f"/start {code}", user=520)
        await self.feed("привет", user=520)
        sent = [c for c in self.api.calls if c.__api_method__ == "sendMessage"]
        self.assertIsNone(sent[-1].reply_markup)  # сессия маленькая — кнопки нет
        [meta] = tg_chats(uid)
        project = os.path.join(core.claude_home(uid), "projects", "-tg")
        os.makedirs(project, exist_ok=True)
        with open(os.path.join(project, meta["sid"] + ".jsonl"), "wb") as f:
            f.write(b'{"type":"user"}\n' * 80000)  # ~1.3 МБ — больше порога 1 МБ
        await self.feed("ещё", user=520)
        sent = [c for c in self.api.calls if c.__api_method__ == "sendMessage"]
        button = sent[-1].reply_markup.inline_keyboard[0][0]
        self.assertTrue(button.text.startswith("🗜 Сжать беседу"))
        self.assertEqual(button.callback_data, f"compact:{meta['id']}")
        await self.press(button.callback_data, message_id=self.api.next_id, user=520)
        kinds = [e["kind"] for e in core.history(uid, meta["id"])]
        self.assertEqual(kinds[-3:], ["compact", "compacted", "done"])  # без «ошибки»: у /compact нет текста
        self.assertEqual(self.api.texts()[-1], "Беседа сжата: было ≈14 тыс. токенов")
        await self.press(f"compact:{meta['id']}", message_id=1, user=521)  # чужой: не привязан
        self.assertEqual(sum(e["kind"] == "compact" for e in core.history(uid, meta["id"])), 1)

    async def test_group(self):
        owner, guest = users.add("group-owner", "pw"), users.add("guest", "pw")
        users.link_tg(owner, 600, "owner")
        users.link_tg(guest, 601, "guest")
        await self.feed("hi", user=601, chat=GROUP, thread=7)  # группа не подключена
        self.assertEqual(tg_chats(owner) + tg_chats(guest), [])
        await self.feed("/start", user=600, chat=GROUP)
        await self.feed("вопрос", user=601, chat=GROUP, thread=7)  # пишет гость, беседа владельца
        [meta] = tg_chats(owner)
        self.assertEqual((meta["tg"]["thread"], meta["tg"]["by"]), (7, 601))
        self.assertEqual(core.history(owner, meta["id"])[0]["text"], "U601: вопрос")
        self.assertEqual(tg_chats(guest), [])


if __name__ == "__main__":
    unittest.main()
