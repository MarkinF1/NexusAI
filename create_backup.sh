#!/usr/bin/env bash
# Полный бэкап данных для восстановления; Python использует только стандартную библиотеку.
set -euo pipefail
PROJECT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)
exec python3 -B "$PROJECT_DIR/tools/create_backup.py" "$@"
