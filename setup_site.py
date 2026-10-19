"""Мастер установки и переносимый unit systemd; пароли и .env не передаются shell."""
import argparse
import getpass
import ipaddress
import os
from pathlib import Path
import re
import socket
import tempfile
import time
import urllib.error
import urllib.request

HERE = Path(__file__).resolve().parent


def env_values(folder=HERE):
    values = {}
    path = Path(folder) / ".env"
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            key, sep, value = line.strip().partition("=")
            if sep and key and not key.startswith("#"):
                values[key.strip()] = value.strip().strip("\"'")
    return values


def load_environment():
    for key, value in env_values().items():
        os.environ.setdefault(key, value)


def data_dir(folder=HERE):
    value = os.environ.get("NEXUS_DATA") or env_values(folder).get("NEXUS_DATA") or "data"
    path = Path(value)
    return path if path.is_absolute() else Path(folder) / path


def quote_unit(value, executable=False):
    value = str(value)
    if any(c in value for c in "\n\r\0"):
        raise ValueError("Путь не должен содержать перевод строки или NUL")
    value = value.replace("\\", "\\\\").replace('"', '\\"').replace("%", "%%")
    if executable:
        value = value.replace("$", "$$")
    return f'"{value}"'


def render_service(folder=HERE):
    folder = Path(folder).resolve()
    # Эти директивы принимают путь целиком, без shell-кавычек.
    quote_unit(folder)  # проверка перевода строки и NUL
    project = str(folder).replace('%', '%%')
    search_path = os.pathsep.join((str(Path.home() / ".local/bin"), "/usr/local/bin", "/usr/bin", "/bin"))
    values = {"PROJECT": project, "ENV_FILE": project + "/.env",
              # systemd не раскрывает переменные в имени исполняемого файла.
              "PYTHON": quote_unit(folder / ".venv/bin/python"),
              "APP": quote_unit(folder / "app.py", True), "PATH": quote_unit("PATH=" + search_path)}
    template = (folder / "nexus-web.service").read_text(encoding="utf-8")
    return re.sub(r"@(PROJECT|ENV_FILE|PYTHON|APP|PATH)@", lambda match: values[match[1]], template)


def write_service(folder=HERE, config=None):
    config = Path(config or os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
    target = config / "systemd/user/nexus-web.service"
    content = render_service(folder)
    if not target.is_symlink() and target.exists() and target.read_text(encoding="utf-8") == content:
        return False
    target.parent.mkdir(parents=True, exist_ok=True)
    # Заменяем старый симлинк, не перезаписывая шаблон в репозитории.
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=target.parent, delete=False) as f:
        temp = Path(f.name)
        f.write(content)
    try:
        os.replace(temp, target)
    finally:
        temp.unlink(missing_ok=True)
    return True


def configure():
    target = HERE / ".env"
    if target.exists():
        print("Существующая .env сохранена.")
        return
    print("127.0.0.1 — доступ с этого компьютера или через прокси/SSH-туннель.")
    print("0.0.0.0 — доступ из сети; для внешнего доступа настройте HTTPS.")
    while True:
        host = input("Адрес прослушивания [127.0.0.1]: ").strip() or "127.0.0.1"
        try:
            address = ipaddress.ip_address(host)
            break
        except ValueError:
            print("Введите IPv4 или IPv6 адрес.")
    while True:
        raw = input("Порт [8080]: ").strip() or "8080"
        try:
            port = int(raw)
            if not 1024 <= port <= 65535:
                raise ValueError
            with socket.socket(socket.AF_INET6 if address.version == 6 else socket.AF_INET) as sock:
                sock.bind((host, port))
            break
        except (ValueError, OSError):
            print("Нужен свободный порт от 1024 до 65535.")
    content = (HERE / ".env.example").read_text(encoding="utf-8")
    content = content.replace("NEXUS_HOST=127.0.0.1", "NEXUS_HOST=" + host)
    content = content.replace("NEXUS_PORT=8080", "NEXUS_PORT=" + str(port))
    with os.fdopen(os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w", encoding="utf-8") as f:
        f.write(content)


def ensure_admin():
    load_environment()
    import users
    if any(u["is_admin"] for u in users.listing()):
        print("Администратор уже существует; его пароль не меняется.")
        return
    print("Создание первого администратора сайта (не пользователя Linux).")
    while True:
        name = input("Имя администратора: ").strip()
        if not name:
            continue
        password = getpass.getpass("Пароль: ")
        if not password:
            print("Пароль не должен быть пустым.")
            continue
        if password != getpass.getpass("Повторите пароль: "):
            print("Пароли не совпали, повторите ввод.")
            continue
        try:
            users.add(name, password, admin=True)
            print("Администратор создан.")
            return
        except ValueError as exc:
            print(exc)


def wait_ready(timeout=210):
    load_environment()
    host = os.environ.get("NEXUS_HOST", "127.0.0.1")
    host = {"0.0.0.0": "127.0.0.1", "::": "::1"}.get(host, host)
    if ":" in host:
        host = "[" + host + "]"
    url = f"http://{host}:{os.environ.get('NEXUS_PORT', '8080')}"
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            opener.open(url + "/api/me", timeout=2).close()
        except urllib.error.HTTPError as exc:
            exc.close()
            if exc.code != 401:
                time.sleep(1)
                continue
        except (OSError, urllib.error.URLError):
            time.sleep(1)
            continue
        print("NexusAI запущен: " + url)
        return
    raise SystemExit("Сервис не ответил. Логи: journalctl --user -u nexus-web -n 50 --no-pager")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("configure", "admin", "service", "data", "wait"))
    action = parser.parse_args().action
    os.chdir(HERE)
    if action == "configure":
        configure()
    elif action == "admin":
        ensure_admin()
    elif action == "service":
        print("changed" if write_service() else "unchanged")
    elif action == "data":
        print(data_dir())
    elif action == "wait":
        wait_ready()


if __name__ == "__main__":
    main()
