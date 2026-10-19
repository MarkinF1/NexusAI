#!/usr/bin/env bash
# Установка самого NexusAI на Debian 13. CLI нейросетей устанавливаются отдельно.
set -Eeuo pipefail
trap 'echo "Установка остановлена на строке $LINENO. Исправьте ошибку выше и повторите ./start.sh." >&2' ERR
PROJECT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)
cd "$PROJECT_DIR"

fail() { echo "$*" >&2; exit 1; }
ask() {
  [[ -t 0 ]] || fail "Нужен интерактивный терминал: запустите bash start.sh вручную."
  read -r -p "$1" "$2"
}
as_root() {
  if [[ $EUID -eq 0 ]]; then "$@"
  elif command -v sudo >/dev/null; then sudo "$@"
  else
    local root_command
    printf -v root_command '%q ' "$@"
    echo 'Нужен пароль root для установки системных зависимостей.' >&2
    su -c "$root_command"
  fi
}

[[ -f /etc/os-release ]] || fail 'Не удалось определить ОС.'
. /etc/os-release
[[ ${ID:-} == debian && ${VERSION_ID%%.*} -ge 13 ]] || fail 'Автоустановка рассчитана на Debian 13 или новее.'
case $(uname -m) in x86_64|aarch64) ;; *) fail 'Поддерживаются Linux x86_64 и ARM64.' ;; esac
[[ -d /run/systemd/system ]] || fail 'Для фонового запуска нужен systemd (контейнер без systemd не подходит).'

install_packages() {
  local package state
  local packages=(git curl ca-certificates python3 python3-venv python3-pip
    bubblewrap socat ripgrep nodejs npm libgomp1 ffmpeg
    libreoffice-writer libreoffice-calc libreoffice-impress poppler-utils
    fonts-dejavu-core fonts-liberation dbus-user-session libpam-systemd)
  local missing=()
  for package in "${packages[@]}"; do
    state=$(dpkg-query -W -f='${db:Status-Status}' "$package" 2>/dev/null || true)
    [[ $state == installed ]] || missing+=("$package")
  done
  if [[ ${#missing[@]} -gt 0 ]]; then
    echo "Устанавливаем системные зависимости: ${missing[*]}"
    as_root apt-get update
    as_root apt-get install -y --no-install-recommends "${missing[@]}"
  fi
}
install_packages

# CLI и веб-сервис работают от обычного пользователя даже при установке от root.
if [[ $EUID -eq 0 ]]; then
  default_user=${SUDO_USER:-nexus}
  [[ $default_user != root ]] || default_user=nexus
  ask "Пользователь Linux для сервиса [$default_user]: " service_user
  service_user=${service_user:-$default_user}
  [[ $service_user =~ ^[a-z_][a-z0-9_-]*$ && $service_user != root ]] || fail 'Укажите имя обычного пользователя Linux.'
  if ! id "$service_user" >/dev/null 2>&1; then
    useradd --create-home --shell /bin/bash "$service_user"
    echo "Создан пользователь $service_user (без пароля для входа в ОС)."
  fi
  service_uid=$(id -u "$service_user")
  [[ $service_uid != 0 ]] || fail 'Сервис нельзя запускать под UID 0.'
  service_gid=$(id -g "$service_user")
  service_home=$(getent passwd "$service_user" | cut -d: -f6)
  destination=$PROJECT_DIR
  if ! runuser -u "$service_user" -- test -w "$PROJECT_DIR" || ! runuser -u "$service_user" -- test -r "$PROJECT_DIR/start.sh"; then
    destination="$service_home/NexusAI"
    [[ ! -e $destination ]] || fail "Папка $destination уже существует. Запустите её start.sh от $service_user или выберите другую папку клонирования."
    ask "Скопировать проект в $destination для пользователя $service_user? [Y/n]: " copy_answer
    case $copy_answer in n|N|нет) fail 'Запустите установку из папки, доступной выбранному пользователю.' ;; esac
    cp -a -- "$PROJECT_DIR" "$destination"
    chown -R -- "$service_uid:$service_gid" "$destination"
    echo "Копия создана: $destination. Исходная папка сохранена."
  fi
  loginctl enable-linger "$service_user"
  systemctl start "user@$service_uid.service"
  printf -v launch 'env XDG_RUNTIME_DIR=%q DBUS_SESSION_BUS_ADDRESS=%q bash %q' \
    "/run/user/$service_uid" "unix:path=/run/user/$service_uid/bus" "$destination/start.sh"
  exec runuser --login "$service_user" --shell /bin/bash --command "$launch"
fi

[[ -w $PROJECT_DIR ]] || fail "Нет права записи в $PROJECT_DIR."
export PATH="$HOME/.local/bin:/usr/local/bin:/usr/bin:/bin:$PATH"
export XDG_RUNTIME_DIR="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}"
export DBUS_SESSION_BUS_ADDRESS="${DBUS_SESSION_BUS_ADDRESS:-unix:path=$XDG_RUNTIME_DIR/bus}"
if ! systemctl --user show-environment >/dev/null 2>&1; then
  as_root loginctl enable-linger "$(id -un)"
  as_root systemctl start "user@$(id -u).service"
fi
systemctl --user show-environment >/dev/null || fail 'Не удалось подключиться к пользовательскому systemd.'
if [[ $(loginctl show-user "$(id -un)" -p Linger --value 2>/dev/null || true) != yes ]]; then
  as_root loginctl enable-linger "$(id -un)"
fi

# При переносе сохраняем прежний venv: его скрипты содержат абсолютные пути.
if [[ -d .venv ]]; then
  venv_prefix=$(.venv/bin/python -c 'import sys; print(sys.prefix)' 2>/dev/null || true)
  venv_location=
  if [[ -f .venv/.nexus-project ]]; then
    IFS= read -r venv_location < .venv/.nexus-project || true
  fi
  if [[ $venv_prefix != "$PROJECT_DIR/.venv" || ( -n $venv_location && $venv_location != "$PROJECT_DIR" ) ]]; then
    backup=".venv.before-install.$(date +%s)"
    [[ ! -e $backup ]] || fail "Резервная папка $backup уже существует."
    mv -- .venv "$backup"
    echo "Прежнее окружение сохранено в $backup; создаём новое."
  fi
fi
[[ -x .venv/bin/python ]] || python3 -m venv .venv
printf '%s\n' "$PROJECT_DIR" > .venv/.nexus-project
.venv/bin/python -m pip install --upgrade pip
.venv/bin/python -m pip install -r requirements.txt

bwrap --ro-bind / / --proc /proc --dev /dev /bin/true || fail 'Bubblewrap не работает. Проверьте user namespaces и ограничения контейнера/хостинга.'
.venv/bin/python setup_site.py configure
.venv/bin/python setup_site.py admin
.venv/bin/python setup_site.py service
systemctl --user daemon-reload
systemctl --user enable nexus-web.service
systemctl --user restart nexus-web.service
.venv/bin/python setup_site.py wait
systemctl --user is-active --quiet nexus-web.service
echo 'Логи: journalctl --user -u nexus-web -f'
echo 'CLI нейросетей не устанавливаются. Авторизация подключённых CLI доступна в общих настройках.'
