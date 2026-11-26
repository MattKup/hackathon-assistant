#!/usr/bin/env bash
# Задача раннера: проверка на масштабе — синтетическая база из копий корпуса (~300 регламентов), сравнение с исходной.
# Аргументы: <папка корпуса> <eval.jsonl> [копий=75] [вопросов=40] — пути внутри INBOX_DIR.
set -euo pipefail
source "$(dirname "$0")/_python.sh"
exec "$PY" -I "$(dirname "$0")/../../scripts/eval/scale_test.py" "$@"
