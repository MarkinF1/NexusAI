"""Portable, versioned data backups. No application imports or AI credential reads."""
import argparse
import contextlib
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import pwd
import signal
import sqlite3
import stat
import subprocess
import sys
import tarfile
import tempfile
import time

PROJECT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT))
from setup_site import env_values

RESTORE = """Восстановление NexusAI (формат nexusai-data-backup, версия 1)

В архиве: data/ — данные приложения; manifest.json — версия, дата, количество
пользователей/бесед и SHA-256 каждого файла. Пути отсчитываются от корня архива.
Программный код, .env сервера, модели и авторизация ИИ в архив не входят.
Пользовательские файлы, включая их .env, сохранены без редактирования.

1. Установите совместимую версию NexusAI и зависимости, остановите его сервис.
2. Распакуйте архив в НОВУЮ пустую папку, а не поверх работающей data:
     mkdir restore
     tar --no-same-owner -xzf backup.tar.gz -C restore
3. Проверьте manifest.json: format=nexusai-data-backup, version=1.
   Сравните SHA-256 файлов из files с их sha256, ссылки — с target.
   Для data/nexus.db выполните PRAGMA integrity_check; ожидается ok.
4. Сохраните прежнюю data отдельно. Поместите restore/data на её место
   (или по пути NEXUS_DATA из .env). Не объединяйте старые и новые данные.
   Назначьте владельцем Linux-пользователя сервиса:
     chown -R nexus:nexus /opt/NexusAI/data
     chmod 700 /opt/NexusAI/data
   Команды и пользователя адаптируйте к вашей установке.
5. Запустите сервис. Проверьте аккаунты, историю бесед, файлы и настройки.

Восстанавливаются все аккаунты на момент снимка, хеши паролей, настройки,
сессии сайта, привязки Telegram, токен бота, настройки и ключи Web Push.
На другом домене браузерные push-подписки может потребоваться создать заново.
Для продолжения CLI-сессий предпочтителен прежний абсолютный путь проекта;
авторизацию и программы ИИ подключайте отдельно.
Не запускайте один Telegram-токен одновременно на старом и новом сервере.
Символические ссылки сохранены как ссылки; внешние цели в архив не включаются.
Архив содержит конфиденциальные данные пользователей и токен Telegram.
"""


def json_file(path):
    with open(path, encoding="utf-8") as stream:
        return json.load(stream)


def excluded(parts):
    if parts[0] in {"engine", "lo-profile", "busy.json", "busy.json.tmp", "draining", ".backup.lock"}:
        return True
    # Only provider-owned credential paths; never filter arbitrary user files.
    if len(parts) >= 4 and parts[0] == "users":
        if parts[2] == "claude" and parts[3] in {".credentials.json", ".claude.json"}:
            return True
        if parts[2] == "codex-home":
            return parts[3] in {"auth.json", "tmp", "cache", "log", "shell_snapshots"} or parts[3].startswith("logs_") and ".sqlite" in parts[3]
    return False


class Service:
    """Operate the data owner's user service, including when invoked by root."""
    def __init__(self, data):
        uid = data.stat().st_uid
        if os.geteuid() not in (0, uid):
            raise RuntimeError("Запустите скрипт от владельца данных или root.")
        self.name = os.environ.get("NEXUS_SERVICE", "nexus-web.service")
        self.prefix = []
        if os.geteuid() == 0 and uid != 0:
            self.prefix = ["runuser", "-u", pwd.getpwuid(uid).pw_name, "--"]
        self.prefix += ["env", f"XDG_RUNTIME_DIR=/run/user/{uid}",
                        f"DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/{uid}/bus", "systemctl", "--user"]

    def run(self, *args, check=False):
        try:
            result = subprocess.run(self.prefix + list(args) + [self.name], capture_output=True, text=True)
        except FileNotFoundError:
            if check:
                raise RuntimeError("Не найден systemctl/runuser.")
            return ""
        if check and result.returncode:
            raise RuntimeError(f"Не удалось выполнить systemctl {' '.join(args)}: {result.stderr.strip()}")
        return result.stdout.strip() if result.returncode == 0 else ""


def alive(pid):
    if not isinstance(pid, int) or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def unmanaged_app(pid):
    if not alive(pid):
        return False
    try:
        arguments = Path(f"/proc/{pid}/cmdline").read_bytes().decode(errors="replace").split("\0")
        cwd = Path(f"/proc/{pid}/cwd").resolve(strict=True)
    except PermissionError:
        return True  # Cannot safely establish that this is a stale/reused PID.
    except FileNotFoundError:
        return False
    # A copied/stopped Docker backup can have a PID now belonging to host init or
    # an unrelated process. Never treat that recycled PID as a running NexusAI.
    return str(PROJECT / "app.py") in arguments or (
        cwd == PROJECT and any(arg in {"app.py", "app:app"} for arg in arguments))


