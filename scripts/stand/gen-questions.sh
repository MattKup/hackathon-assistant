#!/usr/bin/env bash
# Задача раннера (Г14): вопросы-«входы» к каждому пункту корпуса для поискового индекса (локальная LLM, ночью).
# Аргументы: <папка корпуса в INBOX_DIR> [workers=8]
set -euo pipefail
source "$(dirname "$0")/_python.sh"
exec "$PY" -I "$(dirname "$0")/../../scripts/eval/gen_questions.py" "$@"
