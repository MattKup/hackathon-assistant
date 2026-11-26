#!/usr/bin/env bash
# Задача раннера: прогон эталона (поиск + уточнение + ответ LLM + проверки кодом).
# Аргументы: <папка корпуса> <eval.jsonl> [лимит] — пути внутри INBOX_DIR.
set -euo pipefail
exec python3 -I "$(dirname "$0")/../../scripts/eval/answer_eval.py" "$@"