@contextlib.contextmanager
def stopped_service(data, wait):
    service = Service(data)
    state = service.run("show", "-p", "ActiveState", "--value")
    if state in {"activating", "deactivating", "reloading"}:
        raise RuntimeError("Сервис меняет состояние; повторите бэкап после завершения операции.")
    active = state == "active"
    drain = data / "draining"
    draining = restart = False
    try:
        if active:
            working = service.run("show", "-p", "WorkingDirectory", "--value", check=True)
            if Path(working).resolve() != PROJECT:
                raise RuntimeError("User-сервис относится к другому проекту; запускайте бэкап из его установки.")
            cgroup = Path("/proc/self/cgroup")
            if cgroup.exists() and service.name in cgroup.read_text():
                raise RuntimeError("Запустите бэкап в отдельном SSH-терминале, вне сервиса NexusAI.")
            pid = int(service.run("show", "-p", "MainPID", "--value", check=True))
            # Exclusive creation also refuses to race an update already draining jobs.
            with drain.open("x"):
                pass
            draining = True
            deadline = time.monotonic() + wait
            print("Ожидаю завершения ответов и расшифровок…", flush=True)
            while True:
                busy = json_file(data / "busy.json")
                if busy.get("pid") != pid:
                    raise RuntimeError("Состояние сервиса изменилось; повторите бэкап.")
                if not busy.get("jobs", 0) and not busy.get("transcribing", 0):
                    break
                if time.monotonic() >= deadline:
                    raise RuntimeError("Не дождался окончания ответов; бэкап отменён без остановки сервиса.")
                time.sleep(1)
            print("Останавливаю сервис на время создания снимка…", flush=True)
            restart = True  # A partially successful stop must still be followed by start.
            service.run("stop", check=True)
        elif (data / "busy.json").exists() and unmanaged_app(json_file(data / "busy.json").get("pid")):
            raise RuntimeError("Обнаружен работающий процесс NexusAI без управляемого user-сервиса. Остановите его перед бэкапом (для Docker — контейнер).")
        yield
    finally:
        if draining:
            drain.unlink(missing_ok=True)
        if restart:
            service.run("start", check=True)
            print("Сервис снова запущен; архивирование продолжается из снимка.", flush=True)


