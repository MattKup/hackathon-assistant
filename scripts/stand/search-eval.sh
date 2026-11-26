#!/usr/bin/env bash
# Задача раннера: индекс окон (bge-m3) + поиск по вопросам с реранком; запись векторов и скоров для replay.
# Аргументы: <папка корпуса в INBOX_DIR> <файл вопросов в INBOX_DIR (jsonl: id, question)>
set -euo pipefail
exec python3 -I "$(dirname "$0")/../../scripts/eval/search_eval.py" "$@"
