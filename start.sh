#!/usr/bin/env bash
# Ставит и запускает сайт как пользовательский systemd-сервис.
# Разовый запуск в текущем терминале: .venv/bin/python app.py
set -euo pipefail
cd "$(dirname "$0")"
# пути в nexus-web.service — от домашней папки (%h)
[ "$PWD" = "$HOME/projects/NexusAI" ] || { echo "проект должен лежать в ~/projects/NexusAI, а не в $PWD" >&2; exit 1; }

[ -f .env ] || cp .env.example .env
[ -x .venv/bin/python ] || python3 -m venv .venv
.venv/bin/pip install -q -r requirements.txt

if [ -z "$(.venv/bin/python users.py list)" ]; then
  echo "Пользователей ещё нет — создаём администратора."
  read -rp "Имя: " name
  .venv/bin/python users.py add "$name" --admin
fi

mkdir -p ~/.config/systemd/user
ln -sf "$PWD/nexus-web.service" ~/.config/systemd/user/nexus-web.service
systemctl --user daemon-reload
systemctl --user enable --now nexus-web.service
systemctl --user restart nexus-web.service
loginctl enable-linger "$USER" 2>/dev/null || echo "не смог включить linger — сервис не поднимется до входа в систему"

systemctl --user --no-pager status nexus-web.service | head -5
echo "логи: journalctl --user -u nexus-web -f"
