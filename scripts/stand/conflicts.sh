#!/usr/bin/env bash
# Задача раннера: поиск противоречий между пунктами регламентов (кандидаты + сигналы кода + судья LLM + проверка цитат).
# Аргументы: <папка корпуса в INBOX_DIR> [бюджет пар] [k=8] [workers=4] [--no-dense] — файлы пишутся в RUN_DIR.
set -euo pipefail
exec python3 -I "$(dirname "$0")/../../scripts/eval/conflicts.py" "$@"
