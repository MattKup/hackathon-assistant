#!/usr/bin/env bash
# Задача раннера: прогон эталона (поиск + уточнение + ответ LLM + проверки кодом).
# Аргументы: <папка корпуса> <eval.jsonl> [лимит] — пути внутри INBOX_DIR.
set -euo pipefail
source "$(dirname "$0")/_python.sh"
exec "$PY" -I "$(dirname "$0")/../../scripts/eval/answer_eval.py" "$@"
