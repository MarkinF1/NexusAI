"""Миниатюры фото для окна «Файлы беседы» и библиотеки.

Плитке хватает картинки в пару сотен точек, а фото с телефона весит мегабайты.
Миниатюра делается один раз, лежит в users/<id>/thumbs (модель эту папку не видит)
и отдаётся вместо оригинала. Ключ — номер файла, размер и время изменения: у
жёстких ссылок беседы и библиотеки одна миниатюра на двоих, а замена файла сразу
даёт новую.
"""

import logging
import os
import threading
import time
from pathlib import Path

from PIL import Image, ImageOps

try:  # фото iPhone в HEIC: браузеры, кроме Safari, сами их не показывают
    from pillow_heif import register_heif_opener
    register_heif_opener()
except ImportError:  # без пакета HEIC остаётся значком файла
    pass

log = logging.getLogger("nexus.thumbs")

SIZE = 360          # длинная сторона: плитка до ~180 точек, на ретине вдвое больше
QUALITY = 78
MEDIA = "image/webp"
RASTER = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".avif", ".bmp", ".heic", ".heif", ".tif", ".tiff"}
KEEP_DAYS = 30      # миниатюру, которую столько не открывали, убираем
SWEEP_EVERY = 24 * 3600

# Два ядра на сервере: окно с сотней фото не должно занять их все сразу.
slots = threading.Semaphore(2)
swept = {}          # папка -> когда убирали


class Unsupported(Exception):
    """Не картинка или формат, который Pillow не читает."""


def supported(path):
    return Path(path).suffix.lower() in RASTER


def key(info):
    return f"{info.st_dev:x}-{info.st_ino:x}-{info.st_size:x}-{info.st_mtime_ns:x}"


def make(source, target):
    """Уменьшенная копия source в target (WebP). Поворот по EXIF учитывается."""
    temp = target.with_name(f"{target.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    try:
        with Image.open(source) as image:
            image.draft("RGB", (SIZE, SIZE))  # JPEG сразу читается уменьшенным — в разы быстрее
            image = ImageOps.exif_transpose(image)  # у GIF и многостраничных — первый кадр
            image.thumbnail((SIZE, SIZE), Image.Resampling.LANCZOS)
            if image.mode not in ("RGB", "RGBA"):
                alpha = "A" in image.getbands() or "transparency" in image.info
                image = image.convert("RGBA" if alpha else "RGB")
            image.save(temp, "WEBP", quality=QUALITY, method=4)
    except (OSError, ValueError, SyntaxError, Image.DecompressionBombError) as err:
        temp.unlink(missing_ok=True)
        raise Unsupported(str(err)) from err
    os.replace(temp, target)  # параллельный запрос не увидит недописанный файл


def thumbnail(folder, source):
    """Путь к миниатюре source (делается при первом запросе). Unsupported, если
    это не картинка."""
    source = Path(source)
    if not supported(source):
        raise Unsupported(source.suffix)
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    info = source.stat()
    target = folder / (key(info) + ".webp")
    if target.exists():
        os.utime(target)  # открывали — уборка не тронет
    else:
        with slots:
            if not target.exists():
                started = time.monotonic()
                make(source, target)
                log.debug("миниатюра %s за %.2f с", source.name, time.monotonic() - started)
        drop_stale(folder, info, target)
    sweep(folder)
    return target


def drop_stale(folder, info, keep):
    """Прежние миниатюры того же файла: его заменили новой версией."""
    for old in folder.glob(f"{info.st_dev:x}-{info.st_ino:x}-*.webp"):
        if old != keep:
            old.unlink(missing_ok=True)


def sweep(folder, now=None):
    """Раз в сутки убирает миниатюры, которые давно не открывали: файлы удалили."""
    now = now or time.time()
    if now - swept.get(str(folder), 0) < SWEEP_EVERY:
        return
    swept[str(folder)] = now
    for old in Path(folder).iterdir():
        try:
            if now - old.stat().st_mtime > KEEP_DAYS * 86400:
                old.unlink()
        except OSError:
            pass
