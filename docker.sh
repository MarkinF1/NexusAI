#!/usr/bin/env bash
# Docker alternative to start.sh; never touches the native systemd service.
set -Eeuo pipefail
cd -- "$(dirname -- "${BASH_SOURCE[0]}")"
command -v docker >/dev/null || { echo 'Установите Docker Engine и Docker Compose v2.' >&2; exit 1; }
docker compose version >/dev/null
config=${NEXUS_DOCKER_ENV:-.env.docker}
if [[ ! -f $config ]]; then
  if [[ $config != .env.docker ]]; then
    echo "Нет файла настроек: $config" >&2
    exit 1
  fi
  (umask 077; cp -n docker.env.example "$config")
  echo "Создан $config. По умолчанию используется 127.0.0.1:8081 и отдельные Docker-тома."
fi
compose=(docker compose --env-file "$config" -f compose.yaml)
if [[ ${1:-} == --auth ]]; then
  compose+=(-f compose.auth.yaml)
  shift
fi
action=${1:-start}
if (( $# > 1 )); then
  echo 'Ожидалась одна команда. См. ./docker.sh help.' >&2
  exit 2
fi
case "$action" in
  start|setup)
    "${compose[@]}" build
    # No password in env, command line or logs; asks only if no admin exists.
    "${compose[@]}" run --rm --no-deps nexus setup
    if [[ $action == start ]]; then
      "${compose[@]}" up -d --wait --wait-timeout 240
      "${compose[@]}" ps
    fi
    ;;
  update)
    echo 'Обновляется только Docker-вариант. Дождитесь окончания ответов перед обновлением.'
    "${compose[@]}" build
    "${compose[@]}" up -d --wait --wait-timeout 240
    ;;
  stop) "${compose[@]}" down ;;  # Deliberately no -v: retain chats, CLI and auth.
  restart) "${compose[@]}" restart nexus ;;
  logs) "${compose[@]}" logs --tail 100 -f nexus ;;
  status) "${compose[@]}" ps ;;
  shell) "${compose[@]}" exec nexus bash ;;
  *) echo 'Использование: ./docker.sh [--auth] {start|setup|update|restart|stop|logs|status|shell}' >&2; exit 2 ;;
esac
