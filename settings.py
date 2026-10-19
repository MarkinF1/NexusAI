"""Общие настройки сервиса: data/settings.json.

.env — только первоначальные значения. При старте настройки читаются из settings.json;
если файла нет или в нём не хватает параметров, недостающие берутся из .env, а если нет и
там — по умолчанию, и файл сразу дописывается. Дальше настройки меняет администратор в
«Настройках» → «Общие настройки», без перезапуска: кто читает параметр, берёт его в момент
использования — settings.get(settings.TIMEOUT.name). В .env остаётся то, что нужно до старта:
адрес и порт сайта, папка данных (в ней и лежит этот файл), доверенный прокси и пути к CLI.
"""
import json
import os
import threading
from dataclasses import dataclass, field

HERE = os.path.dirname(os.path.abspath(__file__))


@dataclass(frozen=True)
class Parameter:
    """Параметр общих настроек. Тип значения — тип default: число, строка или список
    (несколько значений из choices: в форме — галочки, отмечаешь нужные)."""
    name: str  # техническое имя: ключ в settings.json и в коде
    title: str  # название на экране
    about: str  # за что отвечает
    default: object
    env: tuple = ()  # переменные .env с первоначальным значением, по порядку
    choices: tuple = ()  # допустимые значения; пусто — любые своего типа
    secret: bool = False  # в форме — скрытым полем
    labels: dict = field(default_factory=dict)  # значение -> как его показать на экране
    # Выбор, который знает только работающий сайт (каталог моделей codex): функция -> [(значение, подпись)].
    options: object = None
    shown_if: tuple = ()  # (имя параметра-списка, значение): показывать, только пока оно отмечено
    aliases: tuple = ()  # прежние имена в settings.json — значение переносится под новое

    @property
    def kind(self):
        if isinstance(self.default, list):
            return "list"
        return "int" if isinstance(self.default, int) else "str"

    def label(self, value):
        return self.labels.get(value, str(value))

    def choice_list(self):
        """[(значение, подпись)] — постоянный выбор или каталог от работающего сайта."""
        if self.options:
            try:
                return list(self.options())
            except Exception:  # каталог недоступен — выбор пустой, а не падение формы
                return []
        return [(c, self.label(c)) for c in self.choices]

    def convert(self, value, strict=True):
        """Значение к типу параметра; не подходит — ValueError с понятным текстом.
        strict=False — без сверки с каталогом (при запуске его ещё нет)."""
        if self.kind == "list":
            # Из .env и старых файлов приходит строкой: «[claude, codex]» или «claude, codex».
            items = value if isinstance(value, list) else str(value or "").strip("[] ").split(",")
            picked = {str(v).strip() for v in items if str(v).strip()}
            wrong = {v for v in picked if v not in self.choices}  # set() здесь — свой, для настроек
            if wrong:
                raise ValueError(f"«{self.title}»: нет значения {', '.join(sorted(wrong))}")
            if not picked:
                raise ValueError(f"«{self.title}»: отметьте хотя бы одно")
            return [c for c in self.choices if c in picked]  # порядок — как в choices
        if self.kind == "int":
            try:
                result = int(str(value).strip())
            except ValueError:
                raise ValueError(f"«{self.title}»: нужно целое число") from None
            if result < 0:
                raise ValueError(f"«{self.title}»: не может быть меньше нуля")
        else:
            result = "" if value is None else str(value).strip()
        if self.choices and result not in self.choices:
            raise ValueError(f"«{self.title}»: допустимо только {', '.join(map(str, self.choices))}")
        if self.options and strict and result != self.default and result not in dict(self.choice_list()):
            raise ValueError(f"«{self.title}»: нет такого значения — {result}")
        return result


def codex_catalog():
    import core  # здесь, а не наверху: core сам импортирует настройки
    return [("", "Первая в каталоге Codex")] + [(m["slug"], m["name"]) for m in core.codex_models()]


CLAUDE_MODEL = Parameter(
    "claude_model", "Модель Claude по умолчанию", "С какой моделью создаются новые беседы Claude.",
    "sonnet", ("NEXUS_MODEL",), ("opus", "sonnet", "haiku"), aliases=("model",))
CODEX_MODEL = Parameter(
    "codex_model", "Модель Codex по умолчанию",
    "С какой моделью создаются новые беседы Codex. Список — из каталога самого Codex.",
    "", ("NEXUS_CODEX_MODEL",), options=codex_catalog)
# Модель по умолчанию для новой беседы каждой сети.
DEFAULT_MODEL = {"claude": CLAUDE_MODEL, "codex": CODEX_MODEL}
TIMEOUT = Parameter(
    "timeout", "Предел одного ответа, секунд",
    "Сколько может идти один ответ, прежде чем сайт его остановит. Обычно ответ останавливают "
    "кнопкой «Стоп».", 1800, ("NEXUS_TIMEOUT",))
COMPACT_MB = Parameter(
    "compact_mb", "Порог «Сжать беседу», МБ",
    "С какого размера сессии предлагать сжать беседу тем, кому порог не задан в «Памяти». "
    "0 — не предлагать.", 10, ("NEXUS_COMPACT_MB",))
USAGE_REFRESH = Parameter(
    "usage_refresh", "Опрос занятости подписки, секунд",
    "Как часто спрашивать у Anthropic и OpenAI, насколько занята подписка: так виден и расход "
    "мимо сайта. 0 — не спрашивать, проценты обновятся только после ответов на сайте.",
    120, ("NEXUS_USAGE_REFRESH",))
