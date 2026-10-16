#!/usr/bin/env bash
# Обновляет сайт из git и перезапускает пользовательский systemd-сервис.
# Первая установка — start.sh. Запуск от того пользователя, у кого стоит сервис.
set -euo pipefail
cd "$(dirname "$0")"

before=$(git rev-parse HEAD)
# Только перемотка: если на сервере есть свои коммиты или правки, остановиться, а не сливать.
git pull --ff-only
after=$(git rev-parse HEAD)

if [ "$before" = "$after" ]; then
  echo "Уже последняя версия ($(git log -1 --format='%h %s'))."
else
  git log --oneline "$before..$after"
  # Новые зависимости ставим до перезапуска, иначе сервис упадёт на импорте.
  if ! git diff --quiet "$before" "$after" -- requirements.txt; then
    .venv/bin/pip install -q -r requirements.txt
  fi
fi

# Если скрипт запущен из беседы на этом сайте, перезапуск оборвёт и её — это нормально.
systemctl --user restart nexus-web.service
systemctl --user --no-pager status nexus-web.service | head -5
echo "логи: journalctl --user -u nexus-web -f"