@contextlib.contextmanager
def backup_lock(data):
    fd = os.open(data / ".backup.lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        if os.geteuid() == 0:
            owner = data.stat()
            os.fchown(fd, owner.st_uid, owner.st_gid)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError("Для этих данных уже создаётся бэкап.") from None
        yield
    finally:
        os.close(fd)


def snapshot(data, target):
    """Copy the stopped tree once, preserving hard links without dereferencing links."""
    entries, hardlinks = [], {}
    target.mkdir(mode=0o700)

    def copy(src, dst, relative):
        info = src.lstat()
        entry = {"path": "data/" + relative.as_posix(), "mode": stat.S_IMODE(info.st_mode) & 0o777}
        if stat.S_ISLNK(info.st_mode):
            link = os.readlink(src)
            dst.symlink_to(link)
            entry.update(type="symlink", target=link)
        elif stat.S_ISDIR(info.st_mode):
            dst.mkdir(mode=0o700)
            entry["type"] = "directory"
            for child in sorted(src.iterdir()):
                rel = relative / child.name
                if not excluded(rel.parts):
                    copy(child, dst / child.name, rel)
        elif stat.S_ISREG(info.st_mode):
            key = (info.st_dev, info.st_ino)
            if key in hardlinks:
                first, digest = hardlinks[key]
                os.link(first, dst)
            else:
                digest = hashlib.sha256()
                with src.open("rb") as reader, dst.open("xb") as writer:
                    for chunk in iter(lambda: reader.read(1024 * 1024), b""):
                        writer.write(chunk)
                        digest.update(chunk)
                digest = digest.hexdigest()
                hardlinks[key] = (dst, digest)
            entry.update(type="file", size=info.st_size, sha256=digest)
        else:
            raise RuntimeError(f"Нельзя сохранить специальный файл: {relative}")
        if not stat.S_ISLNK(info.st_mode):
            os.chmod(dst, entry["mode"])
            os.utime(dst, ns=(info.st_atime_ns, info.st_mtime_ns))
        after = src.lstat()
        if (info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns) != (
                after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns):
            raise RuntimeError(f"Файл изменился во время снимка: {relative}. Остановите внешние процессы записи и повторите бэкап.")
        entries.append(entry)

    for child in sorted(data.iterdir()):
        if child.name not in {"nexus.db", "nexus.db-wal", "nexus.db-shm"} and not excluded((child.name,)):
            copy(child, target / child.name, Path(child.name))
    # SQLite's backup API includes committed WAL transactions without copying shm/WAL.
    with contextlib.closing(sqlite3.connect((data / "nexus.db").as_uri() + "?mode=ro", uri=True)) as source:
        with contextlib.closing(sqlite3.connect(target / "nexus.db")) as dest:
            source.backup(dest)
            if dest.execute("PRAGMA integrity_check").fetchone() != ("ok",):
                raise RuntimeError("Копия базы пользователей не прошла integrity_check.")
            user_count = dest.execute("SELECT count(*) FROM users").fetchone()[0]
    database = target / "nexus.db"
    os.chmod(database, 0o600)
    with database.open("rb") as stream:
        database_hash = hashlib.file_digest(stream, "sha256").hexdigest()
    entries.append({"path": "data/nexus.db", "type": "file", "mode": 0o600,
                    "size": database.stat().st_size, "sha256": database_hash})
    chats = sum(1 for path in (target / "users").glob("*/chats/*.json")
                if len(path.stem) == 32 and all(c in "0123456789abcdef" for c in path.stem))
    return {"format": "nexusai-data-backup", "version": 1,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "data_directory": "data", "database": "data/nexus.db",
            "counts": {"users": user_count, "chats": chats},
            "files": sorted(entries, key=lambda item: item["path"])}


def portable_owner(info):
    info.uid = info.gid = 0
    info.uname = info.gname = ""
    info.mode &= 0o777
    return info


def create(destination, name, wait):
    value = os.environ.get("NEXUS_DATA") or env_values(PROJECT).get("NEXUS_DATA") or "data"
    data = (PROJECT / value).resolve()
    if not data.is_dir() or not (data / "nexus.db").is_file():
        raise RuntimeError(f"Не найдена папка данных с nexus.db: {data}")
    destination = destination.resolve()
    if destination.is_relative_to(data):
        raise RuntimeError("Папка архива не должна находиться внутри папки данных.")
    destination.mkdir(parents=True, exist_ok=True)
    output = destination / name
    if output.exists() or output.is_symlink():
        raise RuntimeError(f"Архив уже существует: {output}. Укажите другое имя через -o.")
    with backup_lock(data), tempfile.TemporaryDirectory(prefix=".nexus-backup-", dir=destination) as folder:
        stage = Path(folder)
        with stopped_service(data, wait):
            manifest = snapshot(data, stage / "data")
        (stage / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        (stage / "RESTORE.txt").write_text(RESTORE, encoding="utf-8")
        fd, temporary = tempfile.mkstemp(prefix="." + name + ".", suffix=".partial", dir=destination)
        try:
            with os.fdopen(fd, "wb") as stream:
                with tarfile.open(fileobj=stream, mode="w:gz", compresslevel=6, format=tarfile.PAX_FORMAT) as archive:
                    for item in ("manifest.json", "RESTORE.txt", "data"):
                        archive.add(stage / item, arcname=item, filter=portable_owner)
                stream.flush()
                os.fsync(stream.fileno())
            os.link(temporary, output)  # Atomic publish; never overwrite an existing backup.
            directory_fd = os.open(destination, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        finally:
            Path(temporary).unlink(missing_ok=True)
    print(f"Бэкап создан: {output}\nПользователей: {manifest['counts']['users']}; бесед: {manifest['counts']['chats']}")


def main():
    parser = argparse.ArgumentParser(prog="create_backup.sh", description="Архив данных NexusAI для восстановления (tar.gz). Активный user-сервис будет остановлен только на время снимка и затем запущен.")
    parser.add_argument("directory", nargs="?", type=Path, default=PROJECT, help="Папка архива; по умолчанию рядом со скриптом.")
    parser.add_argument("-o", "--output", default="backup.tar.gz", help="Имя архива (по умолчанию backup.tar.gz). Существующий файл не перезаписывается.")
    parser.add_argument("--wait", type=int, default=1800, metavar="SECONDS", help="Сколько ждать окончания ответов перед снимком (по умолчанию 1800 с).")
    args = parser.parse_args()
    if not args.output or args.output in {".", ".."} or Path(args.output).name != args.output:
        parser.error("В -o укажите только имя файла; папку передайте отдельным аргументом.")
    if args.wait < 0:
        parser.error("--wait не может быть отрицательным.")
    os.umask(0o077)
    def interrupted(signum, frame):
        raise KeyboardInterrupt
    for sig in (signal.SIGTERM, signal.SIGHUP):
        signal.signal(sig, interrupted)
    try:
        create(args.directory, args.output, args.wait)
    except (OSError, RuntimeError, ValueError, sqlite3.Error) as error:
        print(f"Бэкап не создан: {error}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("Создание бэкапа прервано.", file=sys.stderr)
        return 130
    return 0


if __name__ == "__main__":
    sys.exit(main())
