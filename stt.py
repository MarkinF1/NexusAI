"""Расшифровка голосовых: локальная модель faster-whisper, общая для сайта и бота."""
import asyncio
import io
import logging

import settings

log = logging.getLogger("nexus.stt")
# Модель и язык — настройки stt_model и stt_lang: small — быстро, large-v3-turbo — точнее.
model = None  # грузится при первом голосовом: от 0,7 ГБ памяти
loaded = ""  # имя загруженной модели: сменили настройку — при следующем голосовом грузим новую
lock = asyncio.Lock()  # по одной расшифровке: модель одна, процессор общий


def run(data):
    """Блокирующая расшифровка — только из потока."""
    global model, loaded
    name = settings.get(settings.STT_MODEL.name) or settings.STT_MODEL.default
    if model is None or loaded != name:
        from faster_whisper import WhisperModel
        log.info("загружаю модель распознавания %s", name)
        model = None  # прежнюю отпускаем до загрузки новой: памяти на две может не хватить
        model = WhisperModel(name, device="cpu", compute_type="int8")
        loaded = name
    segments, _ = model.transcribe(io.BytesIO(data), language=settings.get(settings.STT_LANG.name) or None, vad_filter=True)
    return " ".join(s.text.strip() for s in segments).strip()


async def transcribe(data):
    async with lock:
        return await asyncio.to_thread(run, data)
