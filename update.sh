#!/usr/bin/env bash
# Обновляет сайт из git и перезапускает пользовательский systemd-сервис, не обрывая ответы.
# Первая установка — start.sh. Запуск от того пользователя, у кого стоит сервис.
#
#   ./update.sh            подтянуть код; перезапустить, если что-то пришло
#   ./update.sh --restart  перезапустить и без новых коммитов
#
# Если идут ответы или расшифровки голосовых, новые сообщения перестают приниматься
# («Сайт обновляется»), а перезапуск делает отдельный процесс вне сервиса — когда они
# закончатся, но не дольше update_wait секунд из «Общих настроек» (по умолчанию 30 минут). Скрипт при этом
# сразу возвращается: запущенный из беседы на этом сайте, он не ждёт сам себя.
set -euo pipefail
cd "$(dirname "$0")"
SERVICE=${NEXUS_SERVICE:-nexus-web.service}
WAITER=${NEXUS_UPDATE_UNIT:-nexus-update}
if [ -f setup_site.py ]; then
  DATA=$(.venv/bin/python setup_site.py data)
else
  DATA=$(sed -n 's/^NEXUS_DATA=//p' .env 2>/dev/null | tail -1)
  DATA=${DATA:-$PWD/data}
fi
# Сколько ждать ответы — «Общие настройки» (update_wait в settings.json); переменная окружения сильнее.
WAIT=${NEXUS_UPDATE_WAIT:-$(.venv/bin/python -c 'import json, sys; print(json.load(open(sys.argv[1])).get("update_wait", ""))' \
  "$DATA/settings.json" 2>/dev/null || true)}
WAIT=${WAIT:-1800}
if [ "$WAIT" -ge 60 ]; then LIMIT="$((WAIT / 60)) мин"; else LIMIT="$WAIT с"; fi

# Сколько ответов и расшифровок идёт в нынешнем процессе сервиса (файл пишет сам сервис).
busy() {
  .venv/bin/python - "$DATA/busy.json" "$(systemctl --user show -p MainPID --value "$SERVICE")" <<'PY'
import json, sys
try:
    b = json.load(open(sys.argv[1]))
except (OSError, ValueError):
    b = {}
print(b["jobs"] + b["transcribing"] if b and str(b.get("pid")) == sys.argv[2] else 0)
PY
}

restart() {
  systemctl --user restart "$SERVICE"
  rm -f "$DATA/draining"
}

if [ "${1:-}" = "--wait" ]; then  # ожидатель: запускается ниже через systemd-run
  trap 'rm -f "$DATA/draining"' EXIT
  deadline=$(( $(date +%s) + WAIT ))
  last=""
  while n=$(busy); [ "$n" -gt 0 ] && [ "$(date +%s)" -lt "$deadline" ]; do
    [ "$n" != "$last" ] && echo "Жду, пока закончатся ответы: $n" && last=$n
    sleep 2
  done
  [ "$n" -gt 0 ] && echo "Не дождался за $LIMIT ($n), перезапускаю"
  restart
  echo "Сервис перезапущен"
  exit 0
fi

before=$(git rev-parse HEAD)
# Только перемотка: если на сервере есть свои коммиты или правки, остановиться, а не сливать.
git pull --ff-only
after=$(git rev-parse HEAD)

# Генерируем обычный unit-файл вместо прежнего симлинка на шаблон из репозитория.
if [ -f setup_site.py ] && [ "$(.venv/bin/python setup_site.py service)" = changed ]; then
  systemctl --user daemon-reload
fi

if [ "$before" = "$after" ]; then
  echo "Уже последняя версия ($(git log -1 --format='%h %s'))."
  [ "${1:-}" = "--restart" ] || exit 0
else
  git log --oneline "$before..$after"
  # Новые зависимости ставим до перезапуска, иначе сервис упадёт на импорте.
  if ! git diff --quiet "$before" "$after" -- requirements.txt; then
    .venv/bin/pip install -q -r requirements.txt
  fi
fi

if systemctl --user is-active --quiet "$WAITER"; then
  echo "Перезапуск уже ждёт окончания ответов — он подхватит и этот код."
  exit 0
fi
n=$(busy)
if [ "$n" -eq 0 ]; then
  restart
  systemctl --user --no-pager status "$SERVICE" | head -5
  echo "логи: journalctl --user -u nexus-web -f"
  exit 0
fi
touch "$DATA/draining"
# Окружение systemd-run не наследует: настройки передаём явно.
systemd-run --user --quiet --collect --unit="$WAITER" --working-directory="$PWD" \
  -E NEXUS_SERVICE="$SERVICE" -E NEXUS_UPDATE_UNIT="$WAITER" -E NEXUS_UPDATE_WAIT="$WAIT" \
  "$PWD/update.sh" --wait
echo "Идут ответы: $n. Новые сообщения пока не принимаются; сервис перезапустится, когда ответы"
echo "закончатся (не дольше $LIMIT). Ход: journalctl --user -u $WAITER -f"