STT_MODEL = Parameter(
    "stt_model", "Модель распознавания голосовых",
    "Модель faster-whisper для голосовых на сайте и в Telegram: small — быстро, large-v3-turbo — "
    "заметно точнее, но в 4 раза медленнее. Скачивается и загружается при следующем голосовом.",
    "small", ("NEXUS_STT_MODEL", "NEXUS_TG_STT_MODEL"),
    ("tiny", "base", "small", "medium", "large-v3", "large-v3-turbo"))
STT_LANG = Parameter(
    "stt_lang", "Язык голосовых",
    "Код языка, например ru или en. Пусто — модель определяет язык сама.",
    "", ("NEXUS_STT_LANG", "NEXUS_TG_STT_LANG"))
TG_TOKEN = Parameter(
    "tg_token", "Токен Telegram-бота",
    "Токен от @BotFather. Пусто — бота нет. Новый токен сразу перезапускает бота. Один токен "
    "нельзя опрашивать из двух мест.", "", ("NEXUS_TG_TOKEN",), secret=True)
UPDATE_WAIT = Parameter(
    "update_wait", "Ожидание ответов при обновлении, секунд",
    "Сколько update.sh ждёт окончания идущих ответов, прежде чем перезапустить сайт.",
    1800, ("NEXUS_UPDATE_WAIT",))

# Порядок — порядок полей в форме.
PARAMETERS = (CLAUDE_MODEL, CODEX_MODEL, TIMEOUT, COMPACT_MB, USAGE_REFRESH, STT_MODEL, STT_LANG, TG_TOKEN, UPDATE_WAIT)
BY_NAME = {p.name: p for p in PARAMETERS}

_values = {}
_lock = threading.Lock()
_watchers = {}  # имя -> [функция(новое значение)]


def path():
    data = os.path.abspath(os.environ.get("NEXUS_DATA", os.path.join(HERE, "data")))
    return os.path.join(data, "settings.json")


def parameter(name):
    """Параметр по техническому имени; нет такого — KeyError."""
    return BY_NAME[name]


def dotenv():
    """Переменные из файла .env рядом с кодом. Сервис получает их окружением, но
    users.py из консоли — нет: без этого первый запуск оттуда потерял бы токен бота."""
    found = {}
    try:
        with open(os.path.join(HERE, ".env"), encoding="utf-8") as f:
            for line in f:
                key, sep, value = line.strip().partition("=")
                if sep and not key.startswith("#"):
                    found[key.strip()] = value.strip().strip("\"'")
    except OSError:
        pass
    return found


def initial(param, file_env=None):
    """Значение из окружения или файла .env, иначе по умолчанию."""
    sources = [os.environ, dotenv() if file_env is None else file_env]
    for env in param.env:
        raw = next((src[env] for src in sources if src.get(env, "").strip()), None)
        if raw is not None:
            try:
                return param.convert(raw, strict=False)
            except ValueError:
                break  # кривое значение в .env — берём по умолчанию
    return list(param.default) if isinstance(param.default, list) else param.default


def _save():
    target = path()
    os.makedirs(os.path.dirname(target), exist_ok=True)
    tmp = target + ".tmp"
    # Внутри токен бота — файл только для владельца.
    with open(os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w", encoding="utf-8") as f:
        json.dump(_values, f, ensure_ascii=False, indent=2)
    os.replace(tmp, target)


def load():
    """Прочитать settings.json, дополнить недостающее из .env или значений по умолчанию."""
    try:
        with open(path(), encoding="utf-8") as f:
            stored = json.load(f)
        if not isinstance(stored, dict):
            stored = {}
    except (OSError, ValueError):
        stored = {}
    values, file_env = {}, dotenv()
    for param in PARAMETERS:
        key = next((k for k in (param.name, *param.aliases) if k in stored), None)
        try:
            values[param.name] = param.convert(stored[key], strict=False) if key else initial(param, file_env)
        except ValueError:
            values[param.name] = initial(param, file_env)
    # Незнакомые параметры (из более новой версии) не теряем; прежние имена — уже перенесены.
    renamed = {a for p in PARAMETERS for a in p.aliases}
    obsolete = {"engines"}  # список сетей теперь определяется проверкой установки и входа
    values.update((k, v) for k, v in stored.items() if k not in BY_NAME and k not in renamed and k not in obsolete)
    with _lock:
        _values.clear()
        _values.update(values)
        if values != stored:
            _save()


def get(name):
    """Значение параметра: settings.get(settings.TIMEOUT.name)."""
    return _values[parameter(name).name]


def set(name, value):  # noqa: A001 — пара к get
    """Установить параметр и сохранить файл; возвращает новое значение."""
    param = parameter(name)
    value = param.convert(value)
    with _lock:
        changed = _values.get(param.name) != value
        _values[param.name] = value
        _save()
    if changed:
        for fn in _watchers.get(param.name, []):
            fn(value)
    return value


def watch(name, fn):
    """fn(значение) после каждого изменения параметра — для того, что нельзя перечитать на лету."""
    _watchers.setdefault(parameter(name).name, []).append(fn)


def listing():
    """Все параметры для формы: имя, название, описание, значение, тип, выбор и условие показа."""
    return [{"name": p.name, "title": p.title, "about": p.about, "value": _values[p.name],
             "default": p.default, "type": p.kind, "secret": p.secret,
             "choices": [{"value": v, "label": label} for v, label in p.choice_list()],
             "shown_if": {"name": p.shown_if[0], "value": p.shown_if[1]} if p.shown_if else None}
            for p in PARAMETERS]


load()
