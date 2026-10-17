"""Расшифровка голосовых: локальная модель faster-whisper, общая для сайта и бота."""
import asyncio
import io
import logging
import os

log = logging.getLogger("nexus.stt")
MODEL = os.environ.get("NEXUS_TG_STT_MODEL", "small")
LANG = os.environ.get("NEXUS_TG_STT_LANG") or None  # пусто — определять само

model = None  # грузится при первом голосовом: ~1 ГБ памяти
lock = asyncio.Lock()  # по одной расшифровке: модель одна, процессор общий


def run(data):
    """Блокирующая расшифровка — только из потока."""
    global model
    if model is None:
        from faster_whisper import WhisperModel
        log.info("загружаю модель распознавания %s", MODEL)
        model = WhisperModel(MODEL, device="cpu", compute_type="int8")
    segments, _ = model.transcribe(io.BytesIO(data), language=LANG, vad_filter=True)
    return " ".join(s.text.strip() for s in segments).strip()


async def transcribe(data):
    async with lock:
        return await asyncio.to_thread(run, data)
