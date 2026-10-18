"""Web Push «Ответ готов»: уведомления в браузер и в веб-приложение iPhone
(iOS 16.4+, только для сайта, добавленного на экран «Домой», и только по https).

Ключи VAPID создаются один раз и лежат в data/vapid.json. Подписки — по устройствам,
в data/users/<id>/push.json: каждое устройство включает уведомления у себя в «Настройках».
"""
import asyncio
import base64
import json
import logging
import os
import threading
import time
from urllib.parse import urlparse

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec

import core

log = logging.getLogger("nexus.push")
# Почтовый адрес или https-ссылка владельца — так требует VAPID; сервис пуша (Apple, Google)
# пишет туда, если с рассылкой что-то не так.
SUBJECT = "https://github.com/MarkinF1/NexusAI"
TTL = 12 * 3600  # устройство выключено дольше — уведомление уже не нужно
_lock = threading.Lock()


def _b64(data):
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def keys_path():
    return os.path.join(core.DATA, "vapid.json")


def keys():
    """{"private": PEM, "public": ключ для браузера}; нет — создаём."""
    with _lock:
        try:
            with open(keys_path(), encoding="utf-8") as f:
                return json.load(f)
        except (OSError, ValueError):
            pass
        private = ec.generate_private_key(ec.SECP256R1())
        pem = private.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                    serialization.NoEncryption()).decode()
        point = private.public_key().public_bytes(serialization.Encoding.X962,
                                                  serialization.PublicFormat.UncompressedPoint)
        data = {"private": pem, "public": _b64(point)}
        os.makedirs(core.DATA, exist_ok=True)
        tmp = keys_path() + ".tmp"
        with open(os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w", encoding="utf-8") as f:
            json.dump(data, f)
        os.replace(tmp, keys_path())
        return data


def public_key():
    return keys()["public"]


def _path(uid):
    return os.path.join(core.user_dir(uid), "push.json")


def subscriptions(uid):
    try:
        with open(_path(uid), encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return []


def _save(uid, subs):
    path = _path(uid)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path + ".tmp", "w", encoding="utf-8") as f:
        json.dump(subs, f, ensure_ascii=False)
    os.replace(path + ".tmp", path)


def clean(sub):
    """Подписка из браузера: только https-адрес сервиса пуша и два ключа. Не то — ValueError."""
    try:
        endpoint, p256dh, auth = sub["endpoint"], sub["keys"]["p256dh"], sub["keys"]["auth"]
    except (KeyError, TypeError):
        raise ValueError("Неполная подписка") from None
    if urlparse(endpoint).scheme != "https" or not all(isinstance(v, str) and v for v in (p256dh, auth)):
        raise ValueError("Неверная подписка")
    return {"endpoint": endpoint, "keys": {"p256dh": p256dh, "auth": auth}}


def add(uid, sub, label=""):
    sub = clean(sub)
    with _lock:
        subs = [s for s in subscriptions(uid) if s["endpoint"] != sub["endpoint"]]
        subs.append({**sub, "label": str(label)[:80], "added": time.time()})
        _save(uid, subs)


def remove(uid, endpoint):
    with _lock:
        subs = subscriptions(uid)
        kept = [s for s in subs if s["endpoint"] != endpoint]
        if len(kept) != len(subs):
            _save(uid, kept)


def _send(sub, payload):
    """Одна отправка (блокирующая — из потока). True — доставлено, False — подписка умерла."""
    from py_vapid import Vapid
    from pywebpush import WebPushException, webpush
    try:
        vapid = Vapid.from_pem(keys()["private"].encode())
        # RFC 8292 разрешает в sub и https-ссылку; py_vapid по умолчанию требует только mailto:.
        vapid.conf["no-strict"] = True
        webpush({"endpoint": sub["endpoint"], "keys": sub["keys"]}, json.dumps(payload, ensure_ascii=False),
                vapid_private_key=vapid,
                vapid_claims={"sub": SUBJECT}, ttl=TTL, timeout=15)
        return True
    except WebPushException as e:
        status = getattr(e.response, "status_code", None)
        if status in (404, 410):  # устройство отписалось или приложение удалили
            return False
        raise


async def notify(uid, title, body, url="/"):
    """Уведомление на все устройства пользователя; мёртвые подписки убираем. Ошибки — в журнал."""
    payload = {"title": title, "body": body, "url": url}
    sent = 0
    for sub in subscriptions(uid):
        try:
            if await asyncio.to_thread(_send, sub, payload):
                sent += 1
            else:
                remove(uid, sub["endpoint"])
        except Exception as e:
            log.warning("push %s: %s", urlparse(sub["endpoint"]).netloc, e)
    return sent
